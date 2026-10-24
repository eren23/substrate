"""Disk gates and cancellation limited to a newly created process group."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

GIB = 1024 ** 3
STOP_GRACE = 10


class ResourceBlocked(RuntimeError):
    pass


class DiskBudget:
    def __init__(self, path, *, free=None):
        self.path = Path(path)
        self.free = free or (lambda path: shutil.disk_usage(path).free)
        self.samples = []

    def observe(self):
        value = self.free(self.path)
        self.samples.append({"monotonic_s": time.monotonic(), "free_bytes": value})
        return value

    def require(self, gib, purpose):
        value = self.observe()
        if value < gib * GIB:
            raise ResourceBlocked(f"disk: {purpose} requires {gib} GiB free; available {value / GIB:.2f} GiB")

    def start(self): self.require(20, "starting heavy work")
    def schedule(self): self.require(10, "scheduling more work")
    def running(self): self.require(5, "continuing owned heavy work")


def stop_owned(process):
    """Only call for Popen(start_new_session=True) created by this module."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + STOP_GRACE
    while time.monotonic() < deadline:
        process.poll()  # reap the leader; its descendants may still own the group
        try: os.killpg(process.pid, 0)
        except ProcessLookupError: return
        time.sleep(.02)
    # A leader exit is not proof that its process group stopped.
    try: os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError: pass
    process.wait(timeout=10)


def guarded(argv, log, *, cwd=None, env=None, timeout=900, heavy=False, budget=None, poll=.25):
    budget = budget or DiskBudget(Path(log).parent)
    if heavy: budget.start()
    with open(log, "xb", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        process = subprocess.Popen([str(a) for a in argv], cwd=cwd, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if heavy: budget.running()
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(argv, timeout)
                time.sleep(poll)
            return process.returncode
        except BaseException:
            stop_owned(process)
            raise
