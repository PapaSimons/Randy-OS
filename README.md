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
mkosi.repart/              Two fixed-size GPT partition definitions
mkosi.extra/               Hostname, networking, SSH and systemd configuration
scripts/validate-image.sh  Small read-only Linux image inspection
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
| 2 | Linux x86-64 root | ext4 | RANDY | 8 GiB | / |

Sizes use systemd's binary M/G units. The raw disk is slightly larger than
8.5 GiB because of GPT/alignment overhead. Use media with at least 16 GB capacity.
There is no MUSIC partition and no first-boot partition/filesystem expansion;
remaining media capacity stays unused.

Explicit packages (plus their required dependencies and Debian essential base):

- System/boot: `systemd-sysv`, `systemd-timesyncd`, `systemd-boot-efi`,
  `linux-image-amd64`, `udev`, `dbus`, `tzdata`.
- Network: `network-manager`, `wpasupplicant`, `wireless-regdb`,
  `firmware-iwlwifi`, `firmware-realtek`, `firmware-mediatek`, `iproute2`,
  `ca-certificates`.
- Discovery: `avahi-daemon`, `libnss-mdns`.
- Audio/access: `alsa-utils`, `openssh-server`.

Wi-Fi drivers come from the Debian kernel with firmware for common Intel,
Realtek and MediaTek devices. Specific hardware support must be tested. There
is no Wi-Fi onboarding or saved Wi-Fi credential. Ethernet uses NetworkManager's
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
and `mkosi build`, and inspects the resulting uncompressed image. It then
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

CI verifies GPT, exactly two partitions, types, labels and exact sizes, FAT/ext4
filesystems, the removable-media EFI bootloader and UKI, NetworkManager, Avahi,
sshd, enabled services, hostname, default target and uninitialized machine ID.
It also rejects cloned SSH host keys and common excluded runtime packages.

Run the same check on Linux after building:

```sh
sudo scripts/validate-image.sh mkosi.output/randy-os-alpha1-x86_64.raw
```

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

Expected: targets/services active, Ethernet connected, hostname `randy`, and
password/keyboard-interactive authentication disabled. Time synchronization also
requires network access to a time server; check `timedatectl timesync-status`.
Verify `randy.local` from a second machine. On two independently flashed devices,
confirm distinct `/etc/machine-id` values and SSH host key fingerprints.

## Flash with balenaEtcher

1. Download and extract the GitHub Actions artifact ZIP.
2. In that directory run `shasum -a 256 -c SHA256SUMS` on your Mac.
3. Open Etcher, choose **Flash from file**, and select the `.raw.xz` file. If your
   Etcher version does not accept it, decompress with `xz -dk` and select `.raw`.
4. Select the intended USB/SSD carefully: flashing erases the whole target disk.
5. Flash, let Etcher validate, eject, connect Ethernet and boot the target using
   its UEFI USB/disk boot option with Secure Boot disabled.

Do not initialize the disk if macOS reports the Linux partition as unreadable.
The flashed drive boots the appliance directly; it is not an installer USB.

## Alpha 2 and later

First establish a successful base-image build and hardware boot. Then scope
Lyrion Music Server, Squeezelite, Node.js/Randy integration, Wi-Fi onboarding,
a MUSIC partition and first-boot expansion. An installer USB belongs to a later
phase. None of these is implemented in Alpha 1.
