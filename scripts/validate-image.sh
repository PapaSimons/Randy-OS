#!/usr/bin/env bash
# Read-only Linux image inspection. Guest programs are never executed.
set -Eeuo pipefail
step=initialization
command_output=

fail() {
    printf '\nERROR: %s\nExpected: %s\nActual: %s\n' "$step" "$1" "${2:-<empty>}" >&2
    exit 1
}

unexpected_error() {
    printf '\nERROR: unexpected command failure during %s\nExpected: exit status 0\nActual: exit status %s at line %s: %s\n' \
        "$step" "$1" "$2" "$3" >&2
    exit "$1"
}

# Commands are explicitly tested: set -e cannot hide their output/status.
capture() {
    step=$1
    shift
    printf '\n[CHECK] %s\n' "$step"
    printf 'Command:'
    printf ' %q' "$@"
    printf '\n'
    local status
    if command_output=$("$@" 2>&1); then
        printf '[OK] Command exited 0\n'
    else
        status=$?
        fail 'command exits 0' "exit status $status; output: ${command_output:-<empty>}"
    fi
}

run() {
    capture "$@"
    printf '%s\n' "${command_output:-<no output>}"
}

equal() {
    step=$1
    printf '\n[CHECK] %s\nExpected: %s\nActual: %s\n' "$step" "$2" "${3:-<empty>}"
    if [[ "$3" != "$2" ]]; then fail "$2" "$3"; fi
    printf '[OK] Values match\n'
}

check_file() {
    local description=$1 path=$2 flag=$3
    run "$description: file metadata (expected $flag $path)" ls -ld "$path"
    step=$description
    printf '[CHECK] %s; expected test %s %s\n' "$description" "$flag" "$path"
    if test "$flag" "$path"; then
        printf '[OK] File satisfies %s\n' "$flag"
    else
        fail "test $flag $path succeeds" "test failed; metadata above: $command_output"
    fi
}

probe_equal() {
    local device=$1 field=$2 expected=$3
    # VERSION is not exposed by the ordinary cache-based blkid interface.
    capture "$device $field (expected $expected; direct superblock probe)" \
        blkid --probe -s "$field" -o value "$device"
    equal "$device $field" "$expected" "$command_output"
}

validate_partitions() {
    run 'GPT partition table: scheme, sector units, count, order, GUIDs, labels and exact sizes' \
        python3 - "$1" <<'PY'
import json
import sys


def check(name, expected, actual):
    print(f'[CHECK] {name}\nExpected: {expected!r}\nActual: {actual!r}', flush=True)
    if actual != expected:
        print(f'ERROR: {name} does not match', file=sys.stderr, flush=True)
        sys.exit(1)
    print('[OK] Values match', flush=True)


try:
    with open(sys.argv[1]) as stream:
        table = json.load(stream)['partitiontable']
    check('partition table scheme', 'gpt', table.get('label'))
    check('partition size units', 'sectors', table.get('unit'))
    sectorsize = table['sectorsize']
    if not isinstance(sectorsize, int) or sectorsize <= 0:
        check('positive integer sector size', 'positive integer', sectorsize)
    print(f'[INFO] Logical sector size: {sectorsize} bytes')
    parts = table['partitions']
    check('partition count', 3, len(parts))
    expected = [
        ('EFI', 'c12a7328-f81f-11d2-ba4b-00a0c93ec93b', 512 * 1024**2),
        ('CONFIG', 'ebd0a0a2-b9e5-4433-87c0-68b6b72699c7', 64 * 1024**2),
        ('RANDY', '4f68bce3-e8cd-4db1-96e7-fbcaf984b709', 8 * 1024**3),
    ]
    for number, (part, (label, guid, size)) in enumerate(zip(parts, expected), 1):
        print(f'[INFO] Partition {number}: {part!r}')
        check(f'partition {number} GPT type', guid, part.get('type', '').lower())
        check(f'partition {number} GPT label/order', label, part.get('name'))
        check(f'partition {number} size in bytes', size, part['size'] * sectorsize)
except (OSError, ValueError, KeyError, TypeError) as error:
    print(f'ERROR: cannot inspect GPT metadata\nExpected: valid sfdisk JSON with sector sizes and partitions\nActual: {error}', file=sys.stderr)
    sys.exit(1)
PY
}

