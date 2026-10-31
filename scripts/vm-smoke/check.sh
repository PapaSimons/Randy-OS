#!/usr/bin/env bash
# Installed only into a disposable VM test copy, never into the build output.
set -Eeuo pipefail
exec > /dev/ttyS0 2>&1

finish() {
    local status=$?
    trap - EXIT
    if (( status == 0 )); then
        echo RANDY_VM_SMOKE_PASS
    else
        echo RANDY_VM_SMOKE_FAIL
        systemctl --failed --no-pager || true
        systemctl list-jobs --no-pager || true
        journalctl -b --no-pager -n 400 || true
    fi
    systemctl --no-block poweroff
    exit "$status"
}
trap finish EXIT

echo 'Checking Randy UEFI boot'
test -d /sys/firmware/efi
grep -qw 'systemd.firstboot=no' /proc/cmdline
test "$(cat /proc/sys/kernel/hostname)" = randy
for unit in multi-user.target NetworkManager.service avahi-daemon.service ssh.service; do
    systemctl is-active --quiet "$unit"
    echo "[OK] $unit active"
done
test "$(systemctl show systemd-firstboot.service -p ActiveState --value)" = inactive
systemctl is-enabled --quiet randy-wifi-import.service
# CONFIG is empty: importer must finish successfully, including its no-input path.
test "$(systemctl show randy-wifi-import.service -p ActiveState --value)" = active
test "$(systemctl show randy-wifi-import.service -p SubState --value)" = exited
test "$(systemctl show randy-wifi-import.service -p Result --value)" = success
test ! -e /run/randy-config/randy-wifi.txt
echo '[OK] Wi-Fi importer completed without credentials or Wi-Fi hardware'

# Future Alpha 2 units are checked only when installed; nothing is installed here.
for unit in lms.service logitechmediaserver.service lyrionmusicserver.service \
            slimserver.service squeezelite.service randy.service; do
    if [[ "$(systemctl show "$unit" -p LoadState --value)" != not-found ]]; then
        systemctl is-active --quiet "$unit"
        echo "[OK] optional $unit active"
    fi
done
failed=$(systemctl --failed --no-legend --plain --no-pager)
if [[ -n "$failed" ]]; then
    printf 'Unexpected failed units:\n%s\n' "$failed"
    exit 1
fi
echo '[OK] no failed units'
