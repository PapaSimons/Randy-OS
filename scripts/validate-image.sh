#!/usr/bin/env bash
# Read-only inspection on a Linux build host. No guest code is executed.
set -euo pipefail
image=${1:?Usage: validate-image.sh IMAGE.raw}
work=$(mktemp -d)
loop=
cleanup() {
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
assert len(parts) == 2, parts
for part, kind, label, size in zip(parts,
    ['c12a7328-f81f-11d2-ba4b-00a0c93ec93b',
     '4f68bce3-e8cd-4db1-96e7-fbcaf984b709'],
    ['EFI', 'RANDY'], [512 * 1024**2, 8 * 1024**3]):
    assert part['type'].lower() == kind, part
    assert part['name'] == label, part
    assert part['size'] * p['sectorsize'] == size, part
PY
loop=$(losetup --find --show --partscan --read-only "$image")
udevadm settle
mkdir "$work/esp" "$work/root"
[[ $(blkid -s TYPE -o value "${loop}p1") == vfat ]]
[[ $(blkid -s TYPE -o value "${loop}p2") == ext4 ]]
mount -o ro "${loop}p1" "$work/esp"
mount -o ro,noload "${loop}p2" "$work/root"
root=$work/root
test -s "$work/esp/EFI/BOOT/BOOTX64.EFI"
compgen -G "$work/esp/EFI/Linux/*.efi" > /dev/null
test -x "$root/usr/sbin/NetworkManager"
test -x "$root/usr/sbin/avahi-daemon"
test -x "$root/usr/sbin/sshd"
[[ $(cat "$root/etc/hostname") == randy ]]
[[ ! -s "$root/etc/machine-id" || $(cat "$root/etc/machine-id") == uninitialized ]]
! compgen -G "$root/etc/ssh/ssh_host_*" > /dev/null
for unit in NetworkManager avahi-daemon ssh systemd-timesyncd; do
    systemctl --root="$root" is-enabled "$unit.service"
done
[[ $(readlink "$root/etc/systemd/system/default.target") == /usr/lib/systemd/system/multi-user.target ]]
# Fail if a dependency unexpectedly pulls an excluded runtime into the image.
packages=$(dpkg-query --admindir="$root/var/lib/dpkg" -W -f='${binary:Package}\n')
if printf '%s\n' "$packages" | grep -Eq '^(xserver-xorg.*|xwayland|weston|gdm3|sddm|lightdm|pulseaudio.*|pipewire.*|docker.*|moby.*)(:.*)?$'; then
    echo 'Excluded desktop/audio/container package found' >&2
    exit 1
fi
echo 'GPT, filesystems, UEFI payload, services, identity and package checks passed.'
