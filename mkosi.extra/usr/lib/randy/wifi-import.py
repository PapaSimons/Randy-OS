#!/usr/bin/python3
"""Import a post-flash CONFIG file; never execute or log its contents."""
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import uuid

SOURCE = Path('/run/randy-config/randy-wifi.txt')
CONNECTIONS = Path('/etc/NetworkManager/system-connections')
PROFILE_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, 'urn:randy-os:wifi-provisioning'))
PROFILE_NAME = 'randy-wifi.nmconnection'


class ProvisionError(Exception):
    """Only fixed, credential-free messages may cross the logging boundary."""


def parse_config(data):
    if len(data) > 8192:
        raise ProvisionError('Provisioning file is too large.')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        raise ProvisionError('Provisioning file must be UTF-8 text.') from None
    values = {}
    # Split only at line endings and the first equals sign. Preserve value spaces.
    for line in text.replace('\r\n', '\n').split('\n'):
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key not in {'SSID', 'PASSWORD', 'COUNTRY'} or key in values:
            raise ProvisionError('Invalid or duplicate provisioning field.')
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ProvisionError('Provisioning values contain control characters.')
        values[key] = value
    if not values.get('SSID') or not values.get('PASSWORD'):
        raise ProvisionError('SSID and PASSWORD are required and must be non-empty.')
    ssid = values['SSID'].encode('utf-8')
    if not 1 <= len(ssid) <= 32:
        raise ProvisionError('SSID must occupy 1 to 32 UTF-8 bytes.')
    password = values['PASSWORD']
    if not (re.fullmatch(r'[0-9a-fA-F]{64}', password) or
            (8 <= len(password) <= 63 and password.isascii())):
        raise ProvisionError('Use an 8–63 character ASCII WPA passphrase or 64 hex digits.')
    country = values.get('COUNTRY', '')
    if country and not re.fullmatch(r'[A-Za-z]{2}', country):
        raise ProvisionError('COUNTRY must be a two-letter code.')
    return ssid, password, country.upper()


def keyfile(ssid, password):
    # Derive a WPA PSK: no escaping of the passphrase into INI syntax is needed.
    # This is still a reusable network secret, not a password protection scheme.
    psk = (password.lower() if len(password) == 64 else
           hashlib.pbkdf2_hmac('sha1', password.encode('ascii'), ssid, 4096, 32).hex())
    # NetworkManager's supported byte-list SSID encoding handles punctuation,
    # numeric-looking SSIDs, leading/trailing spaces and UTF-8 unambiguously.
    encoded_ssid = ''.join(f'{byte};' for byte in ssid)
    return f'''[connection]
id=randy-wifi
uuid={PROFILE_UUID}
type=wifi
autoconnect=false
autoconnect-retries=1

[wifi]
mode=infrastructure
ssid={encoded_ssid}

[wifi-security]
key-mgmt=wpa-psk
psk={psk}
psk-flags=0

[ipv4]
method=auto

[ipv6]
method=auto
'''


def command(*args, timeout=10):
    # Never forward nmcli/iw output: errors can include SSIDs or other input.
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def wifi_available():
    # Sysfs does not contain credentials; allow a short driver/device startup window.
    for _ in range(15):
        if any(Path('/sys/class/net').glob('*/wireless')):
            return True
        time.sleep(1)
    return False


def write_profile(directory, contents):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.randy-wifi-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='ascii') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        target = directory / PROFILE_NAME
        os.replace(temporary, target)
        return target
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def provision(source=SOURCE, connections=CONNECTIONS):
    try:
        # No symlinks or special files, and bounded reads even on malformed media.
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ProvisionError('Provisioning input must be a regular file.')
        data = stream.read(8193)
    ssid, password, country = parse_config(data)
    if not wifi_available():
        raise ProvisionError('No Wi-Fi interface detected; provisioning file retained.')
    if country and not command('iw', 'reg', 'set', country):
        print('Wi-Fi country hint could not be applied; continuing.', flush=True)
    profile = write_profile(connections, keyfile(ssid, password))
    if not command('nmcli', 'connection', 'load', str(profile)):
        raise ProvisionError('NetworkManager could not load the profile; file retained.')
    if not command('nmcli', '--wait', '45', 'connection', 'up', 'uuid', PROFILE_UUID,
                   timeout=50):
        command('nmcli', '--wait', '5', 'connection', 'down', 'uuid', PROFILE_UUID,
                timeout=6)
        raise ProvisionError('Wi-Fi activation failed or timed out; provisioning file retained.')
    if not command('nmcli', 'connection', 'modify', 'uuid', PROFILE_UUID,
                   'connection.autoconnect', 'yes', timeout=10):
        raise ProvisionError('Could not persist autoconnect; provisioning file retained.')
    # Do not remove a replacement file written while activation was in progress.
    if source.read_bytes() != data:
        raise ProvisionError('Provisioning file changed; replacement retained.')
    source.unlink()
    print('Wi-Fi connected; provisioning file removed.', flush=True)
    return True


def main():
    try:
        provision()
        return 0
    except ProvisionError as error:
        print(str(error), flush=True)
    except (OSError, ValueError):
        # Do not print exceptions, paths or traceback locals containing input.
        print('Wi-Fi provisioning could not complete; file retained.', flush=True)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
