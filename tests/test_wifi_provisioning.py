"""Hardware-free checks. All credentials here are generated/synthetic test data."""
import configparser
import contextlib
import hashlib
import importlib.util
import io
import json
import types
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

# Importing the installed script must not create bytecode in mkosi.extra.
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'mkosi.extra/usr/lib/randy'))
SPEC = importlib.util.spec_from_file_location('wifi_import', ROOT / 'mkosi.extra/usr/lib/randy/wifi-import.py')
wifi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wifi)
WIFI_AVAILABLE = wifi.wifi_available


def ini(path):
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    config.read(path)
    return config


class ParserTests(unittest.TestCase):
    def test_missing_or_empty_required_fields(self):
        for data in (b'', b'SSID=\nPASSWORD=\n', b'SSID=TEST_ONLY',
                     b'PASSWORD=TEST_ONLY_NOT_A_SECRET', b'SSID=TEST_ONLY\nPASSWORD='):
            with self.subTest(data=data), self.assertRaises(wifi.ProvisionError):
                wifi.parse_config(data)

    def test_preserves_spaces_equals_punctuation_and_crlf(self):
        ssid = ' TEST_ONLY ;=é\\ '
        password = ' TEST_ONLY = !;#\\$`\" '
        data = f'\ufeffSSID={ssid}\r\nPASSWORD={password}\r\nCOUNTRY=sg\r\n'.encode()
        self.assertEqual(wifi.parse_config(data), (ssid.encode(), password, 'SG'))

    def test_rejects_ambiguous_or_invalid_data(self):
        valid = b'SSID=TEST_ONLY\nPASSWORD=TEST_ONLY_NOT_A_SECRET\n'
        for data in (valid + b'SSID=duplicate', valid + b'UNKNOWN=x', valid + b'COUNTRY=SG;id',
                     valid + b'PASSWORD=x', b'SSID=x\nPASSWORD=short', b'SSID=x\nPASSWORD=x\x00',
                     b'\xff', b'x' * 8193, b'SSID=' + b'x' * 33 + b'\nPASSWORD=TEST_ONLY_PASSWORD'):
            with self.subTest(size=len(data)), self.assertRaises(wifi.ProvisionError):
                wifi.parse_config(data)

    def test_shell_contents_are_inert_data(self):
        with tempfile.TemporaryDirectory() as work:
            marker = Path(work) / 'marker'
            # Password commands may exceed the WPA length, so use a short relative marker.
            data = b'SSID=$(touch marker)\nPASSWORD=`touch marker`\n'
            with mock.patch('os.system', side_effect=AssertionError('shell execution')), \
                 mock.patch.object(subprocess, 'run', side_effect=AssertionError('process execution')):
                ssid, password, _ = wifi.parse_config(data)
                wifi.keyfile(ssid, password)
            self.assertEqual(ssid, b'$(touch marker)')
            self.assertFalse(marker.exists())
            self.assertFalse((Path.cwd() / 'marker').exists())


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.source = Path(self.work.name) / 'randy-wifi.txt'
        self.connections = Path(self.work.name) / 'connections'
        self.password = secrets.token_urlsafe(20)
        self.data = f'SSID=TEST_ONLY = ;\nPASSWORD={self.password}\n'.encode()
        self.source.write_bytes(self.data)
        self.calls = []
        self.available = mock.patch.object(wifi, 'wifi_available', return_value=True).start()
        mock.patch('wifi_diagnostics.probe', return_value=(127, '')).start()
        mock.patch('wifi_diagnostics.ssh_banner', return_value='unavailable').start()
        self.addCleanup(mock.patch.stopall)

    def run_import(self, reject=None, change_source=False):
        def command(*args, **kwargs):
            self.calls.append((args, kwargs))
            if args[0] == 'nmcli' and 'up' in args and change_source:
                self.source.write_bytes(b'CHANGED_BY_USER')
            return reject not in args if reject is not None else True
        output = io.StringIO()
        with mock.patch.object(wifi, 'command', side_effect=command), contextlib.redirect_stdout(output):
            result = wifi.provision(self.source, self.connections)
        self.assertNotIn(self.password, output.getvalue())
        self.assertNotIn(self.password, repr(self.calls))
        return result

    def test_success_removes_input_and_stores_private_profile(self):
        self.assertTrue(self.run_import())
        self.assertFalse(self.source.exists())
        profile = self.connections / wifi.PROFILE_NAME
        self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o600)
        self.assertEqual(profile.stat().st_uid, os.geteuid())
        self.assertEqual(stat.S_IMODE(self.connections.stat().st_mode), 0o700)
        config = ini(profile)
        self.assertEqual(config['connection']['autoconnect'], 'false')
        # Enabled only by the successful nmcli modify command (mocked in this test).
        self.assertTrue(any('modify' in args and args[-1] == 'yes' for args, _ in self.calls))
        self.assertEqual(config['connection']['autoconnect-retries'], '1')
        self.assertEqual(config['wifi-security']['key-mgmt'], 'wpa-psk')
        expected = hashlib.pbkdf2_hmac('sha1', self.password.encode(), b'TEST_ONLY = ;', 4096, 32).hex()
        self.assertEqual(config['wifi-security']['psk'], expected)
        self.assertNotIn(self.password, profile.read_text())
        self.assertEqual(config['ipv4']['method'], 'auto')
        self.assertEqual(config['ipv6']['method'], 'auto')
        self.assertEqual(len(list(self.connections.iterdir())), 1)

    def test_failures_retain_input_and_do_not_enable_autoconnect(self):
        for reject in ('load', 'up', 'modify'):
            with self.subTest(stage=reject):
                self.calls.clear()
                with self.assertRaises(wifi.ProvisionError):
                    self.run_import(reject=reject)
                self.assertEqual(self.source.read_bytes(), self.data)
                self.assertEqual(ini(self.connections / wifi.PROFILE_NAME)['connection']['autoconnect'], 'false')
                self.assertLessEqual(sum('up' in args for args, _ in self.calls), 1)

    def test_failed_activation_cancels_pending_attempt(self):
        with self.assertRaises(wifi.ProvisionError):
            self.run_import(reject='up')
        self.assertTrue(any('down' in args for args, _ in self.calls))

    def test_missing_input_is_noop(self):
        self.source.unlink()
        self.assertFalse(self.run_import())
        self.assertEqual(self.calls, [])
        self.available.assert_not_called()

    def test_no_adapter_retains_file_without_import(self):
        self.available.return_value = False
        with self.assertRaises(wifi.ProvisionError):
            self.run_import()
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.connections.exists())

    def test_invalid_input_is_retained_without_commands(self):
        self.source.write_bytes(b'SSID=TEST_ONLY')
        with self.assertRaises(wifi.ProvisionError):
            self.run_import()
        self.assertTrue(self.source.exists())
        self.assertEqual(self.calls, [])

    def test_replaced_input_is_not_deleted(self):
        with self.assertRaises(wifi.ProvisionError):
            self.run_import(change_source=True)
        self.assertEqual(self.source.read_bytes(), b'CHANGED_BY_USER')

    def test_command_suppresses_output_and_bounds_wait(self):
        with mock.patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('nmcli', 2)) as run:
            self.assertFalse(wifi.command('nmcli', 'connection', 'reload', timeout=2))
        self.assertEqual(run.call_args.kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs['timeout'], 2)
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_adapter_wait_is_bounded(self):
        with mock.patch.object(Path, 'glob', return_value=[]), mock.patch.object(wifi.time, 'sleep') as sleep:
            self.assertFalse(WIFI_AVAILABLE())
        self.assertEqual(sleep.call_count, 15)


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.source = Path(self.work.name) / 'randy-wifi.txt'
        self.connections = Path(self.work.name) / 'connections'
        self.password = secrets.token_urlsafe(20)
        self.ssid = 'TEST_ONLY = ;'
        self.psk = hashlib.pbkdf2_hmac('sha1', self.password.encode(), self.ssid.encode(), 4096, 32).hex()
        self.source.write_text(f'SSID={self.ssid}\nPASSWORD={self.password}\n')
        self.diag = sys.modules['wifi_diagnostics']
        mock.patch.object(wifi, 'wifi_available', return_value=True).start()
        mock.patch.object(self.diag, 'probe', return_value=(127, '')).start()
        mock.patch.object(self.diag, 'ssh_banner', return_value='unavailable').start()
        mock.patch.object(self.diag, 'hardware', return_value=[]).start()
        self.addCleanup(mock.patch.stopall)

    def read_report(self):
        text = (self.source.parent / self.diag.NAME).read_text()
        self.assertNotIn(self.password, text)
        self.assertNotIn(self.psk, text)
        fields = {}
        for line in text.splitlines()[1:]:
            key, value = line.split(': ', 1)
            fields[key] = json.loads(value)
        return fields

    def test_failure_creates_diagnostic_file_and_retains_input(self):
        with mock.patch.object(wifi, 'command', return_value=False), self.assertRaises(wifi.ProvisionError):
            wifi.provision(self.source, self.connections)
        fields = self.read_report()
        self.assertEqual(fields['provisioning_result'], 'failed')
        self.assertEqual(fields['parsing'], 'succeeded')
        self.assertTrue(fields['provisioning_file_found'])
        self.assertTrue(fields['profile_created'])
        self.assertFalse(fields['profile_loaded'])
        self.assertEqual(fields['requested_ssid'], self.ssid)
        self.assertTrue(self.source.exists())

    def test_missing_wifi_device_is_reported(self):
        with mock.patch.object(wifi, 'wifi_available', return_value=False), self.assertRaises(wifi.ProvisionError):
            wifi.provision(self.source, self.connections)
        fields = self.read_report()
        self.assertEqual(fields['device_detection'], 'no-wifi-interface')
        self.assertFalse(fields['profile_created'])
        self.assertIn('No Wi-Fi interface', fields['reason'])
        self.assertTrue(self.source.exists())

    def test_cached_requested_ssid_visibility_is_recorded_without_other_networks(self):
        for visible in (False, True):
            with self.subTest(visible=visible):
                def probe(args, **kwargs):
                    if 'SSID' in args:
                        self.assertIn('--rescan', args)
                        self.assertEqual(args[-1], 'no')
                        return 0, 'UNRELATED_TEST_NETWORK\n' + (self.ssid + '\n' if visible else '')
                    if 'DEVICE,TYPE,STATE' in args:
                        return 0, 'wlan0:wifi:disconnected\n'
                    return 127, ''
                with mock.patch.object(self.diag, 'probe', side_effect=probe), \
                     mock.patch.object(wifi, 'command', return_value=False), self.assertRaises(wifi.ProvisionError):
                    wifi.provision(self.source, self.connections)
                fields = self.read_report()
                self.assertEqual(fields['requested_ssid_visible'], 'yes' if visible else 'no')
                self.assertEqual(fields['wifi_interfaces'], ['wlan0'])
                self.assertNotIn('UNRELATED_TEST_NETWORK', (self.source.parent / self.diag.NAME).read_text())

    def test_activation_error_is_classified_and_never_leaks_output(self):
        token = 'SYNTHETIC_TOKEN_NOT_FOR_REPORT'
        def run(args, **kwargs):
            failed = 'up' in args
            text = f'Secrets were required but not provided PASSWORD={self.password} psk={self.psk} token={token}'
            return types.SimpleNamespace(returncode=4 if failed else 0, stdout=text, stderr=text)
        output = io.StringIO()
        with mock.patch.object(wifi.subprocess, 'run', side_effect=run), contextlib.redirect_stdout(output), \
             self.assertRaises(wifi.ProvisionError):
            wifi.provision(self.source, self.connections)
        fields = self.read_report()
        self.assertEqual(fields['activation'], 'failed')
        self.assertEqual(fields['networkmanager_error'],
                         {'category': 'authentication-or-secrets-unavailable', 'exit_code': 4})
        text = (self.source.parent / self.diag.NAME).read_text() + output.getvalue()
        for forbidden in (self.password, self.psk, token):
            self.assertNotIn(forbidden, text)
        self.assertTrue(self.source.exists())

    def test_success_is_reported_and_existing_deletion_behavior_remains(self):
        with mock.patch.object(wifi, 'command', return_value=True):
            self.assertTrue(wifi.provision(self.source, self.connections))
        fields = self.read_report()
        self.assertEqual(fields['activation'], 'succeeded')
        self.assertEqual(fields['provisioning_result'], 'success')
        self.assertEqual(fields['provisioning_service_result'], 'success')
        self.assertTrue(fields['profile_loaded'])
        self.assertTrue(fields['autoconnect_persisted'])
        self.assertTrue(fields['input_removed'])
        self.assertFalse(self.source.exists())

    def test_network_snapshot_is_structured_and_excludes_unknown_fields(self):
        token = 'SYNTHETIC_QUERY_TOKEN'
        def probe(args, **kwargs):
            if args[0] == 'systemctl':
                return 0, 'active\n'
            if 'radio' in args:
                return 0, 'enabled\n'
            if 'DEVICE,TYPE,STATE' in args:
                return 0, 'wlan0:wifi:disconnected\n'
            if 'GENERAL.STATE,GENERAL.REASON' in args:
                return 0, f'30 (disconnected)\n7 ({token})\n'
            if 'SSID' in args:
                return 0, self.ssid + '\n'
            if args[:3] == ['ip', '-j', 'address']:
                return 0, json.dumps([{'ifname': 'wlan0', 'addr_info': [{'local': '192.0.2.2'}], 'token': token}])
            if args[0] == 'ip':
                return 0, json.dumps([{'dev': 'wlan0', 'gateway': '192.0.2.1', 'token': token}])
            if args[0] == 'dmesg':
                return 0, f'iwlwifi: firmware failed with error -2 token={token}'
            return 127, ''
        with mock.patch.object(self.diag, 'probe', side_effect=probe), \
             mock.patch.object(wifi, 'command', return_value=False), self.assertRaises(wifi.ProvisionError):
            wifi.provision(self.source, self.connections)
        fields = self.read_report()
        self.assertEqual(fields['networkmanager_service'], 'active')
        self.assertEqual(fields['avahi_service'], 'active')
        self.assertEqual(fields['wifi_radio'], 'enabled')
        self.assertEqual(fields['ip_addresses'], [{'interface': 'wlan0', 'address': '192.0.2.2'}])
        self.assertEqual(fields['default_routes'], [{'interface': 'wlan0', 'gateway': '192.0.2.1'}])
        self.assertEqual(fields['nm_device_reason_codes'], {'wlan0': [30, 7]})
        self.assertNotIn(token, (self.source.parent / self.diag.NAME).read_text())

    def test_parse_failure_is_reported_without_password(self):
        self.source.write_text(f'SSID={self.ssid}\nPASSWORD={self.password}\nUNKNOWN=bad\n')
        with self.assertRaises(wifi.ProvisionError):
            wifi.provision(self.source, self.connections)
        fields = self.read_report()
        self.assertEqual(fields['parsing'], 'failed')
        self.assertFalse(fields['profile_created'])
        self.assertTrue(self.source.exists())

    def test_firmware_text_is_reduced_to_fixed_events(self):
        events = self.diag.firmware_events(f'iwlwifi: firmware load failed with error -2 token={self.password} psk={self.psk}')
        self.assertEqual(events, [{'event': 'firmware-load-failed', 'driver': 'iwlwifi', 'errno': -2}])

    def test_finalizer_creates_report_if_importer_never_wrote_checkpoint(self):
        with mock.patch.object(self.diag.os.path, 'ismount', return_value=True), \
             mock.patch.dict(os.environ, {'SERVICE_RESULT': 'exit-code'}):
            self.diag.finalize(self.source)
        text = (self.source.parent / self.diag.NAME).read_text()
        self.assertIn('no-importer-checkpoint', text)
        self.assertIn(self.ssid, text)
        self.assertIn('systemd_service_result: exit-code', text)
        self.assertNotIn(self.password, text)
        self.assertNotIn(self.psk, text)
        self.assertTrue(self.source.exists())

    def test_timeout_finalizer_records_actual_systemd_result(self):
        report = wifi.Report(self.source)
        report.input(self.source.read_bytes())
        report.fields['activation'] = 'in-progress'
        report.write()
        with mock.patch.object(self.diag.os.path, 'ismount', return_value=True), \
             mock.patch.dict(os.environ, {'SERVICE_RESULT': 'timeout'}):
            self.diag.finalize(self.source)
        text = (self.source.parent / self.diag.NAME).read_text()
        self.assertIn('systemd_service_result: timeout', text)
        self.assertNotIn(self.password, text)
        self.assertTrue(self.source.exists())


