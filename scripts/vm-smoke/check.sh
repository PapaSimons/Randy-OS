#!/usr/bin/env bash
# Installed only into a disposable VM test copy, never into the build output.
set -Eeuo pipefail
exec > /dev/ttyS0 2>&1

finish() {
    local status=$?
    trap - EXIT
    if (( status == 0 )); then
        echo 'Randy VM smoke test: PASS'
    else
        echo 'Randy VM smoke test: FAIL'
        systemctl --failed --no-pager || true
        systemctl list-jobs --no-pager || true
        journalctl -b --no-pager -n 400 || true
    fi
    # The result is written synchronously before requesting shutdown. Failure
    # to shut down does not change the recorded result; the host bounds the wait.
    systemctl --no-block poweroff || true
    exit "$status"
}
trap finish EXIT

check() {
    local name=$1
    shift
    echo "Randy VM check: START $name"
    if "$@"; then
        echo "Randy VM check: PASS $name"
    else
        echo "Randy VM check: FAIL $name"
        return 1
    fi
}

firstboot_completed() {
    local state
    state=$(systemctl show systemd-firstboot.service -p ActiveState --value) || return 1
    [[ $state == inactive ]] && return 0
    # RemainAfterExit=yes can retain active/exited after prompting was suppressed.
    [[ $state == active ]] &&
        [[ $(systemctl show systemd-firstboot.service -p SubState --value) == exited ]] &&
        [[ $(systemctl show systemd-firstboot.service -p Result --value) == success ]]
}

importer_completed() {
    local state substate result
    systemctl is-enabled --quiet randy-wifi-import.service || return 1
    state=$(systemctl show randy-wifi-import.service -p ActiveState --value) || return 1
    substate=$(systemctl show randy-wifi-import.service -p SubState --value) || return 1
    result=$(systemctl show randy-wifi-import.service -p Result --value) || return 1
    [[ $result == success ]] || return 1
    # Empty CONFIG completes without association; absent CONFIG can condition-skip
    # the importer. Neither is a boot failure. An ongoing/failed import is rejected.
    [[ $state == active && $substate == exited || $state == inactive && $substate == dead ]]
}

critical_units_healthy() {
    local failed unit rest
    failed=$(systemctl --failed --no-legend --plain --no-pager) || return 1
    while read -r unit rest; do
        case "$unit" in
            multi-user.target|basic.target|sysinit.target|local-fs.target|systemd-remount-fs.service|NetworkManager.service|avahi-daemon.service|ssh.service|ssh.socket|sshd-keygen.service|randy-wifi-import.service)
                echo "Critical failed unit: $unit"
                return 1 ;;
        esac
    done <<< "$failed"
    if [[ -n $failed ]]; then printf 'Noncritical failed units (informational):\n%s\n' "$failed"; fi
}

check 'UEFI boot' test -d /sys/firmware/efi
check 'firstboot kernel flag' grep -qw 'systemd.firstboot=no' /proc/cmdline
check 'hostname randy' test "$(cat /proc/sys/kernel/hostname)" = randy
for unit in multi-user.target NetworkManager.service avahi-daemon.service ssh.service; do
    check "$unit active" systemctl is-active --quiet "$unit"
done
check 'firstboot completed without blocking' firstboot_completed
check 'SSH configuration' /usr/sbin/sshd -t
check 'OpenSSH server banner' python3 - <<'PYSSH'
import socket
import time

deadline = time.monotonic() + 3
with socket.create_connection(('127.0.0.1', 22), timeout=3) as connection:
    data = b''
    while len(data) < 255 and b'\n' not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SystemExit('SSH banner timeout')
        connection.settimeout(remaining)
        chunk = connection.recv(255 - len(data))
        if not chunk:
            raise SystemExit('SSH connection closed before server banner')
        data += chunk
    if not (data.startswith(b'SSH-2.0-OpenSSH_') and b'\n' in data):
        raise SystemExit('No OpenSSH protocol banner')
PYSSH
check 'Wi-Fi importer completed or condition-skipped' importer_completed
check 'no baked-in Wi-Fi credentials' test ! -e /run/randy-config/randy-wifi.txt
check 'critical boot units healthy' critical_units_healthy
