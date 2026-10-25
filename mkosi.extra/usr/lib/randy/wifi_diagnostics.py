"""Non-secret CONFIG diagnostics. Never serialize raw command output or profiles."""
import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tempfile
import time

NAME = 'randy-diagnostics.txt'
IDENTIFIER = re.compile(r'[a-zA-Z0-9_.:+-]{1,64}\Z')


def probe(args, timeout=1):
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=timeout,
                                env=dict(os.environ, LC_ALL='C'))
        return result.returncode, result.stdout[:262144]
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return 127, ''


def error_category(text):
    """Map untrusted nmcli errors to fixed labels; never return any error text."""
    text = text.lower()
    for words, category in (
        (('secrets were required', 'no secrets', 'password', 'authentication', 'supplicant'), 'authentication-or-secrets-unavailable'),
        (('no network', 'ssid not found', 'access point could not'), 'requested-network-not-found'),
        (('no suitable device', 'no wi-fi device', 'device not found'), 'wifi-device-unavailable'),
        (('unmanaged',), 'device-unmanaged'),
        (('ip configuration', 'ip-config', 'dhcp'), 'ip-configuration-failed'),
        (('not running',), 'networkmanager-not-running'),
        (('permission', 'not authorized'), 'permission-denied'),
        (('timeout', 'timed out'), 'activation-timed-out'),
    ):
        if any(word in text for word in words):
            return category
    return 'networkmanager-command-failed'


def hardware():
    """Identifiers only: no USB serials, product strings or arbitrary uevent text."""
    found = []
    for bus in ('pci', 'usb'):
        for device in sorted(Path('/sys/bus', bus, 'devices').glob('*'))[:128]:
            try:
                if bus == 'pci':
                    klass = (device / 'class').read_text().strip()
                    if not klass.startswith('0x02'):  # Include network devices even without a driver.
                        continue
                    vendor, product = [(device / key).read_text().strip().removeprefix('0x')
                                       for key in ('vendor', 'device')]
                else:
                    vendor, product = [(device / key).read_text().strip()
                                       for key in ('idVendor', 'idProduct')]
                if not all(re.fullmatch(r'[0-9a-fA-F]{4}', value) for value in (vendor, product)):
                    continue
                driver = (device / 'driver').resolve().name if (device / 'driver').exists() else 'not-bound'
                found.append({'bus': bus, 'location': device.name if IDENTIFIER.fullmatch(device.name) else 'unknown',
                              'vendor_product': vendor + ':' + product,
                              'driver': driver if IDENTIFIER.fullmatch(driver) else 'unknown'})
            except OSError:
                continue
    return found[:64]


def firmware_events(text):
    events = []
    for line in text.splitlines():
        if 'firmware' not in line.lower():
            continue
        lower = line.lower()
        state = ('firmware-load-failed' if any(word in lower for word in ('failed', 'not found', 'error', 'missing'))
                 else 'firmware-loaded' if 'loaded' in lower else 'firmware-message')
        driver = re.search(r'\b(iwlwifi|iwlmvm|rtw89|rtw89_pci|rtw88|rtw88_pci|rtl8xxxu|rtl8192cu|rtl8723be|mt76|mt76x2u|mt7921e|mt7921u|ath9k|ath9k_htc|ath10k_pci|ath11k_pci|ath12k_pci|brcmfmac)\b', lower)
        errno = re.search(r'\berror\s+(-?\d{1,4})\b', lower)
        event = {'event': state, 'driver': driver[1] if driver else 'unknown'}
        if errno:
            event['errno'] = int(errno[1])
        events.append(event)
    return events[-16:]


