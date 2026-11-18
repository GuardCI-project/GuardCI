"""SQLite store: the internal truth of the ledger (DESIGN.md §8.1)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .models import CheckRun, Clause, Node, Severity, State, Verdict

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS nodes (
    sha TEXT PRIMARY KEY,
    parent TEXT,
    state TEXT NOT NULL,
    delta_tags TEXT NOT NULL DEFAULT '[]',
    metrics TEXT NOT NULL DEFAULT '{}',
    baseline_id INTEGER,
    requested_full INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS check_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    checker TEXT NOT NULL,
    backend TEXT,
    cost TEXT,
    status TEXT NOT NULL,
    seconds REAL,
    metrics TEXT NOT NULL DEFAULT '{}',
    evidence TEXT NOT NULL DEFAULT '[]',
    error TEXT
);
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    clause TEXT NOT NULL,
    severity TEXT,
    status TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha TEXT NOT NULL,
    upto_stage TEXT NOT NULL,
    measurements TEXT NOT NULL DEFAULT '[]',
    params TEXT NOT NULL DEFAULT '{}',
    clauses TEXT NOT NULL DEFAULT '[]',
    baseline_sha TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    prio_class INTEGER NOT NULL DEFAULT 0,
    draft INTEGER NOT NULL DEFAULT 0,
    node_created REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'rebuild',
    state TEXT NOT NULL DEFAULT 'queued',
    retries INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS queue (
    sha TEXT PRIMARY KEY,
    priority INTEGER NOT NULL DEFAULT 0,
    enqueued_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS baselines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    note TEXT,
    path TEXT,
    root_sha TEXT,
    parent_id INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    sha TEXT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
"""


