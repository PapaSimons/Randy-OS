# Randy-OS — Alpha 1

Minimal headless Debian Trixie x86_64 appliance base for the
[Randy music player](https://github.com/PapaSimons/Randy). The application lives
in that separate repository and is not installed or modified here.

Alpha 1 produces a raw GPT disk image for direct flashing to USB/SSD. It uses
UEFI and systemd-boot with an unsigned unified kernel image (UKI). Disable Secure
Boot on the test machine. Legacy BIOS boot is not supported.

## Project structure

```text
mkosi.conf                 OS, packages, boot and build configuration
mkosi.repart/              Three fixed-size GPT partition definitions
mkosi.extra/               Hostname, networking, SSH and systemd configuration
scripts/validate-image.sh  Small read-only Linux image inspection
scripts/configure-wifi-macos.sh  Post-flash Wi-Fi helper (built-in macOS tools)
tests/                     Hardware-free provisioning checks
.github/workflows/build.yml
AGENTS.md                  Scope and contributor instructions
.gitignore                 Generated output and local credentials excluded
```

## Image configuration

[mkosi v27.1](https://github.com/systemd/mkosi/releases/tag/v27.1) is pinned in CI.
The target and Linux build tools tree use Debian Trixie and the Debian snapshot
`20261001T000000Z`. A fixed repart seed and `SourceDateEpoch` stabilize build
metadata. Package recommendations are disabled. The default target is
`multi-user.target`.

This is a repeatable, pinned build recipe; byte-for-byte reproducibility has not
been demonstrated. Filesystem metadata and build environment differences can
still affect hashes. Change the Debian snapshot, tools snapshot and source epoch
together when refreshing inputs. Installed APT sources use normal Debian mirrors
for subsequent maintenance; the snapshot pins the image build, not runtime updates.

| Partition | GPT type | Filesystem | Label | Fixed size | Mount |
| --- | --- | --- | --- | --- | --- |
| 1 | EFI System | FAT32 | EFI | 512 MiB | /efi |
| 2 | Microsoft basic data | FAT32 | CONFIG | 64 MiB | /run/randy-config |
| 3 | Linux x86-64 root | ext4 | RANDY | 8 GiB | / |

The disk uses `SectorSize=512`. Both FAT filesystems additionally receive
`SYSTEMD_REPART_MKFS_OPTIONS_VFAT="-S 512"` through mkosi's `[Build] Environment`
setting. Mkosi v27.1 passes that environment to systemd-repart, including builds
invoked with `sudo` in GitHub Actions. This avoids repart's default 4096-byte
filesystem sectors, which macOS refused to mount on the tested 512-byte USB.
Validation reads both EFI and CONFIG FAT BPB fields at offset `0x0B` and requires
512 bytes per sector; `blkid` identifying VFAT alone is not sufficient. No
partition is reformatted after mkosi. Carry this base-image fix into Alpha 2
without altering its music stack.

Sizes use systemd's binary M/G units. The raw disk is slightly larger than
8.5625 GiB because of GPT/alignment overhead. Use media with at least 16 GB capacity.
There is no MUSIC partition and no first-boot partition/filesystem expansion;
remaining media capacity stays unused.

Explicit packages (plus their required dependencies and Debian essential base):

- System/boot: `systemd-sysv`, `systemd-timesyncd`, `systemd-boot-efi`,
  `linux-image-amd64`, `udev`, `dbus`, `tzdata`, `python3`.
- Network: `network-manager`, `wpasupplicant`, `wireless-regdb`,
  `firmware-iwlwifi`, `firmware-realtek`, `firmware-mediatek`, `iproute2`,
  `ca-certificates`, `iw`.
- Discovery: `avahi-daemon`, `libnss-mdns`.
- Audio/access: `alsa-utils`, `openssh-server`.

Wi-Fi drivers come from the Debian kernel with firmware for common Intel,
Realtek and MediaTek devices. Specific hardware support must be tested. There
is a small post-flash file importer, with no Wi-Fi credentials in the built image.
A captive setup portal is deferred. Ethernet uses NetworkManager's
normal automatic DHCP connection behavior. NetworkManager owns networking and
DNS; systemd-networkd is disabled.

Avahi advertises `randy.local` on a network that allows multicast DNS. macOS can
resolve this directly; other clients need mDNS support. Multiple appliances with
the same hostname can cause Avahi name suffixes. SSH is enabled with password and
keyboard-interactive authentication disabled. Root access requires an explicitly
injected public key; there is no default password or autologin.

Machine ID is left uninitialized by mkosi and generated on first boot. SSH host
keys are removed from the image and generated before sshd starts. Journald uses
normal Debian/systemd behavior; `systemd-timesyncd` enables automatic network
time synchronization. No timezone is selected: the OS falls back to UTC until
configured, for example with `timedatectl set-timezone YOUR/ZONE`.

No desktop, X11, Wayland, PulseAudio, PipeWire or Docker is installed.

## Build from macOS using GitHub Actions

No native macOS mkosi installation is needed. Intended flow:

```text
Mac → git push → GitHub Actions Ubuntu Linux → mkosi → downloadable image
```

After reviewing and committing these files yourself, push the repository to
GitHub. Every push and pull request runs the workflow. Alternatively open
**Actions → Build Alpha 1 image → Run workflow**. A manual run can optionally
accept a test SSH public key; automated push/PR builds contain no login key.

CI installs the Python/build dependencies and the official Debian Trixie archive
signing keys (the Ubuntu host keyring can be older), installs the pinned mkosi release
from its upstream GitHub archive, builds a Debian tools tree, runs `mkosi summary`
and `mkosi build`, and inspects the resulting uncompressed image. Hardware-free
unit checks validate parsing, failure/success behavior and keyfile compatibility
with libnm. The image check verifies CONFIG is empty and has the right label,
and verifies the installed import service/mount with systemd-analyze. No Wi-Fi
credential input, environment variable or secret is used by GitHub Actions. It then
compresses with xz, computes SHA-256 over the compressed image, and uploads one
artifact bundle named `randy-os-alpha1-x86_64` containing:

```text
randy-os-alpha1-x86_64.raw.xz
SHA256SUMS
randy-os-alpha1-x86_64.manifest
```

The manifest records installed packages. The checksum detects corruption; it is
not a signature. The workflow has read-only repository permissions and does not
publish a release or deploy anything.

## Inject a test SSH public key

For GitHub Actions, generate a dedicated key locally if needed:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/randy-alpha1 -C randy-alpha1
cat ~/.ssh/randy-alpha1.pub
```

Use a passphrase. Paste only the `.pub` contents into the manual workflow's
`ssh_public_key` field. The private key stays on your Mac. The public key and the
resulting root access are embedded in that build; distribute keyed images only
to intended testers. Workflow inputs are visible to repository collaborators.
Do not enter private keys, passwords, or Wi-Fi secrets.

For a Linux build, create an ignored overlay instead:

```sh
install -d -m 700 .alpha-ssh/root/.ssh
install -m 600 ~/.ssh/randy-alpha1.pub .alpha-ssh/root/.ssh/authorized_keys
cat > mkosi.local.conf <<'CONFIG'
[Content]
ExtraTrees=.alpha-ssh
CONFIG
sudo /path/to/mkosi build
```

The overlay is copied as root during the build. Neither it nor `mkosi.local.conf`
should be committed. Remove the overlay configuration to return to an unkeyed
build. After booting a keyed image:

```sh
ssh -i ~/.ssh/randy-alpha1 root@randy.local
```

An unkeyed image cannot be administered via SSH. Rebuild with your public key for
Alpha testing. Do not work around this with an empty or shared password.

## Validation and first boot

CI verifies GPT, exactly three partitions, types, labels and exact sizes, FAT/ext4
filesystems, the removable-media EFI bootloader and UKI, NetworkManager, Avahi,
sshd, enabled services (including the importer), hostname, default target and
uninitialized machine ID. CONFIG must be empty, FAT32 and labelled CONFIG; the
root image must contain no provisioned NetworkManager profiles.
It also rejects cloned SSH host keys and common excluded runtime packages.

Run the same check on Linux after building:

```sh
sudo scripts/validate-image.sh mkosi.output/randy-os-alpha1-x86_64.raw
```

Run the hardware-free checks on macOS or Linux without mkosi:

```sh
python3 -m unittest discover -s tests -v
bash -n scripts/configure-wifi-macos.sh scripts/validate-image.sh
```

The libnm check skips on macOS; Linux CI installs the library and requires it.
Tests use only temporary synthetic data and mock activation, not real Wi-Fi.

CI does not currently boot QEMU. These are structural checks, not proof of a
successful boot. A first Linux CI build and real UEFI boot remain required; this
repository was initially prepared on macOS. For an optional Linux VM check,
install QEMU/OVMF and use `sudo /path/to/mkosi vm` with the uncompressed image
still present. Otherwise flash to test hardware. On a keyed appliance verify:

```sh
systemctl is-active multi-user.target NetworkManager avahi-daemon ssh systemd-timesyncd
nmcli device status
hostnamectl --static
journalctl -b -p warning
sshd -T | grep -E 'passwordauthentication|kbdinteractiveauthentication|permitrootlogin'
```

Expected: targets/services active, Wi-Fi or Ethernet connected, hostname `randy`, and
password/keyboard-interactive authentication disabled. Time synchronization also
requires network access to a time server; check `timedatectl timesync-status`.
Verify `randy.local` from a second machine. On two independently flashed devices,
confirm distinct `/etc/machine-id` values and SSH host key fingerprints.

## Headless flash and Wi-Fi setup

The visible volume is **CONFIG**, as selected for Alpha 1. The originally proposed
RANDY-CONFIG is 12 characters, longer than FAT32's 11-character label limit.
CONFIG is a normal FAT32 data partition, separate from the EFI System Partition;
it does not change UEFI/systemd-boot behavior. Do not rename the CONFIG volume.

1. Download `randy-os-alpha1-x86_64.raw.xz` and `SHA256SUMS` from the extracted
   GitHub Actions artifact ZIP. Run `shasum -a 256 -c SHA256SUMS` on your Mac.
2. In balenaEtcher choose **Flash from file**, select `.raw.xz`, select the
   intended USB/SSD carefully, and flash. Flashing erases the whole target disk.
   If needed decompress with `xz -dk` and select `.raw` instead. Let Etcher validate.
3. Unplug/replug the flashed drive if CONFIG does not appear automatically.
4. In Finder open **CONFIG**. Ignore the EFI partition; do not initialize the
   Linux root partition if macOS says it is unreadable.
5. From this repository run the macOS helper, which prompts for SSID, a hidden
   password, and optional country code:

   ```sh
   ./scripts/configure-wifi-macos.sh
   # Or, if macOS mounted it at a different path:
   ./scripts/configure-wifi-macos.sh /Volumes/CONFIG
   ```

   Alternatively use a plain-text editor to create `randy-wifi.txt` at the root
   of CONFIG, replacing the placeholders below with your own network values:

   ```text
   SSID=...
   PASSWORD=...
   COUNTRY=SG
   ```

   COUNTRY is optional. Save as UTF-8 plain text, not rich text, and avoid a
   second `.txt` extension. Do not add quotes, a shell `export`, or spaces around
   the field names. Values are literal: spaces, punctuation and equals signs
   are preserved. The first equals sign separates each name from its value.
   Empty lines and lines beginning with `#` are allowed; duplicate/unknown fields
   are rejected. CRLF line endings and a UTF-8 BOM are accepted.
6. Safely eject the drive.
7. Boot the target x86_64 computer from the drive using UEFI, Secure Boot disabled.
8. Wait approximately 1–2 minutes for boot and Wi-Fi activation.
9. From a machine on the same LAN, run `ping randy.local`.
10. SSH with the Alpha testing key already injected at build time:
    `ssh -i ~/.ssh/randy-alpha1 root@randy.local`. See the SSH key section above.

The helper requires no Homebrew or third-party tools, writes only the provisioning
file on the mounted volume, and never echoes the password or puts it in child
process arguments. Do not place the file in the repository, a build overlay,
GitHub Actions inputs/variables, or an artifact. The GitHub-built CONFIG is empty.
The flashed drive boots the appliance directly; it is not an installer USB.

## Import behavior and credential storage

A native systemd mount unit mounts `/dev/disk/by-label/CONFIG` at
`/run/randy-config`, with root-only permissions and `nosuid,nodev,noexec`. The
oneshot service wants that mount and NetworkManager but does not require them.
A missing partition is bounded by a 10-second mount job timeout and does not
prevent boot; the importer skips unless the mount exists. A missing file is a
successful no-op. A missing Wi-Fi adapter is checked for at most 15 seconds,
leaves the file in place, and does not prevent boot.

The importer uses Python's standard library; the file is never sourced or
executed. It accepts SSIDs of 1–32 UTF-8 bytes and WPA/WPA2-Personal ASCII
passphrases of 8–63 characters (or a 64-digit hexadecimal PSK). Open/WEP,
enterprise and WPA3-only networks are not supported by this small importer.
Optional COUNTRY is a two-letter `iw reg set` regulatory hint for that boot,
subject to hardware/driver restrictions, not a persistent regulatory override.

The generated profile is stored persistently as
`/etc/NetworkManager/system-connections/randy-wifi.nmconnection`, owned by root
with mode **0600**. SSID bytes are encoded unambiguously and a WPA PSK is derived
from the passphrase. That PSK remains an authentication secret equivalent to the
password; it is not encrypted storage. No credential enters `nmcli` arguments,
process output or importer journal messages. Child-command output is suppressed.
NetworkManager may log ordinary connection/device metadata, including SSID.

The profile starts with autoconnect disabled. The service asks NetworkManager to
load it and makes one activation attempt, allowing 45 seconds for success. On
success it persists autoconnect, with one automatic retry per cycle, and removes
`randy-wifi.txt`. NetworkManager handles ordinary future connections. The service
has no restart loop and is bounded by a 120-second overall timeout.

On parse/import/connection failure the provisioning file remains untouched for
correction and reboot. An imported profile can remain root-only on RANDY but
stays with autoconnect disabled if provisioning did not finish. A timed-out
activation is explicitly cancelled. A replacement provisioning file written
during import is retained rather than deleted. Reprovisioning replaces the single
Randy Wi-Fi profile. Removal confirms NetworkManager activation, not internet
access or successful mDNS resolution.

**Plaintext caveat:** the FAT32 provisioning file contains the Wi-Fi password in
plaintext and is intended only for simple first-boot setup. Deleting it after
import reduces casual exposure but is **not guaranteed secure erasure** on
flash/SSD media. The helper creates no local credential backup; editors and
operating systems may still retain their own data. Longer term Randy will use
a captive setup portal. Alpha 1 does not attempt secure erasure.

## Wi-Fi troubleshooting

After each boot/provisioning attempt, open **CONFIG/randy-diagnostics.txt** on
your Mac/PC. The report is written on both success and failure and overwritten
on the next boot. It includes the requested SSID, parse/profile/activation
results, service/radio/device state, numeric NetworkManager device reason codes,
IP addresses/routes, PCI network and USB vendor/product IDs, bound Wi-Fi drivers,
rfkill blocks and categorized firmware events. It contains no password, derived
PSK, profile contents, SSH keys, tokens or raw journal/kernel/command output.
NetworkManager errors use fixed categories; firmware events use fixed summaries
and numeric error codes. Other visible SSIDs and USB serials are not written.

Reports are checkpointed before activation, followed by a bounded status snapshot.
An ExecStopPost helper appends systemd's actual result on failure/timeout (or when
the successful oneshot is later stopped). A missing/unwritable CONFIG cannot
receive a report. Diagnostic collection has an eight-second command budget and
does not change credentials, force scans or add connection attempts. SSID
visibility reflects NetworkManager's cached scan: `no` is an observation, not
proof that a hidden/recently started network is unreachable. Firmware details
may be unavailable if kernel log access is restricted.

Successful provisioning still deletes `randy-wifi.txt`; the diagnostic report
remains for Alpha testing. Failed provisioning retains the input file. No console
password or additional package is introduced by this diagnostic mechanism.

- **Wrong SSID/password:** power down and reconnect the drive to your Mac/PC.
  The failed `randy-wifi.txt` remains on CONFIG. Correct the literal values,
  safely eject and reboot. Check the network uses WPA/WPA2-Personal, and that
  SSID spelling, case and spaces are correct. A successful import removes it.
- **Wi-Fi interface not detected:** the file stays on CONFIG. Confirm the adapter
  is supported by Debian Trixie's kernel and included firmware. With temporary
  Ethernet/SSH access, use `nmcli device status`, `rfkill list` if available,
  and `journalctl -b -u NetworkManager` to inspect device/firmware issues.
- **Import failure:** via temporary Ethernet/SSH inspect
  `systemctl status randy-wifi-import.service` and
  `journalctl -b -u randy-wifi-import.service`. Correct the file and reboot;
  there is no infinite retry loop. Do not share the provisioning file or keyfile
  in logs or bug reports.
- **randy.local does not resolve:** allow multicast DNS and check that client and
  appliance share a LAN. Guest isolation can block discovery. Look for Randy in
  the router's DHCP leases and try SSH to its IP. On the appliance check
  `systemctl is-active avahi-daemon NetworkManager` and `nmcli device status`.
  Provisioning-file removal means Wi-Fi activation succeeded even if mDNS fails.
- **CONFIG absent:** unplug/replug after flashing, check the OS disk utility and
  mount the FAT32 CONFIG volume. Do not initialize or reformat the drive. If
  provisioning has already succeeded, the file's absence is expected.

## Alpha 2 and later

First establish a successful base-image build and hardware boot. Then scope
Lyrion Music Server, Squeezelite, Node.js/Randy integration, captive Wi-Fi setup,
a MUSIC partition and first-boot expansion. An installer USB belongs to a later
phase. None of these is implemented in Alpha 1.
