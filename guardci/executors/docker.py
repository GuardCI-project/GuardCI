"""Docker executor: one-shot ``docker run`` per command on an exclusive
CPU-set slot (DESIGN.md §9.9).

Containers are named ``guardci-*`` and run ``--rm --network none`` as the
invoking uid:gid so bind-mounted outputs stay owned by the user.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

from . import cpuset
from .base import Executor, RunResult, Slot, register


@register
class DockerExecutor(Executor):
    type = "docker"

    def __init__(self, image: str, slots: int = 1, cpus_per_slot: int = 16,
                 memory: str = "64g", lock_dir: str = cpuset.DEFAULT_LOCK_DIR,
                 pids_limit: int = 4096, affinity=None):
        self.image = image
        self._slots = int(slots)
        self.cpus_per_slot = int(cpus_per_slot)
        self.memory = memory
        self.lock_dir = lock_dir
        self.pids_limit = int(pids_limit)
        self._affinity = affinity
        self._image_id = None

    # ------------------------------------------------------------- slots

    def slots(self) -> int:
        return self._slots

    def _wrap(self, alloc) -> Slot:
        return Slot(cpus=list(alloc.cpus), _release=alloc.release)

    def acquire(self, poll_seconds: float = 5.0) -> Slot:
        alloc = cpuset.acquire(self.cpus_per_slot,
                               poll_seconds=poll_seconds,
                               affinity=self._affinity,
                               lock_dir=self.lock_dir)
        return self._wrap(alloc)

    def try_acquire(self) -> Slot | None:
        alloc = cpuset.try_acquire(self.cpus_per_slot,
                                   affinity=self._affinity,
                                   lock_dir=self.lock_dir)
        return self._wrap(alloc) if alloc is not None else None

    # ------------------------------------------------------------- image

    def image_id(self) -> str:
        if self._image_id is None:
            out = subprocess.run(
                ["docker", "image", "inspect", self.image,
                 "--format", "{{.Id}}"],
                capture_output=True, check=True, text=True).stdout.strip()
            self._image_id = out
        return self._image_id

    # ---------------------------------------------------------------- run

    def run(self, cmd, *, mounts=None, workdir=None, env=None, slot=None,
            timeout=None, log=None) -> RunResult:
        name = f"guardci-{uuid.uuid4().hex[:16]}"
        args = ["docker", "run", "--rm", "--name", name,
                "--network", "none",
                "--cpus", str(self.cpus_per_slot),
                "--memory", self.memory,
                "--pids-limit", str(self.pids_limit),
                "--user", f"{os.getuid()}:{os.getgid()}"]
        cpus = getattr(slot, "cpus", None)
        if cpus:
            args += ["--cpuset-cpus", ",".join(str(c) for c in cpus)]
        merged_env = {"HOME": "/tmp/home"}
        merged_env.update(env or {})
        for k, v in merged_env.items():
            args += ["-e", f"{k}={v}"]
        for host, spec in (mounts or {}).items():
            cpath, mode = spec
            args += ["-v", f"{host}:{cpath}:{mode}"]
        if workdir:
            args += ["-w", workdir]
        args += [self.image, *cmd]

        log_fh = None
        try:
            if log is not None and not hasattr(log, "write"):
                Path(log).parent.mkdir(parents=True, exist_ok=True)
                log_fh = open(log, "wb")  # noqa: SIM115 - closed in finally
            sink = log_fh or log
            t0 = time.monotonic()
            try:
                proc = subprocess.run(
                    args, stdout=sink or subprocess.DEVNULL,
                    stderr=subprocess.STDOUT, timeout=timeout,
                    check=False)
                return RunResult(rc=proc.returncode,
                                 seconds=self._elapsed(t0))
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "rm", "-f", name],
                               capture_output=True, check=False)
                return RunResult(rc=None, seconds=self._elapsed(t0),
                                 timed_out=True)
        finally:
            if log_fh is not None:
                log_fh.close()
