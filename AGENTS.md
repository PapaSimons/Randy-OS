# Randy-OS

This repository builds the minimal headless Debian Trixie x86_64 Randy appliance
with mkosi. The Randy application is a separate repository; do not modify it here.

Alpha 1 is only the UEFI-bootable base OS: systemd, NetworkManager, Avahi, ALSA,
and OpenSSH. Keep the ESP at 512 MiB and ext4 RANDY root at 8 GiB. No MUSIC
partition or automatic expansion yet.

Prefer mkosi configuration, repart definitions, systemd presets and small config
files. Keep CI inspection small. Build on Linux, not natively on macOS. Pin mkosi
and Debian snapshot inputs together when updating the toolchain. Do not claim
byte-for-byte reproducibility or successful boot without testing it.

Do not add LMS, Squeezelite, Node.js/Randy, onboarding, installers, a desktop,
X11, Wayland, PulseAudio, PipeWire, Docker, A/B updates, Secure Boot, dm-verity,
or encryption in Alpha 1. Do not commit secrets, SSH keys, passwords or generated
images. Public test keys belong in the ignored build overlay only.

Validation: run mkosi summary and mkosi build on Linux, then
sudo scripts/validate-image.sh mkosi.output/randy-os-alpha1-x86_64.raw.
Changes to boot behavior require a real UEFI boot check before declaring them
working. Do not commit unless the user explicitly asks.
