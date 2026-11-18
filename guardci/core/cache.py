"""Content-addressed stage cache (DESIGN.md §9.3).

``cache/<key>/`` holds the flow's stage snapshot plus ``meta.json``
(``stage``, ``status: ok|failed``, ``seconds``, ``rc``, ``sha``). Entries
are published by atomic rename; a per-key lock file makes concurrent
jobs wait instead of building twice. The root is a project setting and
may be shared between projects: keys contain content only.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Entry:
    key: str
    stage: str
    status: str                     # ok | failed
    seconds: float = 0.0
    rc: int | None = None
    sha: str | None = None
    dir: Path | None = None         # entry root; snapshot under "snapshot/"
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def snapshot(self) -> Path:
        return self.dir / "snapshot"


class Cache:
    def __init__(self, root):
        self.root = Path(root)
        (self.root / "keys").mkdir(parents=True, exist_ok=True)
        (self.root / "locks").mkdir(parents=True, exist_ok=True)

    def entry_dir(self, key: str) -> Path:
        return self.root / "keys" / key

    def lock_path(self, key: str) -> Path:
        return self.root / "locks" / f"{key}.lock"

    @contextlib.contextmanager
    def lock(self, key: str):
        """Exclusive flock on a key — held across lookup+build+publish."""
        with open(self.lock_path(key), "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # -------------------------------------------------------- golden area

    def golden_dir(self, name: str) -> Path:
        """Golden area for one baseline — keyed by the baseline's *tree*
        sha (LibRefs and the golden stream merge depend on the whole
        baseline tree, not just the protected set)."""
        return self.root / "golden" / name

    @contextlib.contextmanager
    def golden_lock(self, name: str):
        """Exclusive flock for populating a golden dir."""
        gdir = self.root / "golden"
        gdir.mkdir(parents=True, exist_ok=True)
        with open(gdir / f"{name}.lock", "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # ---------------------------------------------------- result memos

    def results_dir(self) -> Path:
        d = self.root / "results"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------------------------------------------------------- index

    def index_dir(self) -> Path:
        d = self.root / "index"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def index_get(self, key: str) -> dict | None:
        """§11.4 index entry ``cache/index/<key>.json`` — survives
        entry eviction (gc only walks ``keys/``)."""
        p = self.index_dir() / f"{key}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (ValueError, OSError):
            return None

    def index_put(self, key: str, meta: dict):
        """Merge-write an index entry (stage, output_digest, status,
        seconds, layout_sha256?, netlist_sha256?)."""
        cur = self.index_get(key) or {}
        cur.update({k: v for k, v in meta.items() if v is not None})
        p = self.index_dir() / f"{key}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, indent=2, sort_keys=True))
        tmp.rename(p)

    # ------------------------------------------------------------ gc

    def _touch(self, entry_dir: Path):
        try:
            os.utime(entry_dir / "last_used", None)
        except OSError:
            try:
                (entry_dir / "last_used").touch()
            except OSError:
                pass

    def lookup(self, key: str) -> Entry | None:
        d = self.entry_dir(key)
        meta_path = d / "meta.json"
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text())
        except (ValueError, OSError):
            return None
        self._touch(d)
        return Entry(key=key, stage=meta.get("stage", ""),
                     status=meta.get("status", "failed"),
                     seconds=meta.get("seconds", 0.0),
                     rc=meta.get("rc"), sha=meta.get("sha"), dir=d,
                     meta=meta)

    def publish(self, key: str, populate_fn, meta: dict):
        """populate_fn(tmp_dir) builds the entry content; meta.json is
        written on top; the tmp dir is atomically renamed into place."""
        tmp = Path(tempfile.mkdtemp(prefix=".pub-", dir=self.root / "keys"))
        try:
            populate_fn(tmp)
            (tmp / "meta.json").write_text(json.dumps(
                {"key": key, **meta}, indent=2, sort_keys=True))
            self._touch(tmp)
            dest = self.entry_dir(key)
            try:
                os.rename(tmp, dest)
            except OSError:
                # destination appeared concurrently: keep the first
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
        except BaseException:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    @contextlib.contextmanager
    def try_lock(self, key: str):
        """Non-blocking variant of lock(); yields False when busy."""
        with open(self.lock_path(key), "a+") as fh:
            yield from self._try_locked(fh)

    @staticmethod
    def _try_locked(fh):
        try:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                ok = True
            except OSError:
                ok = False
            try:
                yield ok
            finally:
                if ok:
                    fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()

    def _entry_bytes(self) -> tuple[int, dict]:
        """(total bytes, {key: {inode: size}}) over stage entries —
        inodes shared between entries (hard-linked snapshots) count
        once."""
        per_key: dict[str, dict] = {}
        for d in sorted((self.root / "keys").iterdir()):
            if not d.is_dir() or d.name.startswith(".pub-"):
                continue
            inos = {}
            for root, _dirs, files in os.walk(d):
                for name in files:
                    try:
                        st = os.stat(Path(root) / name)
                    except OSError:
                        continue
                    inos.setdefault((st.st_dev, st.st_ino), st.st_size)
            per_key[d.name] = inos
        seen: dict = {}
        for inos in per_key.values():
            seen.update(inos)
        return sum(seen.values()), per_key

    def gc(self, max_bytes: int):
        """LRU eviction of stage entries (§9.10).

        Stage entries only — ``golden/`` and ``results/`` are never
        touched. Entries whose key lock is held by a running job are
        skipped. Hard-linked files count once per unique inode.
        """
        import shutil
        keys_root = self.root / "keys"
        if not keys_root.is_dir():
            return
        total, per_key = self._entry_bytes()

        def last_used(key):
            stamp = keys_root / key / "last_used"
            meta = keys_root / key / "meta.json"
            for p in (stamp, meta):
                try:
                    return p.stat().st_mtime
                except OSError:
                    continue
            return 0.0

        for key in sorted(per_key, key=last_used):
            if total <= max_bytes:
                break
            with self.try_lock(key) as got:
                if not got:
                    continue
                shutil.rmtree(keys_root / key, ignore_errors=True)
            # bytes are freed only by inodes no remaining entry shares
            freed = sum(
                sz for ino, sz in per_key[key].items()
                if not any(ino in i for k, i in per_key.items()
                           if k != key))
            per_key.pop(key)
            total -= freed
