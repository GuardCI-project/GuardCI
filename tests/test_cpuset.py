"""CPU slot pool: disjoint blocks, non-blocking acquire."""

from __future__ import annotations

from guardci.executors import cpuset


def test_disjoint_blocks(tmp_path):
    a = cpuset.acquire(2, affinity={0, 1, 2, 3},
                       lock_dir=str(tmp_path))
    b = cpuset.acquire(2, affinity={0, 1, 2, 3},
                       lock_dir=str(tmp_path))
    assert set(a.cpus).isdisjoint(b.cpus)
    a.release()
    b.release()


def test_try_acquire_none_when_full(tmp_path):
    a = cpuset.acquire(2, affinity={0, 1, 2, 3}, lock_dir=str(tmp_path))
    b = cpuset.acquire(2, affinity={0, 1, 2, 3}, lock_dir=str(tmp_path))
    try:
        assert cpuset.try_acquire(2, affinity={0, 1, 2, 3},
                                  lock_dir=str(tmp_path)) is None
    finally:
        a.release()
        b.release()


def test_release_frees_block(tmp_path):
    a = cpuset.acquire(4, affinity={0, 1, 2, 3}, lock_dir=str(tmp_path))
    a.release()
    b = cpuset.try_acquire(4, affinity={0, 1, 2, 3},
                           lock_dir=str(tmp_path))
    assert b is not None and b.cpus == a.cpus
    b.release()
