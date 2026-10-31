#!/usr/bin/env python3
"""Bound QEMU's result/shutdown waits and retain a separate guest checklist."""
import argparse
import html
from pathlib import Path
import re
import subprocess
import time

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def read_console(path):
    text = path.read_bytes().decode('utf-8', errors='replace') if path.exists() else ''
    return html.unescape(ANSI.sub('', text).replace('\r', ''))


def guest_result(console):
    lines = console.splitlines()
    if 'Randy VM smoke test: FAIL' in lines:
        return 'FAIL'
    if 'Randy VM smoke test: PASS' in lines:
        return 'PASS'
    return None


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run(command, log_dir, boot_timeout=600, shutdown_timeout=30):
    log_dir.mkdir(parents=True, exist_ok=True)
    serial = log_dir / 'serial.log'
    # No result from a previous invocation may satisfy this boot.
    serial.write_bytes(b'')
    (log_dir / 'console.log').write_text('')
    (log_dir / 'result.txt').write_text('Host: waiting for guest smoke result\n')
    deadline = time.monotonic() + boot_timeout
    result_deadline = None
    reason = None
    process = None
    try:
        with (log_dir / 'qemu.log').open('wb') as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
            while True:
                result = guest_result(read_console(serial))
                if result and result_deadline is None:
                    result_deadline = time.monotonic() + shutdown_timeout
                status = process.poll()
                if status is not None:
                    # Read again after exit to include the final serial write.
                    result = guest_result(read_console(serial))
                    if result == 'FAIL':
                        reason = 'guest smoke checks failed'
                    elif status != 0:
                        reason = f'QEMU exited with status {status}'
                    elif result != 'PASS':
                        reason = 'guest exited before smoke result'
                    else:
                        reason = 'PASS'
                    break
                now = time.monotonic()
                if result_deadline is not None and now >= result_deadline:
                    reason = 'guest smoke checks failed' if result == 'FAIL' else 'guest did not exit after smoke result'
                    break
                if result_deadline is None and now >= deadline:
                    reason = 'timed out waiting for guest smoke result'
                    break
                time.sleep(0.1)
    except OSError as error:
        reason = f'could not start QEMU ({type(error).__name__})'
    finally:
        if process is not None:
            stop(process)
        console = read_console(serial)
        (log_dir / 'console.log').write_text(console)
        checklist = [line for line in console.splitlines()
                     if line.startswith(('Randy VM check:', 'Randy VM smoke test:'))]
        summary = '\n'.join(checklist) + f'\nHost: {reason or "interrupted"}\n'
        (log_dir / 'result.txt').write_text(summary)
    print(summary, end='')
    return 0 if reason == 'PASS' else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log-dir', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('QEMU command is required')
    return run(command, args.log_dir)


if __name__ == '__main__':
    raise SystemExit(main())
