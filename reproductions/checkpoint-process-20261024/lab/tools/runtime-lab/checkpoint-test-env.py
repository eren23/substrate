#!/usr/bin/env python3
"""Private copy for ordinary tests; the real-runtime phase stays privileged."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys


def prepare(source, overlay, scratch, uid, gid):
    source, scratch = Path(source).resolve(), Path(scratch).resolve()
    if uid == 0 or gid == 0:
        raise ValueError('ordinary tests require an unprivileged identity')
    scratch.mkdir(mode=0o700, exist_ok=True)
    if any(scratch.iterdir()):
        raise ValueError('test workspace must be empty')
    shutil.copytree(source, scratch / 'source', symlinks=True)
    replacements = {}
    (scratch / 'overlay-files').mkdir()
    for index, (old, replacement) in enumerate(json.loads(Path(overlay).read_text())['Replace'].items()):
        target = scratch / 'source' / Path(old).resolve().relative_to(source)
        copied = scratch / 'overlay-files' / f'{index}.go'
        shutil.copyfile(replacement, copied)
        replacements[str(target)] = str(copied)
    (scratch / 'overlay.json').write_text(json.dumps({'Replace': replacements}) + '\n')
    for name in ('home', 'tmp', 'go-build', 'go-mod'):
        (scratch / name).mkdir(mode=0o700)
    for directory, dirs, files in os.walk(scratch, followlinks=False):
        for name in files + dirs:
            os.chown(Path(directory) / name, uid, gid, follow_symlinks=False)
    os.chown(scratch, uid, gid)
    scratch.chmod(0o700)


def environment(base, scratch):
    scratch = Path(scratch).resolve()
    return {**base, 'HOME': str(scratch / 'home'), 'TMPDIR': str(scratch / 'tmp'),
            'GOCACHE': str(scratch / 'go-build'), 'GOMODCACHE': str(scratch / 'go-mod')}


def command(scratch, uid, gid, argv):
    return [sys.executable, str(Path(__file__).resolve()), str(Path(scratch).resolve()),
            str(uid), str(gid), *map(str, argv)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scratch', type=Path)
    parser.add_argument('uid', type=int)
    parser.add_argument('gid', type=int)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.uid <= 0 or args.gid <= 0:
        raise ValueError('ordinary tests require an unprivileged identity')
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(args.gid)
        os.setuid(args.uid)
    if os.getuid() != args.uid or os.geteuid() != args.uid or os.getgid() != args.gid or os.getegid() != args.gid:
        raise ValueError('test process identity mismatch')
    os.umask(0o022)
    os.chdir(args.scratch / 'source')
    if args.command:
        os.execvpe(args.command[0], args.command, os.environ)
    else:
        probe = args.scratch / 'tmp/mode-probe'
        probe.mkdir(mode=0o755)
        mode = probe.stat().st_mode & 0o777
        probe.rmdir()
        print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'groups': os.getgroups(),
                          'umask': '022', 'created_directory_mode': oct(mode)}))


if __name__ == '__main__':
    main()
