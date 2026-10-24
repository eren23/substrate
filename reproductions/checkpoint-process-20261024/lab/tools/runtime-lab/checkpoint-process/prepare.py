#!/usr/bin/env python3
"""Prepare private process-test overlays against an exact, clean upstream pin."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
LAB = HERE.parents[2]
PIN = '1756c0f7cb344035889da699981eeff4c8aef755'


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def instrument(source):
    stage = '\t\tif err := root.Rename(src, dst); err != nil {\n\t\t\treturn fmt.Errorf("failed to move %s to %s: %w", src, dst, err)\n\t\t}\n'
    commit = '\n\t\tstate = s.snapshots.CommitLocal(actorUID, snap)'
    worker = '\t// The snapshot commits on this node once worker has written it, in either\n'
    for before, after in [
        (stage, stage + '\t\tcpBoundary("first-stage")\n'),
        (commit, '\n\t\tcpBoundary("local-publication")' + commit + '\n\t\tcpBoundary("tracker-committed")'),
        (worker, '\tcpBoundary("worker-complete")\n' + worker),
    ]:
        if source.count(before) != 1:
            raise ValueError('checkpoint boundary moved or ambiguous')
        source = source.replace(before, after)
    return source


def prepare(source, out):
    source, out = Path(source).resolve(), Path(out).resolve()
    if out == source or out.is_relative_to(source) or source.is_relative_to(out):
        raise ValueError('overlay must be outside upstream source')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()
    if git('rev-parse', 'HEAD') != PIN or git('status', '--porcelain', '--untracked-files=all'):
        raise ValueError('expected clean pinned upstream checkout')
    original = source / 'cmd/atelet/main.go'
    # The prior evidence pin independently names the tested production source.
    meta = json.loads((LAB / 'docs/contributions/runtime-recovery-audit-20261024/packages.json').read_text())
    for name, expected in meta['upstream_files'].items():
        if digest(source / name) != expected:
            raise ValueError('source hash mismatch: ' + name)
    changed = instrument(original.read_text())
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        main = out / 'main.go'
        main.write_text(changed)
        overlays = {}
        for package, fixture in [('atelet', 'atelet_process_test.go'), ('ateworker-gvisor', 'worker_process_test.go')]:
            replacements = {str(source / 'cmd' / package / 'checkpoint_process_test.go'): str(HERE / fixture),
                            str(source / 'cmd' / package / 'checkpoint_process_common_test.go'): str(HERE / 'process_common_test.go')}
            if package == 'atelet':
                replacements[str(original)] = str(main)
            target = out / (package + '.json')
            target.write_text(json.dumps({'Replace': replacements}, indent=2) + '\n')
            overlays[package] = str(target)
        result = {'substrate_revision': PIN, 'upstream_files': meta['upstream_files'],
                  'instrumented_main_sha256': digest(main),
                  'fixtures': {p.name: digest(p) for p in sorted(HERE.glob('*_test.go'))},
                  'overlays': overlays}
        (out / 'sources.json').write_text(json.dumps(result, indent=2) + '\n')
        return result
    except BaseException:
        shutil.rmtree(out)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.out)))