class LayoutTests(unittest.TestCase):
    def test_partition_and_empty_config(self):
        parts = sorted((ROOT / 'mkosi.repart').glob('*.conf'))
        self.assertEqual([p.name for p in parts], ['00-esp.conf', '10-config.conf', '20-root.conf'])
        config = ini(parts[1])['Partition']
        self.assertEqual(config['Label'], 'CONFIG')
        self.assertEqual(config['Format'], 'vfat')
        self.assertEqual(config['SizeMinBytes'], '64M')
        self.assertEqual(config['SizeMaxBytes'], '64M')
        self.assertEqual(config['Type'], 'ebd0a0a2-b9e5-4433-87c0-68b6b72699c7')
        self.assertNotIn('CopyFiles', config)
        self.assertNotIn('MountPoint', config)
        self.assertFalse(list((ROOT / 'mkosi.extra').rglob('randy-wifi.txt')))

    def test_esp_copies_both_kernel_payload_and_bootloader(self):
        # Repart CopyFiles is repeatable: ConfigParser would lose one directive.
        # mkosi v27.1 stages the UKI at /boot/EFI/Linux, the loader under /efi.
        lines = (ROOT / 'mkosi.repart/00-esp.conf').read_text().splitlines()
        copies = [line.split('=', 1)[1].strip() for line in lines
                  if line.strip().startswith('CopyFiles=')]
        self.assertIn('/boot:/', copies, 'UKIs must be copied to the ESP root')
        self.assertIn('/efi:/', copies, 'systemd-boot must be copied to the ESP root')
        self.assertLess(copies.index('/boot:/'), copies.index('/efi:/'))

    def test_unsigned_uki_generation_is_required(self):
        config = ini(ROOT / 'mkosi.conf')
        self.assertEqual(config['Content']['Bootable'], 'yes')
        self.assertEqual(config['Content']['Bootloader'], 'systemd-boot')
        self.assertEqual(config['Content']['UnifiedKernelImages'], 'unsigned')
        self.assertEqual(config['Validation']['SecureBoot'], 'no')
        packages = config['Content']['Packages'].split()
        self.assertIn('linux-image-amd64', packages)
        self.assertIn('systemd-boot-efi', packages)
        self.assertEqual(config['Build']['ToolsTree'], 'yes')
        self.assertEqual(config['Build']['ToolsTreeDistribution'], 'debian')
        self.assertEqual(config['Build']['ToolsTreeRelease'], 'trixie')

    def test_optional_mount_and_service(self):
        base = ROOT / 'mkosi.extra/etc/systemd/system'
        service = ini(base / 'randy-wifi-import.service')
        mount = ini(base / r'run-randy\x2dconfig.mount')
        self.assertNotIn('Requires', service['Unit'])
        self.assertIn(r'run-randy\x2dconfig.mount', service['Unit']['Wants'])
        self.assertEqual(service['Service']['Type'], 'oneshot')
        self.assertEqual(service['Service']['Restart'], 'no')
        self.assertEqual(service['Service']['TimeoutStartSec'], '120s')
        self.assertEqual(service['Service']['ExecStopPost'], '/usr/bin/python3 /usr/lib/randy/wifi_diagnostics.py')
        self.assertEqual(service['Service']['TimeoutStopSec'], '15s')
        self.assertEqual(service['Install']['WantedBy'], 'multi-user.target')
        self.assertEqual(mount['Mount']['What'], '/dev/disk/by-label/CONFIG')
        self.assertEqual(mount['Mount']['Where'], '/run/randy-config')
        self.assertEqual(mount['Unit']['JobTimeoutSec'], '10s')
        self.assertIn('umask=0077', mount['Mount']['Options'])
        preset = (ROOT / 'mkosi.extra/usr/lib/systemd/system-preset/00-randy.preset').read_text()
        self.assertIn('enable randy-wifi-import.service', preset)


