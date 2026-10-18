# Randy-OS

This repository builds the minimal headless Debian Trixie x86_64 Randy appliance
with mkosi. The Randy application is a separate repository; do not modify it here.

Alpha 1 is only the UEFI-bootable base OS: systemd, NetworkManager, Avahi, ALSA,
and OpenSSH, with a small post-flash Wi-Fi provisioning file importer. Keep the
ESP at 512 MiB, the FAT32 CONFIG partition at 64 MiB, and ext4 RANDY root at 8 GiB.
The user selected CONFIG because RANDY-CONFIG exceeds FAT's 11-character limit.
No MUSIC partition or automatic expansion yet.

Prefer mkosi configuration, repart definitions, systemd presets and small config
files. Keep CI inspection small. Build on Linux, not natively on macOS. Pin mkosi
and Debian snapshot inputs together when updating the toolchain. Do not claim
byte-for-byte reproducibility or successful boot without testing it.

Do not add LMS, Squeezelite, Node.js/Randy, captive onboarding portals, installers, a desktop,
X11, Wayland, PulseAudio, PipeWire, Docker, A/B updates, Secure Boot, dm-verity,
or encryption in Alpha 1. Do not commit secrets, SSH keys, passwords or generated
images. Public test keys belong in the ignored build overlay only. Wi-Fi credentials
must only be placed on CONFIG after flashing, never in overlays, GitHub inputs,
environment variables or images. Treat randy-wifi.txt as data, never shell code.
Keep credentials out of logs and subprocess arguments. Tests use synthetic data.

Validation: run python3 -m unittest discover -s tests -v and bash -n on scripts.
CI also verifies generated keyfiles using libnm and installed units using
systemd-analyze. Then run mkosi summary and mkosi build on Linux, then
sudo scripts/validate-image.sh mkosi.output/randy-os-alpha1-x86_64.raw.
Changes to boot behavior require a real UEFI boot check before declaring them
working. Do not commit unless the user explicitly asks.
