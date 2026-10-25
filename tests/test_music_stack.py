"""Small appliance integration checks, not application playback regressions."""
import configparser
import hashlib
import importlib.util
import importlib.machinery
import contextlib
import concurrent.futures
import threading
import io
import json
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


identity = load('identity', ROOT / 'mkosi.extra/usr/lib/randy/player-id.py')
builder = load('builder', ROOT / 'scripts/build-stack.py')
diagnostics = load('diagnostics', ROOT / 'mkosi.extra/usr/bin/randy-diagnostics')


def unit(name):
    result = configparser.ConfigParser(interpolation=None, strict=False)
    result.optionxform = str
    result.read(ROOT / ('mkosi.extra/usr/lib/systemd/system/' + name + '.service'))
    return result


class IdentityTests(unittest.TestCase):
    def test_identity_persists_and_is_locally_administered_unicast(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            value = identity.ensure_identity(directory)
            self.assertEqual(identity.ensure_identity(directory), value)
            self.assertRegex(value, r'^[0-9a-f]{2}(:[0-9a-f]{2}){5}$')
            self.assertEqual(int(value[:2], 16) & 3, 2)
            self.assertEqual(stat.S_IMODE((directory / 'player.env').stat().st_mode), 0o644)
            self.assertEqual(len(list(directory.iterdir())), 1)

    def test_simultaneous_initializers_publish_and_return_only_one_identity(self):
        # Force all callers past the missing-file check before any can publish.
        barrier = threading.Barrier(8)
        original = identity.secrets.token_bytes
        def candidate(length):
            barrier.wait(timeout=5)
            return original(length)
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            with mock.patch.object(identity.secrets, 'token_bytes', side_effect=candidate), \
                 concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                ids = list(executor.map(lambda _: identity.ensure_identity(directory), range(8)))
            self.assertEqual(len(set(ids)), 1)
            self.assertEqual((directory / 'player.env').read_text(), f'LMS_PLAYER_ID={ids[0]}\n')
            self.assertEqual([p.name for p in directory.iterdir()], ['player.env'])

    def test_later_boot_never_generates_or_rewrites_a_valid_existing_identity(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            expected = identity.ensure_identity(directory)
            target = directory / 'player.env'
            before = target.stat()
            with mock.patch.object(identity.secrets, 'token_bytes', side_effect=AssertionError('must reuse existing ID')):
                for _ in range(3):
                    self.assertEqual(identity.ensure_identity(directory), expected)
            after = target.stat()
            self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_independent_installations_have_distinct_ids(self):
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            self.assertNotEqual(identity.ensure_identity(Path(left)), identity.ensure_identity(Path(right)))

    def test_invalid_existing_identity_is_never_silently_replaced(self):
        with tempfile.TemporaryDirectory() as work:
            target = Path(work) / 'player.env'
            target.write_text('invalid\n')
            with self.assertRaises(ValueError):
                identity.ensure_identity(Path(work))
            self.assertEqual(target.read_text(), 'invalid\n')


class StackTests(unittest.TestCase):
    def test_all_source_inputs_are_immutable_and_checksum_pinned(self):
        pins = json.loads((ROOT / 'stack.lock.json').read_text())
        for name, pin in pins.items():
            with self.subTest(component=name):
                self.assertRegex(pin['sha256'], r'^[0-9a-f]{64}$')
                self.assertTrue(pin['url'].startswith('https://'))
                self.assertNotIn('latest', pin['url'])
                self.assertIn(pin.get('commit', pin.get('version')), pin['url'])
        self.assertEqual(pins['randy']['commit'], '92dc6e07448934fc211d7e3d6dd2333383216192')
        self.assertEqual(pins['node']['version'].split('.')[0], '22')
        self.assertEqual(pins['squeezelite']['version'], '2.0.0-1584')
        self.assertEqual(pins['squeezelite']['commit'], 'e1028f3cd058789c12b765d68b82e8b3b0b43a7b')

    def test_services_share_only_one_generated_player_identity(self):
        shared = '/var/lib/randy-device/player.env'
        initializer = unit('randy-player-id')
        self.assertEqual(initializer['Service']['Type'], 'oneshot')
        self.assertEqual(initializer['Service']['RemainAfterExit'], 'yes')
        self.assertEqual(initializer['Service']['ExecStart'], '/usr/bin/python3 /usr/lib/randy/player-id.py')
        for name in ('squeezelite', 'randy'):
            config = unit(name)
            self.assertIn(name + '.service', initializer['Unit']['Before'].split())
            self.assertIn('randy-player-id.service', config['Unit']['After'].split())
            self.assertEqual(config['Unit']['Requires'].split(), ['randy-player-id.service'])
            text = (ROOT / f'mkosi.extra/usr/lib/systemd/system/{name}.service').read_text()
            files = [line.split('=', 1)[1] for line in text.splitlines() if line.startswith('EnvironmentFile=')]
            self.assertEqual(files.count(shared), 1)
            self.assertNotIn('-' + shared, files)  # Missing identity must fail startup.
            self.assertFalse(any(line.startswith('Environment=') and 'LMS_PLAYER_ID=' in line
                                 for line in text.splitlines()))
        self.assertIn('-m ${LMS_PLAYER_ID}', unit('squeezelite')['Service']['ExecStart'])
        self.assertFalse((ROOT / 'mkosi.extra/var/lib/randy-device/player.env').exists())
        for name in ('mkosi.extra/etc/default/squeezelite', 'mkosi.extra/etc/randy/randy.env'):
            self.assertNotIn('LMS_PLAYER_ID=', (ROOT / name).read_text())

    def test_temporary_lms_failure_has_no_hard_dependency_or_stop_propagation(self):
        for name in ('lyrionmusicserver', 'squeezelite', 'randy'):
            config = unit(name)
            for directive in ('Requires', 'BindsTo', 'Requisite', 'PartOf'):
                deps = config['Unit'].get(directive, '').split()
                self.assertFalse(set(deps) & {'lyrionmusicserver.service', 'squeezelite.service', 'randy.service'})
            self.assertEqual(config['Service']['Restart'], 'on-failure')
            # With fast failures, the restart interval stays below the rate limit.
            interval = int(config['Unit']['StartLimitIntervalSec'])
            delay = int(config['Service']['RestartSec'].rstrip('s'))
            burst = int(config['Unit']['StartLimitBurst'])
            self.assertLess(interval // delay + 1, burst)
        for name in ('squeezelite', 'randy'):
            self.assertIn('lyrionmusicserver.service', unit(name)['Unit']['Wants'].split())
            self.assertIn('lyrionmusicserver.service', unit(name)['Unit']['After'].split())

    def test_service_supervision_and_unprivileged_users(self):
        presets = (ROOT / 'mkosi.extra/usr/lib/systemd/system-preset/00-randy.preset').read_text()
        for name, user in [('lyrionmusicserver', 'squeezeboxserver'), ('squeezelite', 'squeezelite'), ('randy', 'randy')]:
            with self.subTest(service=name):
                config = unit(name)
                self.assertEqual(config['Service']['User'], user)
                self.assertEqual(config['Service']['Type'], 'simple')
                self.assertEqual(config['Service']['Restart'], 'on-failure')
                self.assertEqual(config['Service']['RestartSec'], '30s')
                self.assertIn('network-online.target', config['Unit']['After'])
                self.assertIn(f'enable {name}.service', presets)
        self.assertIn('lyrionmusicserver.service', unit('squeezelite')['Unit']['After'])
        self.assertIn('squeezelite.service', unit('randy')['Unit']['After'])
        self.assertNotIn('lyrionmusicserver.service', unit('randy')['Unit']['Requires'])
        self.assertIn('/usr/bin/squeezelite', unit('squeezelite')['Service']['ExecStart'])
        self.assertIn('/usr/bin/node /opt/randy/index.js', unit('randy')['Service']['ExecStart'])

    def test_default_output_and_lms_backend(self):
        settings = (ROOT / 'mkosi.extra/etc/default/squeezelite').read_text()
        self.assertIn('SL_SOUNDCARD=default', settings)
        self.assertIn('SL_NAME=Randy', settings)
        self.assertIn('LMS_SERVER_HOST=127.0.0.1', settings)
        app = (ROOT / 'mkosi.extra/etc/randy/randy.env').read_text()
        self.assertIn('RANDY_PLAYER_BACKEND=lms', app)
        self.assertIn('LMS_SERVER_URL=http://127.0.0.1:9000', app)

    def test_source_allowlist_excludes_state_and_developer_files(self):
        for forbidden in ('DB', '.git', 'test', 'tests', '.env', 'node_modules'):
            self.assertNotIn(forbidden, builder.APP_FILES)
        for flag in ('-DFFMPEG', '-DPULSEAUDIO', '-DPORTAUDIO'):
            self.assertNotIn(flag, builder.SQUEEZELITE_OPTS)
        self.assertIn('-DLINKALL', builder.SQUEEZELITE_OPTS)

    def test_download_hash_mismatch_fails_closed(self):
        pin = {'url': 'https://example.invalid/pinned', 'sha256': '0' * 64}
        with tempfile.TemporaryDirectory() as work:
            with mock.patch.object(builder.urllib.request, 'urlopen', return_value=io.BytesIO(b'synthetic-source')):
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                    builder.fetch(pin, Path(work) / 'download')

    def test_final_runtime_checks_do_not_start_services_or_assume_lms_version_flag(self):
        hook = (ROOT / 'mkosi.postinst.chroot').read_text()
        self.assertIn('/usr/bin/perl -c /usr/sbin/squeezeboxserver', hook)
        self.assertNotIn('squeezeboxserver --version', hook)
        self.assertNotIn('systemctl start', hook)
        self.assertNotIn('npm ', hook)

    def test_diagnostics_omit_journal_message_secrets(self):
        secret = 'SYNTHETIC_TEST_TOKEN_DO_NOT_PRINT'
        row = json.dumps({'__REALTIME_TIMESTAMP': '1000000', '_SYSTEMD_UNIT': 'randy.service',
                          'PRIORITY': '3', 'MESSAGE': secret})
        def command(args):
            return row if args[0] == 'journalctl' else 'synthetic-safe-status'
        output = io.StringIO()
        with mock.patch.object(diagnostics, 'capture', side_effect=command), \
             mock.patch.object(diagnostics.Path, 'read_text', side_effect=FileNotFoundError), \
             contextlib.redirect_stdout(output):
            diagnostics.main()
        self.assertNotIn(secret, output.getvalue())
        self.assertIn('priority=3', output.getvalue())

    def test_unit_files_have_basic_systemd_structure(self):
        for name in ('lyrionmusicserver', 'squeezelite', 'randy-player-id', 'randy'):
            config = unit(name)
            self.assertEqual(config['Install']['WantedBy'], 'multi-user.target')
            command = config['Service']['ExecStart'].split()[0]
            self.assertTrue(command.startswith('/usr/'))
            self.assertEqual(config['Service']['NoNewPrivileges'], 'yes')


if __name__ == '__main__':
    unittest.main()
