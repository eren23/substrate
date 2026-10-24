"""One bounded same-boot process restart with a persistent real worker."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

from evidence import snapshot_matches
from cleanup import unmount_runtime
from prepare import digest
from process import Child

HERE = Path(__file__).resolve().parent
FLAGS = ['--ignore-cgroups', '--platform=systrap', '--network=none']


def save(path, data):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    temp.replace(path)


def read_optional(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None


def jsonlines(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def inventory(root):
    return {str(p.relative_to(root)): {'bytes': p.stat().st_size, 'sha256': digest(p)}
            for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()}


def trial(case, iteration, binaries, runtime, guest, artifacts, env):
    artifacts.mkdir(mode=0o700, parents=True, exist_ok=False)
    root = Path(tempfile.mkdtemp(prefix='cp-'))
    token, actor, snapshot_id, worker_uid = [str(uuid.uuid4()) for _ in range(4)]
    request = {'atespace': 'ate-demo', 'actor_name': 'counter', 'actor_uid': actor,
               'actor_template_atespace': 'ate-demo', 'actor_template_name': 'counter',
               'worker_pod_uid': worker_uid, 'spec': {},
               'snapshot': {'snapshot_uuid': snapshot_id, 'generation': 1, 'fidelity': 3,
                            'object': {'snapshot_uri': f'gs://test-bucket/atespaces/ate-demo/actors/{actor}/snapshots/{snapshot_id}'}},
               'upload_config': {'mode': 1}}
    # Pinned atelet proto: UPLOAD_MODE_DEFERRED = 1; MEMORY fidelity = 3.
    save(root / 'request.json', request)
    cfg = {'root': str(root), 'token': token, 'actor_uid': actor, 'request': str(root / 'request.json'),
           'worker_socket': str(root / 'worker.sock'), 'atelet_socket': str(root / 'atelet.sock'),
           'services_socket': str(root / 'services.sock'), 'asset_sha256': digest(HERE / 'runsc_observer.py')}
    save(root / 'config.json', cfg)
    save(artifacts / 'configuration.json', cfg)
    save(artifacts / 'request.json', request)
    child_env = {**env, 'CP_CONFIG': str(root / 'config.json'), 'CP_TOKEN': token,
                 'CP_TRIAL_ROOT': str(root), 'CP_RUNTIME': str(runtime)}
    record = {'case': case, 'iteration': iteration, 'token': token, 'actor_uid': actor,
              'snapshot': request['snapshot'], 'request_digest': digest(root / 'request.json'),
              'cleanup_verified': False, 'fault': None, 'kill_returncode': None}
    children = []
    runtime_roots = []
    started = time.monotonic()

    def remaining(limit=60):
        left = 180 - (time.monotonic() - started)
        if left <= 0:
            raise TimeoutError('trial observation exceeded 180 seconds')
        return min(limit, left)

    def child(binary, test, name, extra=None):
        item = Child([binary, '-test.run=^' + test + '$', '-test.v', '-test.timeout=4m'],
                     artifacts / (name + '.log'), {**child_env, **(extra or {})}, token)
        item.role = name
        children.append(item)
        return item

    def command(argv, name, check=True, timeout=None):
        with (artifacts / (name + '.stdout')).open('w') as stdout, (artifacts / (name + '.stderr')).open('w') as stderr:
            result = subprocess.run(list(map(str, argv)), stdout=stdout, stderr=stderr,
                                    env={**os.environ, **env}, timeout=timeout or remaining())
        if check and result.returncode:
            raise RuntimeError(name + ' exited ' + str(result.returncode))
        return result.returncode

    def ask(control, number):
        (control / 'request').write_text(str(number))
        deadline = time.monotonic() + remaining(15)
        while time.monotonic() < deadline:
            value = read_optional(control / 'response')
            if value and value.get('request') == str(number):
                return value
            time.sleep(.02)
        raise TimeoutError('guest response missing')

    def observe(layout):
        return {'files': inventory(Path(layout['checkpoint_dir']).parent),
                'disk': read_optional(state_path)}

    try:
        initializer = child(binaries['atelet'], 'TestProcessInitialize', 'initialize')
        init = initializer.event('initialized', remaining(15))
        if initializer.proc.wait(timeout=remaining(10)) != 0:
            raise RuntimeError('one-time metadata initialization failed')
        layout = init['actor_dirs']
        state_path = Path(init['state_path'])
        local = Path(init['local_dir'])
        record['initial_state'] = read_optional(state_path)
        if record['initial_state'] is not None:
            raise ValueError('fresh actor unexpectedly has tracker state')

        assets = Path(layout['sandbox_assets_dir']) / 'gvisor'
        assets.mkdir(parents=True)
        shutil.copy2(HERE / 'runsc_observer.py', assets / 'runsc')
        (assets / 'runsc').chmod(0o755)
        # Both the original and restored OCI config name the same backing paths.
        fixture = root / 'guest-fixture'
        rootfs, control = fixture / 'rootfs', fixture / 'control'
        for path in [rootfs / 'control', rootfs / 'proc', rootfs / 'dev', control]:
            path.mkdir(parents=True, exist_ok=True)
        shutil.copy2(guest, rootfs / 'guest')
        spec = {'ociVersion': '1.0.2', 'root': {'path': str(rootfs), 'readonly': False},
                'process': {'terminal': False, 'user': {'uid': 0, 'gid': 0}, 'args': ['/guest'],
                            'cwd': '/', 'env': ['PATH=/bin']},
                'mounts': [{'destination': '/proc', 'type': 'proc', 'source': 'proc'},
                           {'destination': '/dev', 'type': 'tmpfs', 'source': 'tmpfs'},
                           {'destination': '/control', 'type': 'bind', 'source': str(control), 'options': ['rbind', 'rw']}],
                'linux': {'namespaces': [{'type': x} for x in ['pid', 'ipc', 'uts', 'mount']]}}
        bundle = Path(layout['oci_bundle_dir']) / '_sandbox'
        bundle.mkdir(parents=True)
        save(bundle / 'config.json', spec)
        save(fixture / 'config.json', spec)
        save(artifacts / 'oci.json', spec)
        runtime_root = Path(layout['root_dir']) / 'runsc-state'
        runtime_roots.append(runtime_root)
        pidfiles = Path(layout['root_dir']) / 'pidfiles'
        pidfiles.mkdir()
        services = child(binaries['atelet'], 'TestProcessServices', 'services')
        services.event('ready', remaining(15))
        worker = child(binaries['ateworker-gvisor'], 'TestProcessWorker', 'worker')
        worker.event('ready', remaining(15))
        record['worker_pid'] = worker.proc.pid
        atelet = child(binaries['atelet'], 'TestProcessAtelet', 'atelet-first',
                       {'CP_INITIAL': '1', 'CP_FAULT': '' if case == 'uninterrupted' else case})
        ready = atelet.event('ready', remaining(15))
        record['atelet_pid'], record['boot_id'] = atelet.proc.pid, ready['boot_id']
        if ready['disk'] is not None or not ready['startup_complete']:
            raise ValueError('initial atelet state is not fresh')
        command([runtime, *FLAGS, '--root=' + str(runtime_root), 'run', '--detach',
                 '--pid-file=' + str(pidfiles / '_sandbox.pid'), '--bundle=' + str(bundle), '_sandbox'], 'guest-start')
        if ask(control, 1)['count'] != 1:
            raise ValueError('unexpected initial guest counter')
        record['guest_before'] = ask(control, 2)
        first = child(binaries['atelet'], 'TestProcessClient', 'first-client')
        if case != 'uninterrupted':
            boundary = atelet.event('boundary', remaining())
            record['fault'] = boundary
            record['at_fault'] = observe(layout)
            record['disk_at_fault'] = record['at_fault']['disk']
            if case == 'tracker-committed' and not snapshot_matches(record['disk_at_fault'], request['snapshot'], disk=True):
                raise ValueError('committed control did not persist the exact snapshot')
            first_calls = jsonlines(root / 'worker-calls.jsonl')
            if len(first_calls) != 1 or first_calls[0]['code'] != 'OK':
                raise ValueError('fault was not after a successful real worker capture')
            files = first_calls[0]['response']['snapshot_files']
            checkpoint = Path(layout['checkpoint_dir'])
            if case == 'first-stage':
                staged = [f for f in files if (checkpoint / '.staged' / f).is_file()]
                original = [f for f in files if (checkpoint / f).is_file()]
                if len(staged) != 1 or not original or len(staged) + len(original) != len(files):
                    raise ValueError('first-stage split boundary was not established')
            elif case in ('local-publication', 'tracker-committed'):
                if not all((local / f).is_file() for f in files) or not (local / 'manifest.json').is_file():
                    raise ValueError('published payload is incomplete')
            elif not all((checkpoint / f).is_file() for f in files):
                raise ValueError('worker replay control lost its source files')
            record['kill_returncode'] = atelet.kill_at(boundary, case, actor)
            record['first_rpc'] = first.event('response', remaining(10))
            if first.proc.wait(timeout=remaining(10)) != 0:
                raise RuntimeError('first RPC observer failed')
            Path(cfg['atelet_socket']).unlink(missing_ok=True)
            replacement = child(binaries['atelet'], 'TestProcessAtelet', 'atelet-replacement',
                                {'CP_INITIAL': '0', 'CP_FAULT': ''})
            restarted = replacement.event('ready', remaining(15))
        else:
            record['first_rpc'] = first.event('response', remaining())
            if first.proc.wait(timeout=remaining(10)) != 0 or record['first_rpc']['code'] != 'OK':
                raise RuntimeError('uninterrupted checkpoint failed')
            replacement, restarted = atelet, ready
        record.update(replacement_pid=replacement.proc.pid, replacement_boot_id=restarted['boot_id'],
                      startup_complete=restarted['startup_complete'], startup_state=restarted.get('tracker'),
                      startup_disk=restarted.get('disk'), before_retry=observe(layout))
        record['retry_request_digest'] = digest(root / 'request.json')
        retry = child(binaries['atelet'], 'TestProcessClient', 'retry-client')
        record['retry'] = retry.event('response', remaining())
        if retry.proc.wait(timeout=remaining(10)) != 0:
            raise RuntimeError('retry observer failed')
        replacement.send({'op': 'state', 'id': 'after-retry'})
        state = replacement.event('state', remaining(10))
        record['tracker_after'], record['disk_after'] = state['tracker'], read_optional(state_path)
        record['after_retry'] = observe(layout)
        if replacement.proc.poll() is not None or worker.proc.poll() is not None or services.proc.poll() is not None:
            raise RuntimeError('a retained service exited during observation')
        record['worker_pid_after'] = worker.proc.pid
        record['worker_results'] = jsonlines(root / 'worker-calls.jsonl')
        record['worker_calls'] = len(record['worker_results'])
        commands = jsonlines(root / 'runtime.jsonl')
        record['runtime_records'] = commands
        if any(c['event'] == 'interrupted' for c in commands):
            raise RuntimeError('runtime observer was interrupted')
        record['capture_attempts'] = sum(c['event'] == 'start' and c['operation'] == 'checkpoint' for c in commands)
        record['successful_captures'] = sum(c['event'] == 'end' and c['operation'] == 'checkpoint' and c['returncode'] == 0 for c in commands)
        if record['retry']['code'] == 'OK':
            restore = root / 'restore-witness'
            shutil.copytree(local, restore / 'images')
            (restore / 'bundle').mkdir()
            save(restore / 'bundle/config.json', spec)
            restore_root = restore / 'runsc-state'
            runtime_roots.append(restore_root)
            command([runtime, *FLAGS, '--root=' + str(restore_root), 'restore', '--detach',
                     '--bundle=' + str(restore / 'bundle'), '--image-path=' + str(restore / 'images'), '_sandbox'], 'restore-witness')
            record['guest_restored'] = ask(control, 3)
        record['elapsed_seconds'] = time.monotonic() - started
    except (Exception, KeyboardInterrupt) as exc:
        record['error'] = type(exc).__name__ + ': ' + str(exc)
        record['elapsed_seconds'] = time.monotonic() - started
    finally:
        cleanup_errors = []
        record['child_exits'] = []
        record['runtime_cleanup'] = []
        # Stop RPC sources before deleting any runtime, including on timeout.
        for item in reversed(children):
            verified = False
            try:
                item.close()
                verified = True
                expected = -9 if item.role == 'atelet-first' and case != 'uninterrupted' and record['kill_returncode'] == -9 else 0
                if item.proc.returncode != expected:
                    cleanup_errors.append(item.role + ' unexpected exit ' + str(item.proc.returncode))
            except Exception as exc:
                cleanup_errors.append(str(exc))
            record['child_exits'].append({'role': item.role, 'pid': item.proc.pid,
                                          'returncode': item.proc.poll(), 'group_verified': verified})
        for index, runtime_root in enumerate(runtime_roots):
            try:
                command([runtime, *FLAGS, '--root=' + str(runtime_root), 'delete', '--force', '_sandbox'],
                        'cleanup-' + str(index), check=False, timeout=20)
                command([runtime, *FLAGS, '--root=' + str(runtime_root), 'list', '--format=json'],
                        'cleanup-list-' + str(index), timeout=10)
                contents = json.loads((artifacts / ('cleanup-list-' + str(index) + '.stdout')).read_text())
                if contents not in (None, []):
                    raise RuntimeError('owned runtime still has sandboxes')
                if runtime_root == root or not runtime_root.is_relative_to(root):
                    raise ValueError('runtime cleanup escaped its owned trial root')
                def unmount(point):
                    command(['umount', '--', point], 'cleanup-mount-' + str(index) + '-' + str(len(mount_calls)), timeout=20)
                    mount_calls.append(point)
                mount_calls = []
                receipt = unmount_runtime(runtime_root, unmount,
                                          processes_stopped=all(e['group_verified'] for e in record['child_exits']))
                receipt['containers'] = contents
                record['runtime_cleanup'].append(receipt)
            except Exception as exc:
                cleanup_errors.append(str(exc))
        for name in ('worker-calls.jsonl', 'runtime.jsonl', 'reports.jsonl'):
            if (root / name).exists():
                shutil.copy2(root / name, artifacts / name)
        record['cleanup_errors'] = cleanup_errors
        record['cleanup_verified'] = not cleanup_errors and all(c.proc.poll() is not None for c in children)
        if record['cleanup_verified']:
            try:
                shutil.rmtree(root)
            except OSError as exc:
                record['cleanup_verified'] = False
                record['cleanup_errors'].append(str(exc))
        save(artifacts / 'result.json', record)
    return record
