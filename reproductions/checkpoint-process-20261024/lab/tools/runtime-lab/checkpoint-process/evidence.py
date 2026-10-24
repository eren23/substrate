"""Evidence gates for the focused process experiment; runtime failure is data."""
from pathlib import PurePosixPath
import re

CASES = ('first-stage', 'local-publication', 'worker-complete', 'tracker-committed', 'uninterrupted')
FAULTS = frozenset(CASES[:2])


def snapshot_matches(state, snapshot, disk=False):
    if not isinstance(state, dict):
        return False
    key = 'latestLocal' if disk else 'latest_local'
    ref = state.get(key)
    got = ref.get('snapshot') if isinstance(ref, dict) else None
    if not isinstance(got, dict):
        return False
    if disk:
        # SnapshotRef persists protojson, while RPC evidence uses encoding/json.
        got = dict(got)
        if 'snapshotUuid' in got:
            got['snapshot_uuid'] = got.pop('snapshotUuid')
        if got.get('fidelity') == 'SNAPSHOT_FIDELITY_MEMORY':
            got['fidelity'] = 3
        if isinstance(got.get('object'), dict):
            got['object'] = dict(got['object'])
            if 'snapshotUri' in got['object']:
                got['object']['snapshot_uri'] = got['object'].pop('snapshotUri')
    return got == snapshot


def require_inventory(observation):
    if not isinstance(observation, dict) or not isinstance(observation.get('files'), dict):
        raise ValueError('file inventory missing')
    if 'disk' not in observation or (observation['disk'] is not None and not isinstance(observation['disk'], dict)):
        raise ValueError('disk observation missing')
    for path, item in observation['files'].items():
        if (not path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or
                not isinstance(item, dict) or type(item.get('bytes')) is not int or item['bytes'] < 0 or
                not re.fullmatch('[0-9a-f]{64}', str(item.get('sha256', '')))):
            raise ValueError('invalid file inventory')


def require_processes(r):
    roles = {'initialize', 'services', 'worker', 'atelet-first', 'first-client', 'retry-client'}
    if r['case'] != 'uninterrupted':
        roles.add('atelet-replacement')
    exits = r['child_exits']
    if len(exits) != len(roles) or {e['role'] for e in exits} != roles:
        raise ValueError('child exit receipts missing')
    by_role = {e['role']: e for e in exits}
    for role, event in by_role.items():
        expected = -9 if role == 'atelet-first' and r['case'] != 'uninterrupted' else 0
        if event['returncode'] != expected or not event['group_verified'] or event['pid'] <= 0:
            raise ValueError('unexpected child exit or unverified process group')
    replacement = 'atelet-first' if r['case'] == 'uninterrupted' else 'atelet-replacement'
    for role, key in [('worker', 'worker_pid'), ('atelet-first', 'atelet_pid'), (replacement, 'replacement_pid')]:
        if by_role[role]['pid'] != r[key]:
            raise ValueError('process receipt identity mismatch')
    for role, key in [('first-client', 'first_rpc'), ('retry-client', 'retry')]:
        event = r[key]
        if (event['pid'] != by_role[role]['pid'] or event['token'] != r['token'] or
                event['event'] != 'response' or not isinstance(event['code'], str) or not event['code']):
            raise ValueError('RPC observation missing or wrong identity')
    if (r['first_rpc']['code'] == 'OK') != (r['case'] == 'uninterrupted'):
        raise ValueError('initial RPC does not match the interruption')


def require_runtime(r):
    pending, completed = {}, {}
    for event in r['runtime_records']:
        identity = event['id']
        if not identity or event['token'] != r['token'] or event['observer_pid'] <= 0:
            raise ValueError('runtime record identity mismatch')
        if event['event'] == 'start' and identity not in pending and identity not in completed:
            pending[identity] = event
        elif event['event'] == 'end' and identity in pending:
            start = pending.pop(identity)
            if (any(start[k] != event[k] for k in ('args', 'operation', 'observer_pid')) or
                    event['time'] < start['time'] or type(event['returncode']) is not int):
                raise ValueError('runtime result does not match invocation')
            completed[identity] = event
        else:
            raise ValueError('unpaired or interrupted runtime command')
    captures = [e for e in completed.values() if e['operation'] == 'checkpoint']
    if (pending or not captures or len(captures) != r['capture_attempts'] or
            sum(e['returncode'] == 0 for e in captures) != r['successful_captures']):
        raise ValueError('runtime capture evidence missing or inconsistent')


