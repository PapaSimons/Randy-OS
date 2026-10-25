#!/usr/bin/python3
"""Create one persistent locally administered unicast player ID, never in builds."""
import os
from pathlib import Path
import re
import secrets
import tempfile

PATTERN = re.compile(r'LMS_PLAYER_ID=([0-9a-f]{2}(?::[0-9a-f]{2}){5})\n\Z')


def ensure_identity(directory):
    target = directory / 'player.env'
    if target.exists():
        match = PATTERN.fullmatch(target.read_text())
        if not match or int(match[1][:2], 16) & 3 != 2:
            raise ValueError('Existing player ID is invalid; refusing to replace an installation identity')
        return match[1]
    raw = bytearray(secrets.token_bytes(6))
    raw[0] = (raw[0] & 0xfc) | 0x02
    player_id = ':'.join(f'{part:02x}' for part in raw)
    fd, temporary = tempfile.mkstemp(prefix='.player-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(f'LMS_PLAYER_ID={player_id}\n')
            stream.flush()
            os.fsync(stream.fileno())
        # Publish a complete file without replacing an identity from another start.
        try:
            os.link(temporary, target)
        except FileExistsError:
            return ensure_identity(directory)
    finally:
        os.unlink(temporary)
    return player_id


if __name__ == '__main__':
    ensure_identity(Path('/var/lib/randy-device'))
