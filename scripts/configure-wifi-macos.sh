#!/bin/bash
# Uses only utilities supplied with macOS. Keep passwords out of argv/history.
set +x
set -euo pipefail
volume=${1:-/Volumes/CONFIG}
if [[ $# -gt 1 || ! -d "$volume" || ! -w "$volume" ]]; then
    printf 'Usage: %s [mounted CONFIG volume path]\n' "$0" >&2
    exit 1
fi
# Do not accidentally save credentials into an ordinary directory on the Mac.
if ! /sbin/mount | /usr/bin/grep -Fq " on $volume ("; then
    printf 'The requested path must be a mounted CONFIG volume.\n' >&2
    exit 1
fi
trap 'unset password; printf "\n"' EXIT
IFS= read -r -p 'Wi-Fi SSID: ' ssid
IFS= read -r -s -p 'Wi-Fi password: ' password
printf '\n'
IFS= read -r -p 'Country code (optional, e.g. SG): ' country
if [[ -z "$ssid" || -z "$password" ]]; then
    printf 'SSID and password must be non-empty.\n' >&2
    exit 1
fi
if [[ -n "$country" && ! "$country" =~ ^[A-Za-z][A-Za-z]$ ]]; then
    printf 'Country code must contain two letters.\n' >&2
    exit 1
fi
umask 077
# printf is a Bash builtin: credential values do not become child-process argv.
# Reject symlinks so an unexpected destination cannot redirect the credentials.
if [[ -L "$volume/randy-wifi.txt" ]]; then
    printf 'Refusing a symlink at the provisioning file path.\n' >&2
    exit 1
fi
{
    printf 'SSID=%s\nPASSWORD=%s\n' "$ssid" "$password"
    if [[ -n "$country" ]]; then printf 'COUNTRY=%s\n' "$country"; fi
} > "$volume/randy-wifi.txt"
unset password
printf 'Wi-Fi configuration written to CONFIG. Safely eject the drive.\n'