class Store:
    """Thin SQLite wrapper around the GuardCI tables."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self):
        """Add M2 columns to databases created before them."""
        for table, col, decl in (
                ("nodes", "attempt",
                 "INTEGER NOT NULL DEFAULT 0"),
                ("check_runs", "attempt",
                 "INTEGER NOT NULL DEFAULT 0"),
                ("verdicts", "attempt",
                 "INTEGER NOT NULL DEFAULT 0"),
                ("jobs", "source",
                 "TEXT NOT NULL DEFAULT 'rebuild'")):
            cols = {r["name"] for r in
                    self.db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.db.commit()

    def close(self):
        self.db.close()

    # -- nodes --------------------------------------------------------------

    def upsert_node(self, node: Node):
        now = time.time()
        self.db.execute(
            "INSERT INTO nodes (sha, parent, state, delta_tags, metrics,"
            " baseline_id, requested_full, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(sha) DO UPDATE SET"
            " parent=excluded.parent, updated_at=excluded.updated_at",
            (node.sha, node.parent, node.state.value,
             json.dumps(sorted(node.delta_tags)), json.dumps(node.metrics),
             node.baseline_id, int(node.requested_full), now, now))
        self.db.commit()

    def get_node(self, sha: str) -> Node | None:
        row = self.db.execute(
            "SELECT * FROM nodes WHERE sha=?", (sha,)).fetchone()
        if row is None and len(sha) < 40:
            row = self.db.execute(
                "SELECT * FROM nodes WHERE sha LIKE ?", (sha + "%",)).fetchone()
        if row is None:
            return None
        return Node(
            sha=row["sha"], parent=row["parent"],
            # old ledgers may carry the removed `held` state —
            # proposals are gone; read them as pending
            state=(State.PENDING if row["state"] == "held"
                   else State(row["state"])),
            delta_tags=set(json.loads(row["delta_tags"])),
            metrics=json.loads(row["metrics"]),
            baseline_id=row["baseline_id"],
            requested_full=bool(row["requested_full"]),
            attempt=row["attempt"], created_at=row["created_at"],
            updated_at=row["updated_at"])

    def set_state(self, sha: str, state: State):
        self.db.execute(
            "UPDATE nodes SET state=?, updated_at=? WHERE sha=?",
            (state.value, time.time(), sha))
        self.db.commit()

    def set_tags(self, sha: str, tags):
        self.db.execute(
            "UPDATE nodes SET delta_tags=?, updated_at=? WHERE sha=?",
            (json.dumps(sorted(tags)), time.time(), sha))
        self.db.commit()

    def set_metrics(self, sha: str, metrics: dict):
        self.db.execute(
            "UPDATE nodes SET metrics=?, updated_at=? WHERE sha=?",
            (json.dumps(metrics), time.time(), sha))
        self.db.commit()

    def set_requested_full(self, sha: str, flag: bool = True):
        self.db.execute(
            "UPDATE nodes SET requested_full=?, updated_at=? WHERE sha=?",
            (int(flag), time.time(), sha))
        self.db.commit()

    def set_baseline(self, sha: str, baseline_id: int):
        self.db.execute(
            "UPDATE nodes SET baseline_id=?, updated_at=? WHERE sha=?",
            (baseline_id, time.time(), sha))
        self.db.commit()

    def next_attempt(self, sha: str) -> int:
        """Bump and return the node's verification attempt counter."""
        self.db.execute(
            "UPDATE nodes SET attempt=attempt+1, updated_at=? WHERE sha=?",
            (time.time(), sha))
        self.db.commit()
        row = self.db.execute(
            "SELECT attempt FROM nodes WHERE sha=?", (sha,)).fetchone()
        return row["attempt"] if row else 0

    def node_created(self, sha: str) -> float:
        row = self.db.execute(
            "SELECT created_at, updated_at FROM nodes WHERE sha=?",
            (sha,)).fetchone()
        return (row["created_at"], row["updated_at"]) if row else (0, 0)

    def children(self, sha: str) -> list[str]:
        rows = self.db.execute(
            "SELECT sha FROM nodes WHERE parent=?", (sha,)).fetchall()
        return [r["sha"] for r in rows]

    def all_nodes(self) -> list[Node]:
        rows = self.db.execute(
            "SELECT sha FROM nodes ORDER BY created_at").fetchall()
        return [self.get_node(r["sha"]) for r in rows]

    # -- queue ----------------------------------------------------------------

    def enqueue(self, sha: str, priority: int = 0):
        self.db.execute(
            "INSERT INTO queue (sha, priority, enqueued_at) VALUES (?,?,?)"
            " ON CONFLICT(sha) DO NOTHING",
            (sha, priority, time.time()))
        self.db.commit()

    def dequeue(self) -> str | None:
        row = self.db.execute(
            "SELECT sha FROM queue ORDER BY priority DESC, enqueued_at"
            " LIMIT 1").fetchone()
        if row is None:
            return None
        self.db.execute("DELETE FROM queue WHERE sha=?", (row["sha"],))
        self.db.commit()
        return row["sha"]

    def queued(self) -> set[str]:
        rows = self.db.execute("SELECT sha FROM queue").fetchall()
        return {r["sha"] for r in rows}

    def queue_len(self) -> int:
        return self.db.execute("SELECT COUNT(*) c FROM queue").fetchone()["c"]

    # -- check runs / verdicts -------------------------------------------------

    def add_check_run(self, run: CheckRun) -> int:
        cur = self.db.execute(
            "INSERT INTO check_runs"
            " (sha, attempt, checker, backend, cost, status, seconds,"
            "  metrics, evidence, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run.sha, run.attempt, run.checker, run.backend,
             run.cost.value if run.cost else None, run.status, run.seconds,
             json.dumps(run.metrics), json.dumps(run.evidence), run.error))
        self.db.commit()
        return cur.lastrowid

    def check_runs(self, sha: str, all_attempts: bool = False
                   ) -> list[CheckRun]:
        rows = self.db.execute(
            "SELECT * FROM check_runs WHERE sha=? ORDER BY id", (sha,)
        ).fetchall()
        if rows and not all_attempts:
            latest = max(r["attempt"] for r in rows)
            rows = [r for r in rows if r["attempt"] == latest]
        out = []
        for r in rows:
            out.append(CheckRun(
                id=r["id"], sha=r["sha"], attempt=r["attempt"],
                checker=r["checker"], backend=r["backend"],
                cost=r["cost"], status=r["status"], seconds=r["seconds"],
                metrics=json.loads(r["metrics"]),
                evidence=json.loads(r["evidence"]), error=r["error"]))
        return out

    def replace_check_run(self, run: CheckRun):
        """Replace a named check run within an attempt (edge
        re-evaluation)."""
        self.db.execute(
            "DELETE FROM check_runs WHERE sha=? AND attempt=?"
            " AND checker=?",
            (run.sha, run.attempt, run.checker))
        self.add_check_run(run)

    def add_verdict(self, verdict: Verdict) -> int:
        cur = self.db.execute(
            "INSERT INTO verdicts (sha, attempt, clause, severity,"
            " status, detail) VALUES (?,?,?,?,?,?)",
            (verdict.sha, verdict.attempt, json.dumps({
                "check": verdict.clause.check,
                "id": verdict.clause.id,
                "severity": verdict.clause.severity.value,
                **verdict.clause.params}),
             verdict.clause.severity.value, verdict.status, verdict.detail))
        self.db.commit()
        return cur.lastrowid

    def delete_verdicts_where(self, sha: str, attempt: int, like: str):
        """Drop matching verdicts of one attempt (edge re-evaluation)."""
        self.db.execute(
            "DELETE FROM verdicts WHERE sha=? AND attempt=?"
            " AND clause LIKE ?", (sha, attempt, like))
        self.db.commit()

    def verdicts(self, sha: str, all_attempts: bool = False
                 ) -> list[Verdict]:
        rows = self.db.execute(
            "SELECT * FROM verdicts WHERE sha=? ORDER BY id", (sha,)
        ).fetchall()
        if rows and not all_attempts:
            latest = max(r["attempt"] for r in rows)
            rows = [r for r in rows if r["attempt"] == latest]
        out = []
        for r in rows:
            c = json.loads(r["clause"])
            check = c.pop("check")
            cid = c.pop("id", None)
            sev = Severity(c.pop("severity", r["severity"] or "warn"))
            out.append(Verdict(id=r["id"], sha=r["sha"],
                               attempt=r["attempt"],
                               clause=Clause(check=check, severity=sev,
                                             params=c, id=cid),
                               status=r["status"], detail=r["detail"] or ""))
        return out

    # -- baselines / events -----------------------------------------------------

    def add_baseline(self, note: str = "", path: str = "",
                     root_sha: str | None = None,
                     parent_id: int | None = None,
                     baseline_id: int | None = None) -> int:
        if baseline_id is not None:
            self.db.execute(
                "INSERT OR REPLACE INTO baselines"
                " (id, created_at, note, path, root_sha, parent_id)"
                " VALUES (?,?,?,?,?,?)",
                (baseline_id, time.time(), note, path, root_sha,
                 parent_id))
            self.db.commit()
            return baseline_id
        cur = self.db.execute(
            "INSERT INTO baselines (created_at, note, path, root_sha,"
            " parent_id) VALUES (?,?,?,?,?)",
            (time.time(), note, path, root_sha, parent_id))
        self.db.commit()
        return cur.lastrowid

    def baselines(self) -> list:
        rows = self.db.execute(
            "SELECT * FROM baselines ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def baseline_root(self) -> str | None:
        """root_sha of the current (newest) baseline, if recorded."""
        row = self.db.execute(
            "SELECT root_sha FROM baselines WHERE root_sha IS NOT NULL"
            " ORDER BY id DESC LIMIT 1").fetchone()
        return row["root_sha"] if row else None

    def baseline_for(self, sha: str, ancestors: list) -> dict | None:
        """The baseline whose root_sha is nearest on the first-parent
        chain (``ancestors`` includes-ancestors-excluding-self)."""
        roots = {b["root_sha"]: b for b in self.baselines()
                 if b["root_sha"]}
        if sha in roots:
            return roots[sha]
        for anc in ancestors:
            if anc in roots:
                return roots[anc]
        return None

    def log_event(self, kind: str, sha: str | None = None, payload=None):
        self.db.execute(
            "INSERT INTO events (ts, sha, kind, payload) VALUES (?,?,?,?)",
            (time.time(), sha, kind, json.dumps(payload or {})))
        self.db.commit()

    def events(self, sha: str | None = None) -> list[sqlite3.Row]:
        if sha is None:
            return self.db.execute(
                "SELECT * FROM events ORDER BY id").fetchall()
        return self.db.execute(
            "SELECT * FROM events WHERE sha=? ORDER BY id", (sha,)).fetchall()

    # -- jobs ----------------------------------------------------------------

    def add_job(self, sha: str, upto_stage: str, measurements: list,
                params: dict, clauses: list, baseline_sha: str | None,
                attempt: int, prio_class: int, draft: bool,
                node_created: float, source: str = "rebuild") -> int:
        """Queue a build+accept job for a node; replaces stale queued
        jobs for the same sha."""
        self.db.execute(
            "DELETE FROM jobs WHERE sha=? AND state='queued'", (sha,))
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO jobs (sha, upto_stage, measurements, params,"
            " clauses, baseline_sha, attempt, prio_class, draft,"
            " node_created, source, state, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)",
            (sha, upto_stage, json.dumps(measurements),
             json.dumps(params), json.dumps(clauses), baseline_sha,
             attempt, prio_class, int(draft), node_created, source,
             now, now))
        self.db.commit()
        return cur.lastrowid

    def _job_row(self, r) -> dict:
        d = dict(r)
        d["measurements"] = json.loads(d["measurements"])
        d["params"] = json.loads(d["params"])
        d["clauses"] = json.loads(d["clauses"])
        return d

    def queued_jobs(self) -> list[dict]:
        """Queued jobs in dispatch order: priority class, draft
        newest node first."""
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE state='queued'"
            " ORDER BY prio_class DESC, draft DESC,"
            " node_created DESC, id").fetchall()
        return [self._job_row(r) for r in rows]

    def running_jobs(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE state='running'").fetchall()
        return [self._job_row(r) for r in rows]

    def job_bump(self, sha: str, prio_class: int):
        """Raise the priority class of a queued job (bisection)."""
        self.db.execute(
            "UPDATE jobs SET prio_class=?, updated_at=? WHERE sha=?"
            " AND state='queued'",
            (prio_class, time.time(), sha))
        self.db.commit()

    def queued_job_for(self, sha: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM jobs WHERE sha=? AND state='queued'",
            (sha,)).fetchone()
        return self._job_row(row) if row else None

    def job_state(self, job_id: int, state: str, error=None):
        self.db.execute(
            "UPDATE jobs SET state=?, error=?, updated_at=? WHERE id=?",
            (state, error, time.time(), job_id))
        self.db.commit()

    def job_retry(self, job_id: int):
        self.db.execute(
            "UPDATE jobs SET state='queued', retries=retries+1,"
            " updated_at=? WHERE id=?", (time.time(), job_id))
        self.db.commit()

    def reset_running_jobs(self):
        """On startup, jobs left 'running' by a crash go back to
        'queued' (§9.8: a restarted server re-runs unfinished jobs)."""
        self.db.execute(
            "UPDATE jobs SET state='queued', updated_at=?"
            " WHERE state='running'", (time.time(),))
        self.db.commit()

    def jobs_for(self, sha: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE sha=? ORDER BY id", (sha,)
        ).fetchall()
        return [self._job_row(r) for r in rows]