class Report:
    def __init__(self, source):
        self.source = source
        self.path = source.parent / NAME
        self.secrets = set()
        self.fields = dict(timestamp_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                           hostname=socket.gethostname(), kernel=os.uname().release,
                           config_mounted=os.path.ismount(source.parent), provisioning_file_found=False,
                           requested_ssid='unknown', parsing='not-attempted',
                           networkmanager_service='unknown', wifi_radio='unknown', wifi_interfaces=[],
                           device_state=[], requested_ssid_visible='unknown',
                           ssid_visibility_source='NetworkManager cached scan; no forced rescan',
                           profile_created=False, profile_loaded=False, activation='not-attempted',
                           autoconnect_persisted=False, input_removed=False,
                           provisioning_result='in-progress', reason='not-completed',
                           ip_addresses=[], default_routes=[], default_routes_v6=[], avahi_service='unknown',
                           hardware=[], firmware_events=[])

    def input(self, data):
        try:
            lines = data.decode('utf-8-sig').replace('\r\n', '\n').split('\n')
        except UnicodeError:
            return
        for line in lines:
            key, sep, value = line.partition('=')
            if sep and key == 'PASSWORD' and value:
                self.secrets.add(value)
                if re.fullmatch(r'[0-9a-fA-F]{64}', value):
                    self.secrets.add(value.lower())
            if sep and key == 'SSID' and 1 <= len(value.encode()) <= 32 and not any(ord(c) < 32 for c in value):
                self.fields['requested_ssid'] = value

    def clean(self, value):
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(secret, '[redacted]')
            return value
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, dict):
            return {key: self.clean(item) for key, item in value.items()}
        return value

    def write(self):
        # Never leave a report in a shadow /run directory if CONFIG is absent.
        if str(self.source.parent) == '/run/randy-config' and not os.path.ismount(self.source.parent):
            return
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix='.randy-diagnostics-', dir=self.source.parent)
            with os.fdopen(fd, 'w') as stream:
                stream.write('Randy Alpha 1 headless Wi-Fi diagnostics (no raw logs or secrets)\n')
                for key, value in self.clean(self.fields).items():
                    stream.write(f'{key}: {json.dumps(value, ensure_ascii=True)}\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            # Make the renamed report durable before an appliance is powered down.
            directory = os.open(self.source.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            print('CONFIG diagnostic report could not be fully written.', flush=True)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def collect(self):
        deadline = time.monotonic() + 8
        self.fields['diagnostic_query_status'] = []
        def query(args):
            remaining = deadline - time.monotonic()
            result = probe(args, timeout=min(1, remaining)) if remaining > 0 else (127, '')
            self.fields['diagnostic_query_status'].append({'program': args[0], 'exit_code': result[0]})
            return result
        for unit, field in [('NetworkManager', 'networkmanager_service'), ('avahi-daemon', 'avahi_service')]:
            _, text = query(['systemctl', 'is-active', unit + '.service'])
            self.fields[field] = text.strip() if text.strip() in {'active', 'inactive', 'failed', 'activating', 'deactivating'} else 'unknown'
        _, text = query(['nmcli', '--wait', '1', 'radio', 'wifi'])
        self.fields['wifi_radio'] = text.strip() if text.strip() in {'enabled', 'disabled'} else 'unknown'
        self.fields['wifi_interfaces'] = [p.parent.name for p in Path('/sys/class/net').glob('*/wireless')
                                          if IDENTIFIER.fullmatch(p.parent.name)]
        code, text = query(['nmcli', '--wait', '1', '-t', '--escape', 'no', '-f', 'DEVICE,TYPE,STATE', 'device', 'status'])
        states = []
        for line in text.splitlines() if code == 0 else []:
            parts = line.split(':', 2)
            if len(parts) != 3 or not IDENTIFIER.fullmatch(parts[0]):
                continue
            state = next((s for s in ('disconnected', 'disconnecting', 'connected', 'connecting', 'unavailable', 'unmanaged')
                          if parts[2] == s or parts[2].startswith(s + ' ')), 'unknown')
            states.append({'interface': parts[0], 'type': parts[1] if parts[1] in {'wifi', 'ethernet', 'loopback'} else 'other', 'state': state})
            if parts[1] == 'wifi' and parts[0] not in self.fields['wifi_interfaces']:
                self.fields['wifi_interfaces'].append(parts[0])
        self.fields['device_state'] = states
        self.fields['wifi_drivers'] = []
        for interface in self.fields['wifi_interfaces']:
            device = Path('/sys/class/net', interface, 'device')
            driver = (device / 'driver').resolve().name if (device / 'driver').exists() else 'not-bound'
            self.fields['wifi_drivers'].append({'interface': interface,
                'driver': driver if IDENTIFIER.fullmatch(driver) else 'unknown'})
        self.fields['wifi_rfkill'] = []
        for radio in Path('/sys/class/rfkill').glob('rfkill*'):
            try:
                if (radio / 'type').read_text().strip() == 'wlan':
                    soft, hard = [(radio / name).read_text().strip() for name in ('soft', 'hard')]
                    if soft in {'0', '1'} and hard in {'0', '1'}:
                        self.fields['wifi_rfkill'].append({'soft_blocked': soft == '1', 'hard_blocked': hard == '1'})
            except OSError:
                continue
        code, text = query(['nmcli', '--wait', '1', '-t', '--escape', 'no', '-f', 'SSID', 'device', 'wifi', 'list', '--rescan', 'no'])
        if code == 0 and self.fields['requested_ssid'] != 'unknown':
            self.fields['requested_ssid_visible'] = 'yes' if self.fields['requested_ssid'] in text.splitlines() else 'no'
        for args, field in [(['ip', '-j', 'address', 'show'], 'ip_addresses'),
                            (['ip', '-j', 'route', 'show', 'default'], 'default_routes'),
                            (['ip', '-6', '-j', 'route', 'show', 'default'], 'default_routes_v6')]:
            code, text = query(args)
            values = []
            try:
                for row in json.loads(text) if code == 0 else []:
                    interface = row.get('ifname', row.get('dev', ''))
                    if not IDENTIFIER.fullmatch(interface):
                        continue
                    if field == 'ip_addresses':
                        for address in row.get('addr_info', []):
                            values.append({'interface': interface, 'address': str(ipaddress.ip_address(address['local']))})
                    elif 'gateway' in row:
                        values.append({'interface': interface, 'gateway': str(ipaddress.ip_address(row['gateway']))})
                self.fields[field] = values
            except (ValueError, KeyError, TypeError):
                self.fields[field] = []
        for interface in self.fields['wifi_interfaces'][:4]:
            code, text = query(['nmcli', '--wait', '1', '-g', 'GENERAL.STATE,GENERAL.REASON', 'device', 'show', interface])
            numbers = [int(m[1]) for line in text.splitlines() if (m := re.match(r'^(\d{1,4})\b', line))] if code == 0 else []
            self.fields.setdefault('nm_device_reason_codes', {})[interface] = numbers
        self.fields['hardware'] = hardware()
        code, text = query(['dmesg', '--color=never'])
        self.fields['firmware_events'] = firmware_events(text) if code == 0 else ['kernel-log-unavailable']


def finalize(source):
    """ExecStopPost runs even after timeout or failure before Python main starts."""
    if not os.path.ismount(source.parent):
        return
    path = source.parent / NAME
    if not path.exists():
        report = Report(source)
        report.fields['provisioning_file_found'] = source.exists()
        report.fields['provisioning_result'] = 'no-importer-checkpoint'
        report.fields['reason'] = 'importer-result-unavailable'
        try:
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    report.input(stream.read(8193))
        except OSError:
            pass
        try:
            report.collect()
        except (OSError, ValueError, TypeError):
            report.fields['diagnostic_collection'] = 'partial'
        report.write()
    result = os.environ.get('SERVICE_RESULT', 'unknown')
    if result not in {'success', 'timeout', 'exit-code', 'signal', 'core-dump', 'watchdog', 'resources', 'start-limit-hit', 'oom-kill', 'protocol'}:
        result = 'unknown'
    # Append fixed service status, without reading/re-emitting an existing report.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        with os.fdopen(fd, 'w') as stream:
            stream.write(f'systemd_service_result: {result}\n')
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        print('CONFIG service-result diagnostic could not be written.', flush=True)


if __name__ == '__main__':
    finalize(Path('/run/randy-config/randy-wifi.txt'))
