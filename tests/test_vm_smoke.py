"""Focused guest-check regressions; these do not claim a real QEMU boot."""
import configparser
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('vm_runner', ROOT / 'scripts/vm-smoke/run.py')
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class VmSmokeTests(unittest.TestCase):
    def guest(self, failure=''):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / 'efi').mkdir()
            (work / 'cmdline').write_text('root=test systemd.firstboot=no\n')
            (work / 'hostname').write_text('wrong\n' if failure == 'hostname' else 'randy\n')
            if failure == 'firstboot-flag':
                (work / 'cmdline').write_text('root=test\n')
            # Redirect only host-dependent paths. Exercise the real guest shell logic.
            text = (ROOT / 'scripts/vm-smoke/check.sh').read_text()
            for old, new in [('/dev/ttyS0', str(work / 'serial.log')),
                             ('/sys/firmware/efi', str(work / 'efi')),
                             ('/proc/cmdline', str(work / 'cmdline')),
                             ('/proc/sys/kernel/hostname', str(work / 'hostname')),
                             ('/usr/sbin/sshd', str(work / 'sshd')),
                             ('/run/randy-config/randy-wifi.txt', str(work / 'randy-wifi.txt'))]:
                text = text.replace(old, new)
            script = work / 'check.sh'
            script.write_text(text)
            systemctl = work / 'systemctl'
            systemctl.write_text('''#!/bin/bash
case "$1" in
  is-active)
    [[ "$3" != "$FAILURE" ]] ;;
  is-enabled) [[ "$FAILURE" != disabled-importer ]] ;;
  show)
    case "$2:$4" in
      systemd-firstboot.service:ActiveState)
        if [[ "$FAILURE" == firstboot-active ]]; then echo activating
        elif [[ "$FAILURE" == firstboot-completed || "$FAILURE" == firstboot-failed ]]; then echo active; else echo inactive; fi ;;
      systemd-firstboot.service:SubState) echo exited ;;
      systemd-firstboot.service:Result)
        if [[ "$FAILURE" == firstboot-failed ]]; then echo exit-code; else echo success; fi ;;
      randy-wifi-import.service:ActiveState)
        if [[ "$FAILURE" == importer-skipped ]]; then echo inactive; else echo active; fi ;;
      randy-wifi-import.service:SubState)
        if [[ "$FAILURE" == importer-blocked ]]; then echo running
        elif [[ "$FAILURE" == importer-skipped ]]; then echo dead; else echo exited; fi ;;
      randy-wifi-import.service:Result)
        if [[ "$FAILURE" == importer-failed ]]; then echo timeout; else echo success; fi ;;
      randy.service:LoadState)
        if [[ "$FAILURE" == randy.service ]]; then echo loaded; else echo not-found; fi ;;
      *:LoadState) echo not-found ;;
      *) exit 2 ;;
    esac ;;
  --failed)
    if [[ "$FAILURE" == failed-unit ]]; then echo 'local-fs.target loaded failed failed'
    elif [[ "$FAILURE" == noncritical-unit ]]; then echo 'optional-hardware.service loaded failed failed'; fi ;;
  --no-block) echo poweroff >> "$POWEROFF_LOG" ;;
  list-jobs) : ;;
  *) exit 2 ;;
esac
''')
            systemctl.chmod(0o755)
            journal = work / 'journalctl'
            journal.write_text('#!/bin/bash\necho synthetic-guest-journal\n')
            journal.chmod(0o755)
            sshd = work / 'sshd'
            sshd.write_text('#!/bin/bash\n[[ "$FAILURE" != ssh-config ]]\n')
            sshd.chmod(0o755)
            # Guest-only sshd invocation is exercised by the Linux VM; model its
            # success/failure here without touching host sshd or private keys.
            python = work / 'python3'
            python.write_text('#!/bin/bash\ncat >/dev/null\n[[ "$FAILURE" != ssh-banner ]]\n')
            python.chmod(0o755)
            result = subprocess.run(
                ['bash', str(script)], capture_output=True, text=True, timeout=10,
                env=dict(os.environ, PATH=str(work) + os.pathsep + os.environ['PATH'],
                         FAILURE=failure, POWEROFF_LOG=str(work / 'poweroff')))
            return result, (work / 'serial.log').read_text(), (work / 'poweroff').read_text()

    def test_guest_passes_without_wifi_hardware_or_optional_alpha2_units(self):
        result, log, poweroff = self.guest()
        self.assertEqual(result.returncode, 0, log + result.stderr)
        self.assertIn('Randy VM smoke test: PASS', log)
        self.assertNotIn('Randy VM smoke test: FAIL', log)
        self.assertIn('Randy VM check: PASS critical boot units healthy', log)
        self.assertIn('poweroff', poweroff)

    def test_guest_rejects_boot_service_and_firstboot_regressions(self):
        for failure in ['hostname', 'firstboot-flag', 'firstboot-active', 'firstboot-failed',
                        'NetworkManager.service', 'avahi-daemon.service', 'ssh.service',
                        'multi-user.target', 'disabled-importer', 'importer-blocked',
                        'importer-failed', 'failed-unit', 'ssh-config', 'ssh-banner']:
            with self.subTest(failure=failure):
                result, log, poweroff = self.guest(failure)
                self.assertNotEqual(result.returncode, 0, log)
                self.assertIn('Randy VM smoke test: FAIL', log)
                self.assertNotIn('Randy VM smoke test: PASS', log)
                self.assertIn('Randy VM check: FAIL', log)
                self.assertIn('synthetic-guest-journal', log)
                self.assertIn('poweroff', poweroff)

    def test_successfully_completed_firstboot_does_not_hide_ssh_banner_check(self):
        result, log, _ = self.guest('firstboot-completed')
        self.assertEqual(result.returncode, 0, log + result.stderr)
        self.assertIn('Randy VM smoke test: PASS', log)

    def test_skipped_importer_noncritical_hardware_and_alpha2_do_not_fail_alpha1(self):
        for state in ('importer-skipped', 'noncritical-unit', 'randy.service'):
            with self.subTest(state=state):
                result, log, _ = self.guest(state)
                self.assertEqual(result.returncode, 0, log + result.stderr)
                self.assertIn('Randy VM smoke test: PASS', log)

    def test_debug_profile_is_opt_in_and_distinct(self):
        normal = configparser.ConfigParser(interpolation=None)
        normal.read(ROOT / 'mkosi.conf')
        debug = configparser.ConfigParser(interpolation=None)
        debug.read(ROOT / 'mkosi.profiles/debug/mkosi.conf')
        self.assertEqual(normal['Content']['Autologin'], 'no')
        self.assertEqual(normal['Content']['RootPassword'], 'hashed:!')
        self.assertEqual(debug['Content']['Autologin'], 'yes')
        self.assertNotIn('RootPassword', debug['Content'])
        self.assertEqual(debug['Output']['Output'], 'randy-os-alpha1-debug-x86_64')
        self.assertNotIn('debug', normal.get('Config', 'Profiles', fallback=''))
        for name in ['issue', 'motd']:
            self.assertIn('DEBUG', (ROOT / 'mkosi.profiles/debug/mkosi.extra/etc' / name).read_text())


