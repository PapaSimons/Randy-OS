#!/usr/bin/python3
"""Build-only mkosi hook. Pinned inputs; toolchain/caches stay in the build overlay."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SQUEEZELITE_OPTS = '-DLINKALL -DRESAMPLE -DUSE_SSL -DOPUS'
APP_FILES = ('index.js', 'lib', 'public', 'package.json', 'package-lock.json', 'LICENSE.md')


def run(*args, **kwargs):
    print('[BUILD]', *map(str, args), flush=True)
    return subprocess.run(args, check=True, **kwargs)


def fetch(component, path):
    print('[FETCH]', component['url'], flush=True)
    digest = hashlib.sha256()
    with urllib.request.urlopen(component['url'], timeout=120) as response, path.open('wb') as output:
        for chunk in iter(lambda: response.read(1024 * 1024), b''):
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != component['sha256']:
        raise RuntimeError(f"SHA256 mismatch: expected {component['sha256']}, actual {digest.hexdigest()}")


def unpack(archive, destination):
    destination.mkdir()
    with tarfile.open(archive) as stream:
        stream.extractall(destination, filter='data')
    children = list(destination.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        raise RuntimeError('Expected one top-level directory in source archive')
    return children[0]


def prune_dependency_tests(modules):
    # npm ships upstream package tests even for --omit=dev; none are runtime inputs.
    for directory, dirs, _ in os.walk(modules, topdown=True):
        for name in list(dirs):
            if name in {'test', 'tests', '__tests__', '.github'}:
                shutil.rmtree(Path(directory) / name)
                dirs.remove(name)


def main():
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('Build this hook in the mkosi Debian x86_64 build overlay, not macOS')
    dest = Path(os.environ['DESTDIR'])
    pins = json.loads((ROOT / 'stack.lock.json').read_text())
    with tempfile.TemporaryDirectory(prefix='randy-stack-') as temporary:
        work = Path(temporary)
        archives = {}
        for name, pin in pins.items():
            archives[name] = work / (name + '.archive')
            fetch(pin, archives[name])

        lms = work / 'lms'
        run('dpkg-deb', '--extract', archives['lms'], lms)
        # Upstream still declares the obsolete libgcc1 dependency. Install its
        # payload, with Trixie runtime dependencies explicitly listed in mkosi.conf.
        # No SysV init script, vendor unit, root user or maintainer script is used.
        shutil.copytree(lms / 'usr', dest / 'usr', dirs_exist_ok=True, symlinks=True)
        shutil.copytree(lms / 'etc/squeezeboxserver', dest / 'etc/squeezeboxserver', dirs_exist_ok=True)
        # Test Perl bootstrap against the real final paths inside this disposable
        # build overlay (the same payload is already staged for the final image).
        shutil.copytree(lms / 'usr', Path('/usr'), dirs_exist_ok=True, symlinks=True)
        shutil.copytree(lms / 'etc/squeezeboxserver', Path('/etc/squeezeboxserver'), dirs_exist_ok=True)
        run('/usr/bin/perl', '-c', '/usr/sbin/squeezeboxserver')

        sq = unpack(archives['squeezelite'], work / 'squeezelite')
        run('make', '-C', sq, '-j2', f'OPTS={SQUEEZELITE_OPTS}')
        run('ldd', sq / 'squeezelite')
        bindir = dest / 'usr/bin'
        bindir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sq / 'squeezelite', bindir / 'squeezelite')
        run('strip', bindir / 'squeezelite')
        result = run(str(bindir / 'squeezelite'), '-?', capture_output=True, text=True)
        help_text = result.stdout + result.stderr
        print(help_text, flush=True)
        if pins['squeezelite']['version'] not in help_text:
            raise RuntimeError('Built Squeezelite version does not match stack.lock.json')

        node = unpack(archives['node'], work / 'node')
        nodebin = node / 'bin/node'
        run(nodebin, '--version')
        shutil.copy2(nodebin, bindir / 'node')
        licenses = dest / 'usr/share/doc/randy-stack'
        licenses.mkdir(parents=True)
        shutil.copy2(node / 'LICENSE', licenses / 'NODE-LICENSE')
        shutil.copy2(sq / 'LICENSE.txt', licenses / 'SQUEEZELITE-LICENSE')

        source = unpack(archives['randy'], work / 'randy')
        app = dest / 'opt/randy'
        app.mkdir(parents=True)
        for name in APP_FILES:
            origin = source / name
            if origin.is_dir():
                shutil.copytree(origin, app / name)
            else:
                shutil.copy2(origin, app / name)
        # Never seed DB contents from a developer checkout/source archive.
        (app / 'DB').symlink_to('/var/lib/randy')
        env = dict(os.environ, PATH=str(node / 'bin') + ':' + os.environ['PATH'],
                   npm_config_cache=str(work / 'npm-cache'), npm_config_update_notifier='false')
        run(nodebin, node / 'lib/node_modules/npm/bin/npm-cli.js', 'ci', '--omit=dev',
            '--ignore-scripts', '--no-audit', '--no-fund', '--engine-strict', cwd=app, env=env)
        prune_dependency_tests(app / 'node_modules')
        run(nodebin, '--check', app / 'index.js')
        run(nodebin, '-e', "for (const m of ['express','body-parser','music-metadata','socket.io','./lib/player/lms']) require(m)",
            cwd=app, env=env)
        if 'RANDY_PLAYER_BACKEND' not in (app / 'index.js').read_text():
            raise RuntimeError('Pinned Randy does not provide selectable LMS playback')
        metadata = dest / 'usr/share/randy-os'
        metadata.mkdir(parents=True)
        (metadata / 'stack.lock.json').write_text(json.dumps(pins, indent=2) + '\n')
        contributions = {}
        for name, path in {'lms': dest / 'usr/share/squeezeboxserver',
                           'lms-perl': dest / 'usr/share/perl5', 'node': bindir / 'node',
                           'squeezelite': bindir / 'squeezelite', 'randy': app}.items():
            contributions[name] = sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink()) if path.is_dir() else path.stat().st_size
        (metadata / 'component-sizes.json').write_text(json.dumps(contributions, indent=2) + '\n')
        print('[SIZE] Installed component bytes:', json.dumps(contributions), flush=True)


if __name__ == '__main__':
    main()
