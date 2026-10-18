#!/usr/bin/env bash
# Read-only inspection on a Linux build host. No guest code is executed.
set -euo pipefail
image=${1:?Usage: validate-image.sh IMAGE.raw}
work=$(mktemp -d)
loop=
cleanup() {
    mountpoint -q "$work/config" && umount "$work/config"
    mountpoint -q "$work/esp" && umount "$work/esp"
    mountpoint -q "$work/root" && umount "$work/root"
    if [[ -n "$loop" ]]; then losetup -d "$loop"; fi
    rm -rf "$work"
}
trap cleanup EXIT
sfdisk --json "$image" > "$work/partitions.json"
python3 - "$work/partitions.json" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))['partitiontable']
assert p['label'] == 'gpt', p
parts = p['partitions']
assert len(parts) == 3, parts
for part, kind, label, size in zip(parts,
    ['c12a7328-f81f-11d2-ba4b-00a0c93ec93b',
     'ebd0a0a2-b9e5-4433-87c0-68b6b72699c7',
     '4f68bce3-e8cd-4db1-96e7-fbcaf984b709'],
    ['EFI', 'CONFIG', 'RANDY'], [512 * 1024**2, 64 * 1024**2, 8 * 1024**3]):
    assert part['type'].lower() == kind, part
    assert part['name'] == label, part
    assert part['size'] * p['sectorsize'] == size, part
PY
loop=$(losetup --find --show --partscan --read-only "$image")
udevadm settle
mkdir "$work/esp" "$work/config" "$work/root"
[[ $(blkid -s TYPE -o value "${loop}p1") == vfat ]]
[[ $(blkid -s TYPE -o value "${loop}p2") == vfat ]]
[[ $(blkid -s VERSION -o value "${loop}p2") == FAT32 ]]
[[ $(blkid -s LABEL -o value "${loop}p2") == CONFIG ]]
[[ $(blkid -s TYPE -o value "${loop}p3") == ext4 ]]
mount -o ro "${loop}p1" "$work/esp"
mount -o ro "${loop}p2" "$work/config"
[[ -z $(find "$work/config" -mindepth 1 -print -quit) ]]
mount -o ro,noload "${loop}p3" "$work/root"
root=$work/root
test -s "$work/esp/EFI/BOOT/BOOTX64.EFI"
compgen -G "$work/esp/EFI/Linux/*.efi" > /dev/null
test -x "$root/usr/sbin/NetworkManager"
test -x "$root/usr/sbin/avahi-daemon"
test -x "$root/usr/sbin/sshd"
[[ $(cat "$root/etc/hostname") == randy ]]
[[ ! -s "$root/etc/machine-id" || $(cat "$root/etc/machine-id") == uninitialized ]]
! compgen -G "$root/etc/ssh/ssh_host_*" > /dev/null
for unit in NetworkManager avahi-daemon ssh systemd-timesyncd randy-wifi-import; do
    systemctl --root="$root" is-enabled "$unit.service"
done
[[ $(readlink "$root/etc/systemd/system/default.target") == /usr/lib/systemd/system/multi-user.target ]]
test -f "$root/usr/lib/randy/wifi-import.py"
test -f "$root/etc/systemd/system/randy-wifi-import.service"
test -f "$root/etc/systemd/system/"'run-randy\x2dconfig.mount'
# Verify installed unit syntax and referenced executables against the guest root.
systemd-analyze --root="$root" verify \
    /etc/systemd/system/randy-wifi-import.service \
    '/etc/systemd/system/run-randy\x2dconfig.mount'
# Ensure no provisioning input or pre-provisioned profiles enter the artifact.
[[ -z $(find "$root" -type f -name randy-wifi.txt -print -quit) ]]
[[ -z $(find "$root/etc/NetworkManager/system-connections" -type f -print -quit) ]]
# Fail if a dependency unexpectedly pulls an excluded runtime into the image.
packages=$(dpkg-query --admindir="$root/var/lib/dpkg" -W -f='${binary:Package}\n')
if printf '%s\n' "$packages" | grep -Eq '^(xserver-xorg.*|xwayland|weston|gdm3|sddm|lightdm|pulseaudio.*|pipewire.*|docker.*|moby.*)(:.*)?$'; then
    echo 'Excluded desktop/audio/container package found' >&2
    exit 1
fi
echo 'GPT, filesystems, UEFI payload, services, identity and package checks passed.'
