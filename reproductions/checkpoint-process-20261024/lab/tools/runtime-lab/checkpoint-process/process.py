"""Own child processes and consume authenticated, bounded JSON event streams."""
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time


class Child:
    def __init__(self, command, log, env, token):
        self.token = token
        self.events = queue.Queue()
        self.seen = set()
        self.log = Path(log).open('w')
        self.proc = subprocess.Popen(list(map(str, command)), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1, env={**os.environ, **env},
                                     start_new_session=True)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                self.log.write(line)
                self.log.flush()
                if line.startswith('CP_EVENT '):
                    try:
                        self.events.put(json.loads(line[len('CP_EVENT '):]))
                    except ValueError as exc:
                        self.events.put(exc)
        finally:
            self.events.put(None)

    def event(self, name, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                event = self.events.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError('missing child event: ' + name) from None
            if event is None:
                raise RuntimeError('child exited before event: ' + name)
            if isinstance(event, Exception):
                raise ValueError('malformed child event') from event
            if event.get('token') != self.token or event.get('pid') != self.proc.pid:
                raise ValueError('child event identity mismatch')
            key = (event.get('event'), event.get('id', ''))
            if key in self.seen:
                raise ValueError('duplicate child event')
            self.seen.add(key)
            if event.get('event') != name:
                raise ValueError('unexpected child event: ' + str(event))
            return event

    def send(self, value):
        if self.proc.poll() is not None:
            raise RuntimeError('child already exited')
        self.proc.stdin.write(json.dumps(value) + '\n')
        self.proc.stdin.flush()

    def kill_at(self, event, point, actor_uid):
        if (event.get('pid') != self.proc.pid or event.get('token') != self.token or
                event.get('event') != 'boundary' or event.get('point') != point or
                event.get('actor_uid') != actor_uid or self.proc.poll() is not None):
            raise ValueError('refusing unverified process kill')
        self.proc.kill()
        return self.proc.wait(timeout=10)

    def _live_group(self):
        # A zombie has exited but may await init's reap after its parent died.
        # Inspect only PID/group/status, never other processes' command lines.
        rows = subprocess.check_output(['ps', '-axo', 'pid=,pgid=,stat='], text=True)
        return [int(pid) for pid, group, status in (line.split() for line in rows.splitlines())
                if int(group) == self.proc.pid and not status.startswith('Z')]

    def close(self):
        # Pipes and the leader's exit do not establish descendant cleanup.
        # Every Child starts its own session/process group; always stop it.
        def stop(sig):
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                # macOS can report EPERM for an already exited group.
                if self.proc.poll() is None:
                    raise
        self.proc.poll()
        try:
            stop(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stop(signal.SIGKILL)
                self.proc.wait(timeout=5)
            if self._live_group():
                stop(signal.SIGKILL)
            deadline = time.monotonic() + 5
            while self._live_group():
                if time.monotonic() >= deadline:
                    raise RuntimeError('owned process group still has live descendants')
                time.sleep(.02)
        finally:
            # Verification itself may fail (for example denied ps access).
            # Still stop owned descendants; callers must mark that inconclusive.
            stop(signal.SIGKILL)
            self.proc.wait(timeout=5)
            self.reader.join(timeout=5)
            self.proc.stdin.close()
            if not self.reader.is_alive():
                self.proc.stdout.close()
                self.log.close()
        if self.reader.is_alive():
            raise RuntimeError('child output remained open after group cleanup')
        return self.proc.returncode
