#!/usr/bin/env python3
"""Build and run the real-process experiment through the existing hosted guards."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile

from prepare import digest, prepare

HERE = Path(__file__).resolve().parent
LAB = HERE.parents[2]
sys.path.insert(0, str(LAB / 'bin'))
from runtime_lab.resources import DiskBudget, guarded


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args):
    hosted = load('process_hosted', HERE.parent / 'hosted-e2e.py')
    hosted.require_hosted('digitalocean', args.droplet_id)
    if os.geteuid() != 0:
        raise ValueError('worker baseline and runtime require root')
    out = hosted.output_path(args.out, 'digitalocean')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    artifacts, private = out / 'artifacts', out / 'private'
    artifacts.mkdir(mode=0o700)
    private.mkdir(mode=0o700)
    report = {'schema_version': 1, 'outcome': 'inconclusive', 'lab_commit': args.lab_commit,
              'scope': 'same-boot atelet test-process SIGKILL, real worker and runsc; simulated control plane',
              'checks': [], 'cleanup': {'verified': False}}
    def save():
        hosted.save(artifacts / 'results.json', report)
    budget = DiskBudget(out)
    base = load('previous_checkpoint', HERE.parent / 'checkpoint-audit.py')
    testenv = load('process_testenv', HERE.parent / 'checkpoint-test-env.py')
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR') if k in os.environ}
    env.update(GOTOOLCHAIN='local', GOWORK='off', GOFLAGS='-p=2', GOMAXPROCS='2', CGO_ENABLED='1')
    scratch = None

    def phase(name, command, timeout, child_env=None, cwd=None):
        budget.schedule()
        code = guarded(command, artifacts / (name + '.log'), cwd=cwd or args.source,
                       env=child_env or env, timeout=timeout, heavy=True, budget=budget, poll=2)
        report['checks'].append({'name': name, 'exit_code': code})
        save()
        return code

    try:
        budget.start()
        prepared = prepare(args.source, private / 'overlay')
        hosted.save(artifacts / 'sources.json', prepared)
        if hosted.capture(['go', 'env', 'GOVERSION'], env=env).strip() != 'go1.27.3':
            raise ValueError('unexpected Go version')
        (private / 'baseline-overlay.json').write_text('{"Replace": {}}\n')
        identity = pwd.getpwnam('nobody')
        scratch = Path(tempfile.mkdtemp(prefix='checkpoint-process-build-'))
        testenv.prepare(args.source, private / 'baseline-overlay.json', scratch, identity.pw_uid, identity.pw_gid)
        ordinary_env = testenv.environment(env, scratch)
        def ordinary(name, command, timeout):
            return phase(name, testenv.command(scratch, identity.pw_uid, identity.pw_gid, command),
                         timeout, child_env=ordinary_env, cwd='/tmp')
        if ordinary('test-environment', [], 30):
            raise RuntimeError('ordinary test setup failed')
        probe = json.loads((artifacts / 'test-environment.log').read_text())
        if probe['uid'] == 0 or probe['groups'] or probe['created_directory_mode'] != '0o755':
            raise ValueError('ordinary test identity/mode mismatch')
        report['test_environment'] = probe
        ordinary_code = ordinary('baseline', ['go', 'test', '-json', '-race', '-shuffle=on', '-count=1', '-timeout=8m',
                                             './internal/ateworker', './cmd/atelet', './cmd/atelet/internal/snapshottracker'], 1800)
        env = {**env, 'GOCACHE': ordinary_env['GOCACHE'], 'GOMODCACHE': ordinary_env['GOMODCACHE']}
        worker_code = phase('baseline-workers', base.worker_command(['go', 'test', '-json', '-race', '-shuffle=on',
                            '-count=1', '-timeout=8m', './cmd/ateworker-gvisor', './cmd/ateworker-microvm']), 1800)
        report['baseline'] = base.unit_summary(artifacts / 'baseline.log', artifacts / 'baseline-workers.log')
        if ordinary_code or worker_code or report['baseline']['outcome'] != 'pass':
            raise RuntimeError('baseline did not pass all five packages without skips')
        binaries = private / 'binaries'
        binaries.mkdir(mode=0o700)
        for package, output in [('atelet', 'atelet.test'), ('ateworker-gvisor', 'worker.test')]:
            if phase('build-' + package, base.worker_command(['go', 'test', '-c', '-race', '-buildvcs=false',
                     '-overlay', prepared['overlays'][package], '-o', binaries / output, './cmd/' + package]), 600):
                raise RuntimeError('process fixture did not compile')
        if phase('build-guest', ['go', 'build', '-o', binaries / 'guest', HERE.parent / 'checkpoint-interruption/guest.go'],
                 120, child_env={**env, 'CGO_ENABLED': '0'}):
            raise RuntimeError('guest build failed')
        runtime = args.runtime.resolve()
        report['runtime_sha256'] = digest(runtime / 'runsc')
        report['binary_sha256'] = {p.name: digest(p) for p in binaries.iterdir()}
        code = phase('process-matrix', [sys.executable, HERE / 'matrix.py', '--binaries', binaries,
                     '--runtime', runtime / 'runsc', '--out', artifacts / 'process-matrix',
                     '--droplet-id', args.droplet_id], 3000)
        matrix_path = artifacts / 'process-matrix/results.json'
        if not matrix_path.is_file():
            raise RuntimeError('matrix result is missing')
        matrix = json.loads(matrix_path.read_text())
        report['matrix'] = matrix
        records = json.loads((matrix_path.parent / 'records.json').read_text())
        # Recompute from raw records; never trust the matrix's aggregate alone.
        from evidence import summarize
        if matrix != summarize(records):
            raise ValueError('matrix summary does not match raw records')
        report['cleanup']['verified'] = len(records) == 15 and all(r.get('cleanup_verified') for r in records)
        if digest(runtime / 'runsc') != report['runtime_sha256']:
            raise ValueError('runtime executable changed')
        if hosted.capture(['git', '-C', args.source, 'status', '--porcelain', '--untracked-files=all']).strip():
            raise ValueError('upstream checkout changed')
        expected = {'pass': 0, 'findings': 1, 'inconclusive': 2}[matrix['outcome']]
        if code == expected and report['cleanup']['verified']:
            report['outcome'] = matrix['outcome']
    except (Exception, KeyboardInterrupt) as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        if scratch is not None:
            try:
                shutil.rmtree(scratch)
                report['cleanup']['test_workspace_removed'] = True
            except OSError as exc:
                report['cleanup']['test_workspace_removed'] = False
                report['cleanup']['verified'] = False
                report['outcome'] = 'inconclusive'
                report['cleanup']['error'] = str(exc)
        report['disk_samples'] = budget.samples
        save()
    print(json.dumps({'outcome': report['outcome'], 'cleanup': report['cleanup']}))
    return {'pass': 0, 'findings': 1}.get(report['outcome'], 2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--droplet-id', required=True)
    parser.add_argument('--lab-commit', required=True)
    os.umask(0o077)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(run(parser.parse_args()))