check_packages() {
    # No grep -q pipeline: SIGPIPE must not hide a forbidden package match.
    run 'Reject excluded desktop, audio-server and container packages' python3 -c '
import re, sys
pattern = re.compile(r"^(xserver-xorg.*|xwayland|weston|gdm3|sddm|lightdm|pulseaudio.*|pipewire.*|docker.*|moby.*)(:.*)?$")
bad = [line for line in sys.stdin.read().splitlines() if pattern.fullmatch(line)]
print("Expected: no excluded packages\nActual: " + (", ".join(bad) if bad else "none"))
if bad:
    print("ERROR: excluded runtime packages installed", file=sys.stderr)
    sys.exit(1)
'
}

cleanup() {
    local status=$? output mounted_left=false directory
    trap - EXIT ERR
    # A cleanup failure must not mask the original failure or turn success green.
    for directory in "$work/root" "$work/config" "$work/esp"; do
        if mountpoint -q "$directory"; then
            printf '\n[CLEANUP] Unmount %s\n' "$directory"
            if output=$(umount "$directory" 2>&1); then :; else
                printf 'ERROR: cleanup unmount\nExpected: unmounted %s\nActual: %s\n' "$directory" "$output" >&2
                mounted_left=true
                status=1
            fi
        fi
    done
    if [[ "$mounted_left" == false ]]; then
        if [[ -n "$loop" ]]; then
            printf '[CLEANUP] Detach %s\n' "$loop"
            if output=$(losetup -d "$loop" 2>&1); then :; else
                printf 'ERROR: cleanup detach\nExpected: detached %s\nActual: %s\n' "$loop" "$output" >&2
                status=1
            fi
        fi
        if output=$(rm -rf "$work" 2>&1); then :; else
            printf 'ERROR: cleanup temporary directory\nExpected: removed %s\nActual: %s\n' "$work" "$output" >&2
            status=1
        fi
    else
        printf 'ERROR: leaving mounted workspace at %s for inspection\n' "$work" >&2
    fi
    exit "$status"
}

