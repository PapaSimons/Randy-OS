"""Exercise the actual validator helpers without Linux mounts or a disk image."""
import json
import configparser
import shlex
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/validate-image.sh'


class ImageValidationTests(unittest.TestCase):
    def call(self, *args, data=None, env=None):
        return subprocess.run(
            ['/bin/bash', '-c', 'source "$1"; shift; "$@"', '--', str(SCRIPT), *args],
            input=data, text=True, capture_output=True, env=env, timeout=10)

    def assert_diagnostic(self, result, *details):
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        for detail in ('ERROR:', 'Expected:', 'Actual:', *details):
            self.assertIn(detail, output)

    def table(self):
        return {'partitiontable': {
            'label': 'gpt', 'unit': 'sectors', 'sectorsize': 512,
            'partitions': [
                {'name': 'EFI', 'type': 'c12a7328-f81f-11d2-ba4b-00a0c93ec93b', 'size': 1048576},
                {'name': 'CONFIG', 'type': 'ebd0a0a2-b9e5-4433-87c0-68b6b72699c7', 'size': 131072},
                {'name': 'RANDY', 'type': '4f68bce3-e8cd-4db1-96e7-fbcaf984b709', 'size': 16777216},
            ]}}

    def validate_table(self, table):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'table.json'
            path.write_text(json.dumps(table))
            return self.call('validate_partitions', str(path))

    def test_vfat_sector_settings_are_explicit_mkosi_configuration(self):
        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = str
        config.read(ROOT / 'mkosi.conf')
        self.assertEqual(config['Output']['SectorSize'], '512')
        # Model the documented shell-style parsing of mkosi Environment= values.
        settings = dict(item.split('=', 1) for item in shlex.split(config['Build']['Environment']))
        self.assertEqual(settings['SYSTEMD_REPART_MKFS_OPTIONS_VFAT'], '-S 512')

    def test_unattended_firstboot_defaults_preserve_locked_root(self):
        config = configparser.ConfigParser(interpolation=None)
        config.read(ROOT / 'mkosi.conf')
        content = config['Content']
        self.assertEqual(shlex.split(content['KernelCommandLine']), ['systemd.firstboot=no'])
        for key, value in [('Hostname', 'randy'), ('Locale', 'C.UTF-8'),
                           ('Keymap', 'us'), ('Timezone', 'UTC'),
                           ('RootPassword', 'hashed:!'), ('Autologin', 'no')]:
            self.assertEqual(content[key], value)
        self.assertEqual((ROOT / 'mkosi.extra/etc/hostname').read_text().strip(), 'randy')
        self.assertNotIn('/etc/localtime', content['RemoveFiles'].split())
        self.assertNotIn('/etc/timezone', content['RemoveFiles'].split())

    def test_custom_units_do_not_invoke_firstboot(self):
        for path in (ROOT / 'mkosi.extra').rglob('*'):
            if path.is_file() and ('systemd/system' in path.as_posix()):
                active = '\n'.join(line for line in path.read_text().splitlines()
                                   if not line.lstrip().startswith(('#', ';')))
                self.assertNotIn('systemd-firstboot', active, str(path))

    def test_embedded_command_line_requires_exact_flag_without_global_override(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'cmdline'
            for cmdline, valid in [
                ('root=PARTUUID=test systemd.firstboot=no rw\0', True),
                ('root=PARTUUID=test rw', False),
                ('systemd.firstboot=nope', False),
                ('systemd.firstboot=no systemd.firstboot=yes', False),
                ('systemd.firstboot=no systemd.condition_first_boot=no', False),
            ]:
                with self.subTest(cmdline=cmdline):
                    path.write_bytes(cmdline.encode())
                    result = self.call('validate_kernel_command_line', str(path))
                    if valid:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    else:
                        self.assert_diagnostic(result, 'UKI firstboot command line')

    def test_both_fat_partitions_are_checked_in_the_image_validator(self):
        text = SCRIPT.read_text()
        self.assertIn('validate_fat_sector_size "${loop}p1" EFI', text)
        self.assertIn('validate_fat_sector_size "${loop}p2" CONFIG', text)

    def test_fat_bpb_accepts_512_for_both_partitions(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'fat-sector.bin'
            sector = bytearray(512)
            sector[:3] = bytes.fromhex('eb5890')
            sector[3:11] = b'mkfs.fat'
            sector[11:13] = (512).to_bytes(2, 'little')
            path.write_bytes(sector)
            for label in ('EFI', 'CONFIG'):
                result = self.call('validate_fat_sector_size', str(path), label)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('Actual: 512', result.stdout)

    def test_fat_bpb_rejects_1024_2048_and_reported_4096_regression(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'fat-sector.bin'
            for size in (1024, 2048, 4096):
                sector = bytearray(512)
                sector[:3] = bytes.fromhex('eb5890')
                sector[3:11] = b'mkfs.fat'
                sector[11:13] = size.to_bytes(2, 'little')
                path.write_bytes(sector)
                for label in ('EFI', 'CONFIG'):
                    with self.subTest(size=size, partition=label):
                        result = self.call('validate_fat_sector_size', str(path), label)
                        self.assert_diagnostic(result, label, 'Expected: 512', f'Actual: {size}')

    def test_fat_bpb_missing_and_truncated_data_fail_diagnostically(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'fat-sector.bin'
            self.assert_diagnostic(self.call('validate_fat_sector_size', str(path), 'CONFIG'), 'BPB read failed')
            path.write_bytes(bytes(12))
            self.assert_diagnostic(self.call('validate_fat_sector_size', str(path), 'EFI'), 'truncated', 'Actual: 1 bytes')

    def test_valid_partition_table(self):
        result = self.validate_table(self.table())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('partition 3 size in bytes', result.stdout)
        self.assertIn('8589934592', result.stdout)

    def test_partition_count_order_type_label_and_size_fail_diagnostically(self):
        cases = [
            ('count', lambda parts: parts.pop(), 'partition count'),
            ('order', lambda parts: parts.reverse(), 'partition 1 GPT type'),
            ('guid', lambda parts: parts[1].update(type='invalid-guid'), 'invalid-guid'),
            ('label', lambda parts: parts[1].update(name='WRONG'), 'WRONG'),
            ('size', lambda parts: parts[1].update(size=1), 'partition 2 size in bytes'),
        ]
        for name, change, detail in cases:
            with self.subTest(check=name):
                table = self.table()
                change(table['partitiontable']['partitions'])
                self.assert_diagnostic(self.validate_table(table), detail)

    def test_sector_size_is_used_instead_of_assuming_512_bytes(self):
        table = self.table()
        table['partitiontable']['sectorsize'] = 4096
        for part in table['partitiontable']['partitions']:
            part['size'] //= 8
        result = self.validate_table(table)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_malformed_partition_metadata_is_diagnostic(self):
        self.assert_diagnostic(self.validate_table({}), 'cannot inspect GPT metadata')

    def test_empty_actual_value_is_visible_and_fails(self):
        result = self.call('equal', 'CONFIG FAT version', 'FAT32', '')
        self.assert_diagnostic(result, 'CONFIG FAT version', 'FAT32', '<empty>')

    def test_failed_command_output_and_status_are_retained(self):
        result = self.call('capture', 'loop setup failure', 'python3', '-c',
                           'import sys; print("synthetic loop error", file=sys.stderr); sys.exit(7)')
        self.assert_diagnostic(result, 'loop setup failure', 'synthetic loop error', 'exit status 7')

    def test_missing_file_is_diagnostic(self):
        result = self.call('check_file', 'EFI loader', '/nonexistent-randy-test/BOOTX64.EFI', '-s')
        self.assert_diagnostic(result, 'EFI loader', 'BOOTX64.EFI')

    def test_blkid_probes_directly_for_version_and_labels(self):
        with tempfile.TemporaryDirectory() as work:
            tool = Path(work) / 'blkid'
            tool.write_text('''#!/bin/bash
# Model cache-based blkid: it can succeed with no VERSION value.
if [[ "$1" != --probe ]]; then exit 0; fi
case "$3" in
  VERSION) printf 'FAT32\\n' ;;
  LABEL) printf 'CONFIG\\n' ;;
esac
''')
            tool.chmod(0o700)
            env = dict(os.environ, PATH=work + os.pathsep + os.environ['PATH'])
            for field, expected in [('VERSION', 'FAT32'), ('LABEL', 'CONFIG')]:
                result = self.call('probe_equal', '/dev/loop-test', field, expected, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('--probe', result.stdout)
            self.assert_diagnostic(self.call('probe_equal', '/dev/loop-test', 'LABEL', 'EFI', env=env),
                                   'EFI', 'CONFIG')

    def test_forbidden_package_is_not_hidden_by_a_pipeline(self):
        result = self.call('check_packages', data='pipewire\n' + 'synthetic-base-package\n' * 10000)
        self.assert_diagnostic(result, 'pipewire', 'excluded runtime packages installed')
        result = self.call('check_packages', data='systemd\nnetwork-manager\nopenssh-server\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Actual: none', result.stdout)

    def test_tee_keeps_failed_validation_status(self):
        with tempfile.TemporaryDirectory() as work:
            log = Path(work) / 'validation.log'
            result = subprocess.run(
                ['/bin/bash', '-c', 'source "$1"; equal "CONFIG label" CONFIG WRONG 2>&1 | tee "$2"',
                 '--', str(SCRIPT), str(log)], capture_output=True, text=True, timeout=10)
            self.assert_diagnostic(result, 'CONFIG label', 'WRONG')
            self.assertIn('ERROR:', log.read_text())

    def test_cleanup_preserves_validation_failure(self):
        with tempfile.TemporaryDirectory() as parent:
            work = Path(parent) / 'inspection'
            work.mkdir()
            result = subprocess.run(
                ['/bin/bash', '-c',
                 'source "$1"; work=$2; loop=/dev/loop9999; '
                 'mountpoint() { return 1; }; losetup() { return 0; }; '
                 'trap cleanup EXIT; exit 7', '--', str(SCRIPT), str(work)],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertFalse(work.exists())

    def test_cleanup_failure_is_diagnostic_and_does_not_remove_mounted_tree(self):
        with tempfile.TemporaryDirectory() as parent:
            work = Path(parent) / 'inspection'
            work.mkdir()
            result = subprocess.run(
                ['/bin/bash', '-c',
                 'source "$1"; work=$2; loop=/dev/loop9999; '
                 'mountpoint() { return 0; }; '
                 'umount() { printf "synthetic unmount error"; return 1; }; '
                 'trap cleanup EXIT; exit 0', '--', str(SCRIPT), str(work)],
                capture_output=True, text=True, timeout=10)
            self.assert_diagnostic(result, 'synthetic unmount error', 'leaving mounted workspace')
            self.assertTrue(work.exists())


if __name__ == '__main__':
    unittest.main()