class HostResultTests(unittest.TestCase):
    def run_guest(self, code, timeout=2, shutdown_timeout=1, stale=False):
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            serial = log_dir / 'serial.log'
            if stale:
                serial.write_text('Randy VM smoke test: PASS\n')
            # Exercise the real subprocess, polling, exit-status and log lifecycle.
            command = [sys.executable, '-c',
                       'from pathlib import Path; import sys,time; p=Path(sys.argv[1]); ' + code,
                       str(serial)]
            with contextlib.redirect_stdout(io.StringIO()):
                status = RUNNER.run(command, log_dir, boot_timeout=timeout,
                                    shutdown_timeout=shutdown_timeout)
            return status, {p.name: p.read_text() for p in log_dir.iterdir()}

    def test_delayed_pass_and_final_write_immediately_before_clean_exit(self):
        for pause in (0, 0.2):
            status, logs = self.run_guest(
                f'time.sleep({pause}); p.write_text("Randy VM check: PASS hostname\\nRandy VM smoke test: PASS\\n")')
            self.assertEqual(status, 0, logs)
            self.assertIn('Randy VM check: PASS hostname', logs['result.txt'])
            self.assertIn('Host: PASS', logs['result.txt'])

    def test_clean_exit_without_result_and_stale_pass_are_rejected(self):
        for stale in (False, True):
            status, logs = self.run_guest('pass', stale=stale)
            self.assertEqual(status, 1)
            self.assertIn('guest exited before smoke result', logs['result.txt'])

    def test_guest_fail_retains_checklist_and_complete_shutdown_diagnostics(self):
        status, logs = self.run_guest(
            'p.write_text("Randy VM check: FAIL hostname\\nRandy VM smoke test: FAIL\\n"); '
            'time.sleep(.2); p.open("a").write("final guest diagnostic\\n")')
        self.assertEqual(status, 1)
        self.assertIn('guest smoke checks failed', logs['result.txt'])
        self.assertIn('Randy VM check: FAIL hostname', logs['result.txt'])
        self.assertIn('final guest diagnostic', logs['serial.log'])

    def test_nonzero_qemu_exit_is_not_hidden_by_pass(self):
        status, logs = self.run_guest('p.write_text("Randy VM smoke test: PASS\\n"); sys.exit(2)')
        self.assertEqual(status, 1)
        self.assertIn('QEMU exited with status 2', logs['result.txt'])

    def test_boot_and_shutdown_waits_are_bounded(self):
        status, logs = self.run_guest('time.sleep(10)', timeout=.15)
        self.assertEqual(status, 1)
        self.assertIn('timed out waiting for guest smoke result', logs['result.txt'])
        status, logs = self.run_guest('p.write_text("Randy VM smoke test: PASS\\n"); time.sleep(10)',
                                      shutdown_timeout=.15)
        self.assertEqual(status, 1)
        self.assertIn('guest did not exit after smoke result', logs['result.txt'])

    def test_crlf_ansi_and_html_entities_do_not_hide_result(self):
        status, logs = self.run_guest(
            'p.write_text("\\x1b[32mRandy&#x20;VM&#x20;smoke&#x20;test:&#x20;PASS\\x1b[0m\\r\\n")')
        self.assertEqual(status, 0, logs)
        self.assertIn('&#x20;', logs['serial.log'])
        self.assertIn('Randy VM smoke test: PASS', logs['result.txt'])

    def test_failure_takes_precedence_over_pass(self):
        status, logs = self.run_guest(
            'p.write_text("Randy VM smoke test: PASS\\nRandy VM smoke test: FAIL\\n")')
        self.assertEqual(status, 1)
        self.assertIn('guest smoke checks failed', logs['result.txt'])


if __name__ == '__main__':
    unittest.main()
