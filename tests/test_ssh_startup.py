"""SSH lifecycle and safe-report checks; all key material is temporary test data."""
import configparser
import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'mkosi.extra/usr/lib/randy'))
import wifi_diagnostics as diagnostics


class SshStartupTests(unittest.TestCase):
    def test_generation_is_before_validation_without_firstboot_condition(self):
        path = ROOT / 'mkosi.extra/etc/systemd/system/ssh.service.d/10-host-keys.conf'
        text = path.read_text()
        self.assertNotIn('ConditionFirstBoot=', text)
        self.assertEqual([line for line in text.splitlines() if line.startswith('ExecStartPre=')],
                         ['ExecStartPre=', 'ExecStartPre=/usr/bin/ssh-keygen -A',
                          'ExecStartPre=/usr/sbin/sshd -t'])
        config = configparser.ConfigParser(interpolation=None)
        config.read(ROOT / 'mkosi.conf')
        self.assertIn('/etc/ssh/ssh_host_*', config['Content']['RemoveFiles'].split())
        self.assertEqual(config['Content']['RootPassword'], 'hashed:!')
        self.assertFalse(list((ROOT / 'mkosi.extra/etc/ssh').glob('ssh_host_*')))
        presets = (ROOT / 'mkosi.extra/usr/lib/systemd/system-preset/00-randy.preset').read_text()
        self.assertIn('enable ssh.service', presets)
        self.assertIn('disable ssh.socket', presets)

    @unittest.skipUnless(shutil.which('ssh-keygen'), 'OpenSSH keygen required')
    def test_native_keygen_preserves_keys_and_distinguishes_fresh_installations(self):
        with tempfile.TemporaryDirectory() as directory:
            public = []
            for name in ('installation-a', 'installation-b'):
                prefix = Path(directory) / name
                keydir = prefix / 'etc/ssh'
                keydir.mkdir(parents=True)
                subprocess.run(['ssh-keygen', '-A', '-f', str(prefix)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
                before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in keydir.iterdir()}
                self.assertIn('ssh_host_ed25519_key', before)
                subprocess.run(['ssh-keygen', '-A', '-f', str(prefix)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
                after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in keydir.iterdir()}
                self.assertTrue(before == after, 'ssh-keygen -A replaced existing keys')
                public.append((keydir / 'ssh_host_ed25519_key.pub').read_bytes())
            self.assertTrue(public[0] != public[1], 'Fresh installations reused a host key')

    def test_banner_requires_protocol_line_instead_of_tcp_accept(self):
        cases = [([b''], 'closed-before-banner'),
                 ([b'SSH-2.0-', b'OpenSSH_10.0\r\n'], 'received'),
                 ([b'SYNTHETIC_UNTRUSTED_BANNER\n'], 'invalid-banner'),
                 ([b'x' * 255], 'invalid-banner')]
        for chunks, expected in cases:
            with self.subTest(expected=expected):
                connection = mock.MagicMock()
                connection.__enter__.return_value = connection
                connection.recv.side_effect = chunks
                with mock.patch.object(diagnostics.socket, 'create_connection', return_value=connection):
                    self.assertEqual(diagnostics.ssh_banner(), expected)
        for error, expected in [(TimeoutError(), 'timeout'), (OSError(), 'unavailable')]:
            with mock.patch.object(diagnostics.socket, 'create_connection', side_effect=error):
                self.assertEqual(diagnostics.ssh_banner(), expected)

    def test_ssh_report_categories_and_untrusted_output_are_bounded(self):
        token = 'SYNTHETIC_SECRET_NEVER_SERIALIZED'
        cases = [
            (True, 0, 'active', 'inactive', 'received', '', 'ok'),
            (False, 1, 'failed', 'inactive', 'unavailable', '', 'missing-host-keys'),
            (True, 1, 'failed', 'inactive', 'unavailable', '', 'invalid-config'),
            (True, 0, 'failed', 'inactive', 'unavailable', '', 'service-start-failed'),
            (True, 0, 'inactive', 'active', 'closed-before-banner', '', 'socket-only'),
            (True, 0, 'active', 'inactive', 'closed-before-banner',
             'fatal: rexec of /usr/lib/openssh/sshd-session failed: Permission denied', 'session-exec-failed'),
            (True, 0, 'active', 'inactive', 'closed-before-banner',
             'Missing privilege separation directory: /run/sshd', 'privilege-separation-failed'),
            (True, 0, 'active', 'inactive', 'closed-before-banner', '', 'unknown'),
            (True, 127, token, token, 'unavailable', token, 'unknown'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            keydir = Path(directory)
            key = keydir / 'ssh_host_ed25519_key'
            for present, code, service, sock, banner, journal, category in cases:
                with self.subTest(category=category):
                    if present:
                        key.write_text(token)
                    else:
                        key.unlink(missing_ok=True)
                    def query(args):
                        if args[0] == 'systemctl':
                            return 0, (sock if args[-1] == 'ssh.socket' else service) + '\n'
                        if args[0] == '/usr/sbin/sshd':
                            self.assertEqual(args, ['/usr/sbin/sshd', '-t'])
                            return code, token
                        return 0, journal + token
                    with mock.patch.object(diagnostics, 'ssh_banner', return_value=banner), \
                         contextlib.redirect_stdout(io.StringIO()) as output:
                        fields = diagnostics.ssh_diagnostics(query, keydir)
                    self.assertEqual(fields['ssh_failure_category'], category)
                    self.assertNotIn(token, repr(fields) + output.getvalue())


if __name__ == '__main__':
    unittest.main()
