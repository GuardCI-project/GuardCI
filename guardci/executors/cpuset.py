"""Cross-process allocator of disjoint host CPU blocks via flock lock
files — port of ppa-hack-bench ``cpus.py`` (DESIGN.md §8.2)."""

from __future__ import annotations

import fcntl
import os
import time

DEFAULT_LOCK_DIR = "/tmp/guardci-cpusets"


class Allocation:
    def __init__(self, cpus, handles, paths):
        self.cpus = cpus
        self._handles = handles
        self.paths = paths

    def release(self):
        # Lock files are intentionally left on disk: unlinking while
        # another process has one open would let two processes flock
        # different inodes for the same CPU.
        for handle in self._handles:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()
        return False


def _blocks(affinity, block_size):
    cpus = sorted(affinity)
    return [cpus[i:i + block_size]
            for i in range(0, len(cpus) - block_size + 1, block_size)]


def _try_block(block, lock_dir):
    """Acquire flock on every CPU of ``block``; Allocation or None."""
    handles = []
    paths = []
    for cpu in block:
        path = os.path.join(lock_dir, f"cpu-{cpu}")
        handle = open(path, "a+")  # noqa: SIM115 - kept open for flock
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # refresh mtime so tmpfiles age-based cleanup of /tmp
            # never unlinks a lock file that is in use
            os.utime(path, None)
            handles.append(handle)
            paths.append(path)
        except OSError:
            handle.close()
            for h in handles:
                fcntl.flock(h, fcntl.LOCK_UN)
                h.close()
            return None
    return Allocation(block, handles, paths)


def try_acquire(block_size, affinity=None, lock_dir=DEFAULT_LOCK_DIR):
    """Non-blocking variant of acquire(): Allocation or None."""
    if affinity is None:
        affinity = os.sched_getaffinity(0)
    blocks = _blocks(affinity, block_size)
    if not blocks:
        raise RuntimeError(f"no CPU block of size {block_size} in affinity "
                           f"{sorted(affinity)}")
    os.makedirs(lock_dir, exist_ok=True)
    return _try_block(blocks[0], lock_dir) or _first_free(blocks, lock_dir)


def _first_free(blocks, lock_dir):
    for block in blocks[1:]:
        alloc = _try_block(block, lock_dir)
        if alloc is not None:
            return alloc
    return None


def acquire(block_size, poll_seconds=5, log=None, affinity=None,
            lock_dir=DEFAULT_LOCK_DIR):
    """Return an Allocation for the first free contiguous block.

    Blocks are contiguous ranges of the allowed host CPUs; ownership is a
    flock'd lock file so it survives across processes and frees on exit."""
    if affinity is None:
        affinity = os.sched_getaffinity(0)
    blocks = _blocks(affinity, block_size)
    if not blocks:
        raise RuntimeError(f"no CPU block of size {block_size} in affinity "
                           f"{sorted(affinity)}")
    os.makedirs(lock_dir, exist_ok=True)
    while True:
        for block in blocks:
            alloc = _try_block(block, lock_dir)
            if alloc is not None:
                return alloc
        if log:
            log(f"cpus: all {len(blocks)} blocks of size {block_size} "
                f"busy; retry in {poll_seconds}s")
        time.sleep(poll_seconds)
