#!/usr/bin/env python3
"""Run the exact process matrix only on the identified disposable Linux VM."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys

from evidence import classify, summarize
from trial import save, trial


def main(args):
    path = Path(__file__).resolve().parent.parent / 'hosted-e2e.py'
    spec = importlib.util.spec_from_file_location('process_hosted', path)
    hosted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hosted)
    hosted.require_hosted('digitalocean', args.droplet_id)
    if os.geteuid() != 0:
        raise ValueError('real worker experiment requires root on the owned VM')
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    records = []
    binaries = {'atelet': args.binaries / 'atelet.test', 'ateworker-gvisor': args.binaries / 'worker.test'}
    # Establish working controls before consuming budget on suspected failures.
    cases = ('uninterrupted', 'worker-complete', 'tracker-committed', 'first-stage', 'local-publication')
    for case in cases:
        for iteration in range(1, 4):
            record = trial(case, iteration, binaries, args.runtime, args.binaries / 'guest',
                           args.out / f'{case}-{iteration}', dict(os.environ))
            records.append(record)
            save(args.out / 'records.json', records)
            result = summarize(records)
            save(args.out / 'results.json', result)
            verdict = classify(record)
            print(json.dumps(verdict), flush=True)
            if verdict['outcome'] == 'inconclusive':
                return 2
    return {'pass': 0, 'findings': 1, 'inconclusive': 2}[result['outcome']]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binaries', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--droplet-id', required=True)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(main(parser.parse_args()))
