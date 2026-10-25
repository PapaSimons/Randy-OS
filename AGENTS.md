# Randy-OS

This repository builds the minimal headless Debian Trixie x86_64 Randy appliance
with mkosi. The Randy application is a separate repository; do not modify it here.

Alpha 1 is frozen for physical testing. This branch is Alpha 2: add only the
systemd-supervised LMS/Squeezelite/Node/Randy music stack. Do not modify the
separate Randy application repository. Pin its published LMS-ready commit and
all downloaded stack inputs in stack.lock.json; never download latest at boot.

Preserve the ESP at 512 MiB, FAT32 CONFIG at 64 MiB and ext4 RANDY root at 8 GiB.
Preserve the existing CONFIG Wi-Fi importer and UEFI/unsigned-UKI build setup.
The user selected CONFIG because RANDY-CONFIG exceeds FAT's label limit.
No MUSIC partition or automatic expansion yet.

Prefer mkosi configuration, repart definitions, systemd presets and small config
files. Keep CI inspection small. Build on Linux, not natively on macOS. Pin mkosi
and Debian snapshot inputs together when updating the toolchain. Do not claim
byte-for-byte reproducibility or successful boot without testing it.

Do not add SpotOn (Alpha 2b), ffmpeg, yt-dlp, Qobuz, Tidal, captive onboarding,
installers, desktops, X11, Wayland, PulseAudio, PipeWire, Docker, A/B updates,
Secure Boot, dm-verity or encryption in this phase. Do not commit secrets, SSH keys, passwords or generated
images. Public test keys belong in the ignored build overlay only. Wi-Fi credentials
must only be placed on CONFIG after flashing, never in overlays, GitHub inputs,
environment variables or images. Treat randy-wifi.txt as data, never shell code.
Keep credentials out of logs and subprocess arguments. Tests use synthetic data.

Validation: run python3 -m unittest discover -s tests -v and bash -n on scripts.
CI also verifies generated keyfiles using libnm and installed units using
systemd-analyze. Then run mkosi summary and mkosi build on Linux, then
sudo scripts/validate-image.sh mkosi.output/randy-os-alpha2-x86_64.raw.
Changes to boot behavior require a real UEFI boot check before declaring them
working. Do not commit unless the user explicitly asks.

Long-running LMS, Squeezelite and Randy belong to systemd under separate users.
Player identity is generated at first boot in /var/lib/randy-device/player.env;
never bake a player ID into an image. Both services read that same EnvironmentFile.
Randy must not manage LMS or spawn a persistent Squeezelite process. Audio-output
privilege integration is deferred: do not grant broad service-control or config
write privileges to Randy. Keep source/build caches/toolchains out of the image.
