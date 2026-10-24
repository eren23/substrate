#!/usr/bin/env python3
"""Prepare the exact audited Go overlay without modifying the source checkout."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

PACKET = Path(__file__).resolve().parent

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def instrument(source):
    stage = '\t\tif err := root.Rename(src, dst); err != nil {\n\t\t\treturn fmt.Errorf("failed to move %s to %s: %w", src, dst, err)\n\t\t}\n'
    edits = [
        (stage, stage + '\t\tif err := auditCheckpointBoundary("first-stage"); err != nil { return err }\n'),
        ('\n\t\tstate = s.snapshots.CommitLocal(actorUID, snap)', '\n\t\tif err := auditCheckpointBoundary("local-publication"); err != nil { return nil, err }\n\t\tstate = s.snapshots.CommitLocal(actorUID, snap)'),
        ('\n\t\tstate = s.snapshots.CommitDurable(actorUID, durable)', '\n\t\tif err := auditCheckpointBoundary("durable-publication"); err != nil { return nil, err }\n\t\tstate = s.snapshots.CommitDurable(actorUID, durable)'),
    ]
    for before, after in edits:
        if source.count(before) != 1:
            raise ValueError('checkpoint boundary moved or is ambiguous')
        source = source.replace(before, after)
    return source


def prepare(source, out):
    source, out = Path(source).resolve(), Path(out).resolve()
    if out == source or out.is_relative_to(source) or source.is_relative_to(out):
        raise ValueError('overlay must be separate from upstream source')
    meta = json.loads((PACKET / 'manifest.json').read_text())
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()
    if git('rev-parse', 'HEAD') != meta['substrate_revision'] or git('status', '--porcelain', '--untracked-files=all'):
        raise ValueError('expected clean pinned Substrate checkout')
    for name, expected in meta['upstream_files'].items():
        if sha(source / name) != expected:
            raise ValueError('source hash mismatch: ' + name)
    fixture = PACKET / 'recovery_audit_test.go'
    if sha(fixture) != meta['fixture_sha256']:
        raise ValueError('fixture hash mismatch')
    changed = instrument((source / 'cmd/atelet/main.go').read_text())
    if hashlib.sha256(changed.encode()).hexdigest() != meta['instrumented_main_sha256']:
        raise ValueError('instrumented source differs from the remote-tested overlay')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        main = out / 'atelet-main.go'
        main.write_text(changed)
        overlay = {'Replace': {str(source / 'cmd/atelet/main.go'): str(main),
                              str(source / 'cmd/atelet/recovery_audit_test.go'): str(fixture)}}
        (out / 'overlay.json').write_text(json.dumps(overlay, indent=2) + '\n')
        print(json.dumps({'overlay': str(out / 'overlay.json'), 'instrumented_main_sha256': sha(main),
                          'fixture_sha256': sha(fixture), 'upstream_unchanged': not git('status', '--porcelain', '--untracked-files=all')}))
    except BaseException:
        shutil.rmtree(out)
        raise

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    prepare(args.source, args.out)
