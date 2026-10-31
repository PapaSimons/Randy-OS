"""Focused guest-check regressions; these do not claim a real QEMU boot."""
import configparser
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


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
        if [[ "$FAILURE" == firstboot-active ]]; then echo activating; else echo inactive; fi ;;
      randy-wifi-import.service:ActiveState) echo active ;;
      randy-wifi-import.service:SubState)
        if [[ "$FAILURE" == importer-blocked ]]; then echo running; else echo exited; fi ;;
      randy-wifi-import.service:Result)
        if [[ "$FAILURE" == importer-failed ]]; then echo timeout; else echo success; fi ;;
      randy.service:LoadState)
        if [[ "$FAILURE" == randy.service ]]; then echo loaded; else echo not-found; fi ;;
      *:LoadState) echo not-found ;;
      *) exit 2 ;;
    esac ;;
  --failed) if [[ "$FAILURE" == failed-unit ]]; then echo 'unexpected.service loaded failed failed'; fi ;;
  --no-block) echo poweroff >> "$POWEROFF_LOG" ;;
  list-jobs) : ;;
  *) exit 2 ;;
esac
''')
            systemctl.chmod(0o755)
            journal = work / 'journalctl'
            journal.write_text('#!/bin/bash\necho synthetic-guest-journal\n')
            journal.chmod(0o755)
            result = subprocess.run(
                ['bash', str(script)], capture_output=True, text=True, timeout=10,
                env=dict(os.environ, PATH=str(work) + os.pathsep + os.environ['PATH'],
                         FAILURE=failure, POWEROFF_LOG=str(work / 'poweroff')))
            return result, (work / 'serial.log').read_text(), (work / 'poweroff').read_text()

    def test_guest_passes_without_wifi_hardware_or_optional_alpha2_units(self):
        result, log, poweroff = self.guest()
        self.assertEqual(result.returncode, 0, log + result.stderr)
        self.assertIn('RANDY_VM_SMOKE_PASS', log)
        self.assertNotIn('RANDY_VM_SMOKE_FAIL', log)
        self.assertIn('poweroff', poweroff)

    def test_guest_rejects_boot_service_and_firstboot_regressions(self):
        for failure in ['hostname', 'firstboot-flag', 'firstboot-active',
                        'NetworkManager.service', 'avahi-daemon.service', 'ssh.service',
                        'multi-user.target', 'disabled-importer', 'importer-blocked',
                        'importer-failed', 'failed-unit', 'randy.service']:
            with self.subTest(failure=failure):
                result, log, poweroff = self.guest(failure)
                self.assertNotEqual(result.returncode, 0, log)
                self.assertIn('RANDY_VM_SMOKE_FAIL', log)
                self.assertNotIn('RANDY_VM_SMOKE_PASS', log)
                self.assertIn('synthetic-guest-journal', log)
                self.assertIn('poweroff', poweroff)

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


if __name__ == '__main__':
    unittest.main()