class HelperTests(unittest.TestCase):
    def test_helper_preserves_literal_input_without_echo_or_extra_files(self):
        with tempfile.TemporaryDirectory() as work:
            base = Path(work)
            volume = base / 'CONFIG'
            volume.mkdir()
            mount = base / 'mount-stub'
            mount.write_text('#!/bin/bash\nprintf "%s\\n" "test-device on ' + str(volume) + ' (msdos, local)"\n')
            mount.chmod(0o700)
            # Substitute only mount discovery, so no real removable media is required.
            helper = base / 'helper.sh'
            helper.write_text((ROOT / 'scripts/configure-wifi-macos.sh').read_text().replace(
                '/sbin/mount', '"' + str(mount) + '"'))
            password = secrets.token_urlsafe(12) + ' =;\\$`!'
            ssid = ' TEST_ONLY = ; '
            result = subprocess.run(['/bin/bash', str(helper), str(volume)],
                                    input=f'{ssid}\n{password}\nSG\n', text=True,
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(password, result.stdout + result.stderr)
            self.assertEqual(wifi.parse_config((volume / 'randy-wifi.txt').read_bytes()),
                             (ssid.encode(), password, 'SG'))
            self.assertEqual([p.name for p in volume.iterdir()], ['randy-wifi.txt'])

    def test_unmounted_directory_is_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as work:
            result = subprocess.run(['/bin/bash', str(ROOT / 'scripts/configure-wifi-macos.sh'), work],
                                    input='', text=True, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(Path(work).iterdir()), [])


class LibnmTests(unittest.TestCase):
    def test_keyfiles_are_accepted_by_networkmanager(self):
        try:
            import gi
            gi.require_version('NM', '1.0')
            from gi.repository import GLib, NM
        except (ImportError, ValueError):
            self.skipTest('libnm introspection is installed by Linux CI; unavailable locally')
        for ssid in (b'TEST_ONLY = ;\\ ', b'1;2;3;', 'TEST_ONLY é'.encode()):
            with self.subTest(ssid=ssid):
                password = secrets.token_urlsafe(20)
                text = wifi.keyfile(ssid, password)
                key = GLib.KeyFile.new()
                key.load_from_data(text, len(text), GLib.KeyFileFlags.NONE)
                connection = NM.keyfile_read(key, '/', NM.KeyfileHandlerFlags.NONE, None)
                self.assertTrue(connection.verify())
                self.assertEqual(connection.get_setting_wireless().get_ssid().get_data(), ssid)
                self.assertEqual(connection.get_setting_wireless_security().get_psk(),
                                 hashlib.pbkdf2_hmac('sha1', password.encode(), ssid, 4096, 32).hex())


if __name__ == '__main__':
    unittest.main()
