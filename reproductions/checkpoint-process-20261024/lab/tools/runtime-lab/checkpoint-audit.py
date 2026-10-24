"""Unchanged function bodies excerpted for the standalone packet; see manifest.json."""
import json
from pathlib import Path


def worker_command(argv):
    # Keep the controller's root identity for mount/network unit tests, while
    # allowing requested directory modes instead of inheriting artifact umask077.
    return ['/bin/sh', '-c', 'umask 022; exec "$@"', 'checkpoint-worker-tests', *map(str, argv)]


def unit_summary(*logs):
    required = {
        ('go.ate.dev/substrate/cmd/atelet', name) for name in (
            'TestCheckpointFinishesWhenCallerCancels', 'TestCheckpointManifestBootID',
            'TestRestoreLocalSnapshotFromEarlierBoot', 'TestRetriedCheckpointAnswersFromCommittedSnapshot')
    } | {('go.ate.dev/substrate/internal/ateworker', name) for name in (
        'TestRecordAndReuseCheckpoint', 'TestReuseCheckpointDataSnapshotFilesByFidelity')}
    expected_packages = {'go.ate.dev/substrate/' + name for name in (
        'cmd/ateworker-gvisor', 'cmd/ateworker-microvm', 'internal/ateworker',
        'cmd/atelet', 'cmd/atelet/internal/snapshottracker')}
    passed, package_pass, failures, skipped = set(), set(), [], []
    for line in (line for log in logs for line in Path(log).read_text().splitlines()):
        try: event = json.loads(line)
        except json.JSONDecodeError: continue
        if not isinstance(event, dict): continue
        pair = (event.get('Package'), event.get('Test'))
        action = event.get('Action')
        if action == 'pass':
            passed.add(pair)
            if not pair[1]: package_pass.add(pair[0])
        elif action == 'fail': failures.append(pair)
        elif action == 'skip': skipped.append(pair)
    return {'outcome': 'pass' if not failures and not skipped and required <= passed and expected_packages <= package_pass else 'inconclusive',
            'missing_controls': sorted(required - passed), 'failures': failures, 'skips': skipped,
            'packages_passed': sorted(package_pass)}
