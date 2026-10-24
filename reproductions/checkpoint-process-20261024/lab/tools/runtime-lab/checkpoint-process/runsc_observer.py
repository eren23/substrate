#!/usr/bin/env python3
"""Record real runsc invocations without replacing checkpoint or cleanup behavior."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def main():
    root = Path(os.environ['CP_TRIAL_ROOT'])
    operation = next((arg for arg in sys.argv[1:] if arg in
                      {'checkpoint', 'fscheckpoint', 'delete', 'state', 'list', 'kill', 'wait'}), '')
    record = {'id': str(uuid.uuid4()), 'operation': operation, 'args': sys.argv[1:],
              'token': os.environ['CP_TOKEN'], 'observer_pid': os.getpid()}
    def emit(event, **fields):
        with (root / 'runtime.jsonl').open('a') as stream:
            stream.write(json.dumps({**record, 'event': event, 'time': time.time(), **fields}) + '\n')
    emit('start')
    child = subprocess.Popen([os.environ['CP_RUNTIME'], *sys.argv[1:]])
    try:
        code = child.wait(timeout=60)
    except BaseException:
        child.kill()
        child.wait(timeout=5)
        emit('interrupted', returncode=child.returncode)
        raise
    emit('end', returncode=code)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