main() {
    trap 'unexpected_error "$?" "$LINENO" "$BASH_COMMAND"' ERR
    if [[ $# != 1 ]]; then
        step=arguments
        fail 'validate-image.sh IMAGE.raw' "$# arguments supplied"
    fi
    local image=$1 tool device root packages machine_id
    for tool in uname id sfdisk python3 losetup udevadm lsblk blkid mount umount mountpoint \
                find systemctl systemd-analyze dpkg-query readlink ls cat mktemp mkdir rm; do
        capture "Required host command: $tool" command -v "$tool"
    done
    capture 'Linux host kernel (x86_64 guest is checked through its GPT root type)' uname -s
    equal 'host supports Linux loop devices' Linux "$command_output"
    capture 'Root privileges for loop/mount operations' id -u
    equal 'effective UID' 0 "$command_output"
    check_file 'Input raw image is nonempty' "$image" -s
    run 'Host architecture (informational; no guest binaries are executed)' uname -m
    run 'blkid implementation/version' blkid --version
    run 'systemd-analyze implementation/version' systemd-analyze --version
    capture 'Create inspection workspace' mktemp -d
    work=$command_output
    loop=
    trap cleanup EXIT

    run 'GPT disk dump before loop setup' sfdisk --dump "$image"
    capture 'Read machine-readable GPT metadata' sfdisk --json "$image"
    printf '%s\n' "$command_output" > "$work/partitions.json"
    validate_partitions "$work/partitions.json"
    capture 'Attach read-only loop device and scan partitions' \
        losetup --find --show --partscan --read-only "$image"
    loop=$command_output
    step='loop device output'
    printf '[CHECK] Expected a /dev/loopN path; actual: %s\n' "$loop"
    if [[ ! "$loop" =~ ^/dev/loop[0-9]+$ ]]; then fail '/dev/loopN' "$loop"; fi
    run 'Wait for udev partition-device setup (30-second limit)' udevadm settle --timeout=30
    run 'Loop device, filesystems, labels, GPT types and sizes (display only; not parsed)' \
        lsblk -o NAME,TYPE,FSTYPE,LABEL,PARTLABEL,PARTTYPE,SIZE "$loop"
    for device in "${loop}p1" "${loop}p2" "${loop}p3"; do
        check_file 'Expected loop partition device exists' "$device" -b
        run "Uncached blkid metadata for $device" blkid --probe -o export "$device"
    done
    probe_equal "${loop}p1" TYPE vfat
    probe_equal "${loop}p1" LABEL EFI
    probe_equal "${loop}p2" TYPE vfat
    probe_equal "${loop}p2" VERSION FAT32
    probe_equal "${loop}p2" LABEL CONFIG
    probe_equal "${loop}p3" TYPE ext4
    probe_equal "${loop}p3" LABEL RANDY

    run 'Create read-only inspection mountpoints' mkdir "$work/esp" "$work/config" "$work/root"
    run 'Mount EFI read-only' mount -o ro "${loop}p1" "$work/esp"
    run 'Mount CONFIG read-only' mount -o ro "${loop}p2" "$work/config"
    capture 'CONFIG directory must be empty' find "$work/config" -mindepth 1 -print -quit
    equal 'CONFIG contains no baked-in files' '' "$command_output"
    run 'Mount ext4 root read-only without journal replay' mount -o ro,noload "${loop}p3" "$work/root"
    root=$work/root
    check_file 'Removable-media x86_64 UEFI loader is nonempty' "$work/esp/EFI/BOOT/BOOTX64.EFI" -s
    capture 'List EFI payload files' find "$work/esp/EFI" -maxdepth 3 -type f -print
    printf '%s\n' "$command_output"
    capture 'At least one UKI exists in EFI/Linux' find "$work/esp/EFI/Linux" -maxdepth 1 -type f -name '*.efi' -print
    step='UKI existence'
    if [[ -z "$command_output" ]]; then fail 'one or more EFI/Linux/*.efi files' '<none>'; fi
    printf '[OK] UKIs: %s\n' "$command_output"
    for tool in NetworkManager avahi-daemon sshd; do
        check_file "$tool executable in guest" "$root/usr/sbin/$tool" -x
    done
    capture 'Read guest hostname' cat "$root/etc/hostname"
    equal 'hostname' randy "$command_output"
    step='first-boot machine ID'
    printf '\n[CHECK] machine ID must be absent, empty or uninitialized\n'
    machine_id='<absent>'
    if [[ -e "$root/etc/machine-id" ]]; then
        capture 'Read guest machine ID' cat "$root/etc/machine-id"
        machine_id=$command_output
    fi
    step='first-boot machine ID'
    printf 'Actual: %s\n' "${machine_id:-<empty>}"
    if [[ -n "$machine_id" && "$machine_id" != '<absent>' && "$machine_id" != uninitialized ]]; then
        fail 'absent, empty or uninitialized machine ID' "$machine_id"
    fi
    capture 'No cloned SSH host keys' find "$root/etc/ssh" -maxdepth 1 -name 'ssh_host_*' -print
    equal 'SSH host key paths' '' "$command_output"
    for tool in NetworkManager avahi-daemon ssh systemd-timesyncd randy-wifi-import; do
        capture "$tool.service must be enabled" systemctl --root="$root" is-enabled "$tool.service"
        equal "$tool.service enablement" enabled "$command_output"
    done
    run 'Default target link metadata (expected /usr/lib/systemd/system/multi-user.target)' ls -ld "$root/etc/systemd/system/default.target"
    capture 'Read configured default-target link' readlink "$root/etc/systemd/system/default.target"
    equal 'default target link' /usr/lib/systemd/system/multi-user.target "$command_output"
    check_file 'Wi-Fi importer is installed' "$root/usr/lib/randy/wifi-import.py" -f
    check_file 'Wi-Fi import service is installed' "$root/etc/systemd/system/randy-wifi-import.service" -f
    check_file 'Optional CONFIG mount unit is installed' "$root/etc/systemd/system/"'run-randy\x2dconfig.mount' -f
    run 'Verify installed systemd units and referenced executables against guest root' \
        systemd-analyze --root="$root" verify /etc/systemd/system/randy-wifi-import.service \
        '/etc/systemd/system/run-randy\x2dconfig.mount'
    capture 'No provisioning input in guest root' find "$root" -type f -name randy-wifi.txt -print -quit
    equal 'provisioning input paths' '' "$command_output"
    capture 'No pre-provisioned NetworkManager profiles' \
        find "$root/etc/NetworkManager/system-connections" -type f -print -quit
    equal 'pre-provisioned NetworkManager profile paths' '' "$command_output"
    capture 'Read installed package database' dpkg-query --admindir="$root/var/lib/dpkg" -W -f='${binary:Package}\n'
    packages=$command_output
    check_packages <<< "$packages"
    printf '\n[PASS] All image validation checks passed.\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