def classify(r):
    def result(outcome, reason):
        return {'case': r.get('case'), 'iteration': r.get('iteration'), 'outcome': outcome, 'reason': reason}
    try:
        case = r['case']
        if case not in CASES or r['iteration'] not in (1, 2, 3):
            raise ValueError('unknown trial')
        if r.get('error') or not r['cleanup_verified']:
            raise ValueError('execution or cleanup failed')
        if not 0 <= r['elapsed_seconds'] <= 180:
            raise ValueError('observation exceeded its bound')
        if r['initial_state'] is not None or not r['startup_complete']:
            raise ValueError('fresh initialization/startup was not established')
        if (r['request_digest'] != r['retry_request_digest'] or
                r['boot_id'] != r['replacement_boot_id'] or
                r['worker_pid'] != r['worker_pid_after']):
            raise ValueError('request, boot, or worker changed')
        require_processes(r)
        cleanups = r['runtime_cleanup']
        expected_roots = 2 if r['retry']['code'] == 'OK' else 1
        if (len(cleanups) != expected_roots or len({c['root'] for c in cleanups}) != expected_roots or
                any(c['remaining_mounts'] != [] or c['containers'] not in (None, []) or
                    not isinstance(c['unmounted'], list) for c in cleanups)):
            raise ValueError('runtime cleanup receipts incomplete')
        require_runtime(r)
        require_inventory(r['before_retry'])
        require_inventory(r['after_retry'])
        if not isinstance(r['startup_state'], dict) or 'startup_disk' not in r:
            raise ValueError('replacement startup observation missing')
        if case != 'uninterrupted':
            require_inventory(r['at_fault'])
            fault = r['fault']
            if (r['atelet_pid'] == r['replacement_pid'] or r['kill_returncode'] != -9 or
                    fault['token'] != r['token'] or fault['pid'] != r['atelet_pid'] or
                    fault['point'] != case or fault['actor_uid'] != r['actor_uid']):
                raise ValueError('wrong kill/boundary identity')
        elif r['fault'] is not None or r['kill_returncode'] is not None:
            raise ValueError('uninterrupted control was interrupted')
        if case == 'tracker-committed':
            if (not snapshot_matches(r['disk_at_fault'], r['snapshot'], disk=True) or
                    not snapshot_matches(r['startup_state'], r['snapshot']) or
                    not snapshot_matches(r['startup_disk'], r['snapshot'], disk=True)):
                raise ValueError('persisted commit missing before kill or at replacement startup')
        calls = r['worker_results']
        if not calls or len(calls) != r['worker_calls'] or calls[0]['code'] != 'OK':
            raise ValueError('initial real worker capture not established')
        for number, call in enumerate(calls, 1):
            if (call['call'] != number or call['pid'] != r['worker_pid'] or call['token'] != r['token'] or
                    call['request']['actor_uid'] != r['actor_uid'] or
                    call['method'] != '/ateworker.Worker/CheckpointWorkload'):
                raise ValueError('worker call identity mismatch')
        if r['successful_captures'] < 1:
            raise ValueError('no successful runtime capture')
        retry = r['retry']
        if retry['code'] != 'OK':
            if r['worker_calls'] != 2 or calls[-1]['code'] == 'OK':
                raise ValueError('retry failed outside the observed worker path')
            if case not in FAULTS:
                raise ValueError('recovery control failed')
            return result('findings', 'real worker rejected the retry; inspect inventories for file retention')
        committed = snapshot_matches(r['tracker_after'], r['snapshot']) and snapshot_matches(r['disk_after'], r['snapshot'], disk=True)
        expected_calls = 1 if case in ('tracker-committed', 'uninterrupted') else 2
        before, after = r['guest_before'], r['guest_restored']
        restored = (before['count'] == 2 and after['count'] == 3 and
                    before['boot'] == after['boot'] and before['memory_sha256'] == after['memory_sha256'])
        recovered = (committed and restored and r['capture_attempts'] == 1 and
                     r['successful_captures'] == 1 and r['worker_calls'] == expected_calls and
                     all(c['code'] == 'OK' for c in calls))
        if not recovered:
            return result('findings' if case in FAULTS else 'inconclusive', 'retry response did not establish the recovery invariant')
        return result('pass', 'same snapshot committed and memory witness restored after one capture')
    except (KeyError, TypeError, ValueError) as exc:
        return result('inconclusive', str(exc))


def summarize(records):
    expected = {(case, n) for case in CASES for n in range(1, 4)}
    results = [classify(r) for r in records]
    keys = [(r.get('case'), r.get('iteration')) for r in records]
    tokens = [r.get('token') for r in records]
    complete = (len(keys) == len(expected) and set(keys) == expected and
                len(set(tokens)) == len(tokens) and all(tokens))
    outcome = 'inconclusive'
    if complete and all(r['outcome'] != 'inconclusive' for r in results):
        outcome = 'findings' if any(r['outcome'] == 'findings' for r in results) else 'pass'
    return {'outcome': outcome, 'complete': complete, 'trials': results}
