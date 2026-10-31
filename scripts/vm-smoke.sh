#!/usr/bin/env bash
# Linux/root only. Instrument and boot a sparse disposable copy of the raw image.
set -Eeuo pipefail
if [[ $# != 1 || $(uname -s) != Linux || $EUID != 0 ]]; then
    echo 'Usage (Linux): sudo scripts/vm-smoke.sh IMAGE.raw' >&2
    exit 2
fi
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
image=$(realpath -- "$1")
log_dir=$(pwd)/vm-smoke-logs
mkdir -p "$log_dir"
for log in serial.log console.log qemu.log result.txt; do : > "$log_dir/$log"; done
printf 'Host: preparing disposable VM image\n' > "$log_dir/result.txt"
exec > >(tee "$log_dir/host.log") 2>&1
for tool in qemu-system-x86_64 losetup udevadm mount umount objcopy python3; do
    command -v "$tool" >/dev/null
done
firmware=${OVMF_CODE:-/usr/share/OVMF/OVMF_CODE_4M.fd}
variables=${OVMF_VARS:-/usr/share/OVMF/OVMF_VARS_4M.fd}
test -s "$firmware"
test -s "$variables"
work=$(mktemp -d)
loop=
cleanup() {
    local status=$? safe=yes
    trap - EXIT
    for dir in "$work/root" "$work/esp"; do
        if mountpoint -q "$dir"; then
            if ! umount "$dir"; then safe=no; status=1; fi
        fi
    done
    if [[ $safe == yes && -n $loop ]]; then
        if ! losetup -d "$loop"; then safe=no; status=1; fi
    fi
    if [[ $safe == yes ]]; then rm -rf -- "$work"; else echo "Cleanup incomplete: $work" >&2; fi
    exit "$status"
}
trap cleanup EXIT
cp --reflink=auto --sparse=always -- "$image" "$work/smoke.raw"
cp -- "$variables" "$work/OVMF_VARS.fd"
loop=$(losetup --find --show --partscan "$work/smoke.raw")
udevadm settle --timeout=30
mkdir "$work/root" "$work/esp"
mount "${loop}p3" "$work/root"
mount "${loop}p1" "$work/esp"
install -D -m 755 "$source_dir/vm-smoke/check.sh" "$work/root/usr/local/lib/randy-vm-smoke-check"
install -m 644 "$source_dir/vm-smoke/"*.service "$source_dir/vm-smoke/"*.timer "$work/root/etc/systemd/system/"
mkdir -p "$work/root/etc/systemd/system/timers.target.wants"
ln -s ../randy-vm-smoke.timer "$work/root/etc/systemd/system/timers.target.wants/randy-vm-smoke.timer"
systemd-analyze --root="$work/root" verify \
    /etc/systemd/system/randy-vm-smoke.service /etc/systemd/system/randy-vm-smoke.timer
# Add serial logging only. In particular, never add/repair the firstboot flag here:
# missing or ineffective firstboot suppression must fail the boot check.
ukis=("$work/esp/EFI/Linux/"*.efi)
test -f "${ukis[0]}"
for uki in "${ukis[@]}"; do
    objcopy --dump-section ".cmdline=$work/cmdline" "$uki" "$work/extract.efi"
    python3 - "$work/cmdline" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
original = path.read_bytes().rstrip(b'\0')
path.write_bytes(original + b' console=tty0 console=ttyS0,115200 systemd.show_status=yes\0')
PY
    objcopy --update-section ".cmdline=$work/cmdline" "$uki" "$work/serial.efi"
    cp "$work/serial.efi" "$uki"
done
umount "$work/root" "$work/esp"
losetup -d "$loop"
loop=
echo 'Booting disposable image with OVMF UEFI and QEMU TCG (600-second limit)'
status=0
python3 "$source_dir/vm-smoke/run.py" --log-dir "$log_dir" -- \
    qemu-system-x86_64 -machine q35,accel=tcg -cpu max -smp 2 -m 2048 \
    -drive "if=pflash,format=raw,readonly=on,file=$firmware" \
    -drive "if=pflash,format=raw,file=$work/OVMF_VARS.fd" \
    -drive "if=virtio,format=raw,file=$work/smoke.raw" \
    -nic user,model=virtio-net-pci -display none -monitor none \
    -serial "file:$log_dir/serial.log" -no-reboot || status=$?
cat "$log_dir/console.log"
cat "$log_dir/qemu.log"
if (( status != 0 )); then
    echo "VM smoke test FAILED. See $log_dir/result.txt and serial.log" >&2
    exit 1
fi
echo 'VM smoke test passed'
