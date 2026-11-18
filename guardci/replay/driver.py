"""Snapshot-replay driver (source-agnostic).

Takes a loader-produced sequence of desired repo states and replays them
as linear commits through a real GuardCI project (init → commits → one
push → in-process drain), then collects per-node records.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml

from ..core.diff import list_tree
from ..core.models import Cost, State, public_state
from ..core.project import DEFAULTS, Project
from ..core.scheduler import Scheduler, _checker_cost, node_coverage, process_once
from ..core.store import Store
from ..core.tree import ancestors
from ..remote.repo import init_bare
from . import phb as phb_loader


def _git(repo, *args, env=None, check=True):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check,
                          env=env)


def _rebuild_seed_dir(manifest: dict, texts_root: Path, dest: Path,
                      placeholders: set | None = None,
                      recover=None):
    """Write the agent-visible flow dir of the first snapshot to disk
    (untranslated paths — ``guardci init --flow orfs`` does the ORFS
    seeding itself). ``placeholders``, if given, collects the repo
    paths whose content is the replay placeholder. ``recover`` (an
    optional ``_Recovery.lookup``) substitutes real bytes for
    metadata-only entries."""
    for e in manifest["entries"]:
        p = e["path"]
        if not p.startswith("./flow/") or e["type"] == "dir":
            continue
        rel = p[len("./flow/"):]
        if rel.startswith(phb_loader.IGNORED_PREFIXES) or \
                phb_loader._CORE_DUMP_RE.match(rel):
            continue
        out = dest / rel
        data: str | bytes = phb_loader.PLACEHOLDER.format(
            size=e["size"], mtime=e["mtime"])
        if e["class"] == "text-stored":
            member = phb_loader._member_path(
                texts_root, e.get("stored_in") or manifest["seq"], p)
            try:
                data = member.read_text()
            except (OSError, UnicodeDecodeError):
                pass
        if isinstance(data, str) and data.startswith(
                phb_loader.PLACEHOLDER_PREFIX) and recover is not None:
            src = recover(rel, e["size"], e["mtime"])
            if src is not None:
                try:
                    data = Path(src).read_bytes()
                except OSError:
                    pass
        if placeholders is not None and isinstance(data, str) and \
                data.startswith(phb_loader.PLACEHOLDER_PREFIX):
            placeholders.add(phb_loader.translate_path(rel))
        _write_replay_file(out, data, e.get("mode"))


def _write_replay_file(path: Path, data: str | bytes,
                       mode: str | None = None):
    """Write a replayed file (str or recovered bytes). Restore the
    recorded manifest mode when available; otherwise fall back to the
    shebang (scripts/*.py|sh|rb are invoked directly by the ORFS
    Makefile)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data)
    try:
        path.chmod(int(mode, 8))
    except (TypeError, ValueError):
        if isinstance(data, str) and data.startswith("#!"):
            path.chmod(0o755)


def _apply_delta(work: Path, delta: dict, modes: dict | None = None):
    for p, text in delta["writes"].items():
        _write_replay_file(work / p, text,
                           (modes or {}).get(p))
    for p in delta["deletes"]:
        f = work / p
        if f.exists():
            f.unlink()


class _Recovery:
    """Recover content for ``metadata-only`` manifest entries (§10.1).

    Sources, in order: (a) the task template workspace file —
    ``<templates>/<platform>-<design>/workspace/flow/<rel>`` — whose
    size must equal the manifest entry's; (b) the member ``./flow/<rel>``
    or ``flow/<rel>`` of the episode's ``final-workspace.tar.zst``
    whose size AND mtime must equal the entry's. The tar is streamed
    once per episode. ``map``: ``(rel, size, mtime) -> Path``."""

    def __init__(self, manifests: list, templates: Path | None,
                 source_dir: Path, platform: str, design: str,
                 recdir: Path):
        self.map: dict = {}
        self.counts = {"template": 0, "final_workspace": 0}
        need: dict[str, set] = {}
        for m in manifests:
            for e in m["entries"]:
                p = e["path"]
                if not p.startswith("./flow/") or e["type"] != "file":
                    continue
                rel = p[len("./flow/"):]
                if (rel.startswith(phb_loader.IGNORED_PREFIXES)
                        or phb_loader._CORE_DUMP_RE.match(rel)):
                    continue
                if e["class"] != "text-stored":
                    need.setdefault(rel, set()).add(
                        (e["size"], e["mtime"]))
        if not need:
            return
        # (a) task template workspace
        if templates:
            troot = (Path(templates) / f"{platform}-{design}"
                     / "workspace" / "flow")
            for rel, keys in need.items():
                for k in list(keys):
                    f = troot / rel
                    try:
                        if f.is_file() and f.stat().st_size == k[0]:
                            self.map[(rel, *k)] = f
                            keys.discard(k)
                            self.counts["template"] += 1
                    except OSError:
                        pass
        # (b) final-workspace.tar.zst, streamed once
        leftover = {rel for rel, keys in need.items() if keys}
        tar_path = Path(source_dir) / "final-workspace.tar.zst"
        if leftover and tar_path.exists():
            recdir.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                ["zstd", "-dc", str(tar_path)],
                stdout=subprocess.PIPE)
            try:
                with tarfile.open(fileobj=proc.stdout,
                                  mode="r|") as tf:
                    for m in tf:
                        nm = m.name
                        if nm.startswith("./flow/"):
                            rel = nm[len("./flow/"):]
                        elif nm.startswith("flow/"):
                            rel = nm[len("flow/"):]
                        else:
                            continue
                        for k in list(need.get(rel, ())):
                            if m.size == k[0] and \
                                    int(m.mtime) == int(k[1]):
                                safe = hashlib.sha1(
                                    f"{rel}:{k[0]}".encode()
                                ).hexdigest()[:16]
                                out = recdir / safe
                                out.write_bytes(
                                    tf.extractfile(m).read())
                                self.map[(rel, *k)] = out
                                need[rel].discard(k)
                                self.counts[
                                    "final_workspace"] += 1
            finally:
                proc.wait()

    def lookup(self, rel: str, size, mtime) -> Path | None:
        return self.map.get((rel, size, mtime))


def replay_episode(source_dir: Path, episode_dir: Path,
                   episode_id: str | None = None,
                   platform: str | None = None,
                   design: str | None = None,
                   meta: dict | None = None,
                   contract: str = "orfs-rebuild",
                   project_dir: Path | None = None,
                   executor: dict | None = None,
                   cache: str | None = None,
                   policies: list | None = None,
                   cache_max_gb: float | None = None,
                   artifacts_keep: str = "reports",
                   templates: Path | None = None,
                   flow: str = "orfs",
                   seed_fn=None,
                   backends: dict | None = None,
                   reset: dict | None = None) -> dict:
    """Replay one episode. Returns the summary dict (also written to
    ``<episode_dir>/summary.json``; per-node records go to
    ``nodes.jsonl``; the project lives in ``<episode_dir>/project``).

    With ``executor`` given (a guardci.yaml executor block) the project
    is configured for real builds against the shared ``cache`` and each
    listed ``policies`` entry is drained and recorded to
    ``nodes.<policy>.jsonl`` (§10.2/§10.3); without an executor the
    static M1 behaviour and record files are unchanged."""
    t0 = time.monotonic()
    from ..flows.orfs.adapter import reset_unknown
    reset_unknown()
    source_dir = Path(source_dir)
    episode_dir = Path(episode_dir)
    episode_dir.mkdir(parents=True, exist_ok=True)

    snapshots = phb_loader.list_snapshots(source_dir)
    if not snapshots:
        raise RuntimeError(f"no snapshots under {source_dir}/snapshots")
    manifests = [phb_loader.load_manifest(s["dir"]) for s in snapshots]
    need = phb_loader.needed_members(manifests)
    texts_root = phb_loader.extract_needed(
        snapshots, need, episode_dir / "text-cache")

    if meta:
        platform = platform or meta.get("platform")
        design = design or meta.get("design")
    if not platform or not design:
        raise RuntimeError("platform/design required (no meta given)")

    first_started = manifests[0].get("capture_started") or 0
    snap_meta = [phb_loader.snapshot_time(m, first_started)
                 for m in manifests]

    # ---- project + baseline -----------------------------------------
    proj_dir = Path(project_dir) if project_dir else (
        episode_dir / "project")
    recovery = _Recovery(manifests, templates, source_dir,
                         platform, design,
                         episode_dir / "recovered")
    seed_placeholders: set = set()   # baseline files left uncaptured
    placeholder_paths: set = set()   # current-tree placeholders
    if not (proj_dir / "guardci.yaml").exists():
        with tempfile.TemporaryDirectory() as td:
            seed_dir = Path(td) / "seed"
            _rebuild_seed_dir(manifests[0], texts_root, seed_dir,
                              placeholders=seed_placeholders,
                              recover=recovery.lookup)
            placeholder_paths |= seed_placeholders
            _init_project(proj_dir, contract, platform, design,
                          seed_dir, executor=executor, cache=cache,
                          cache_max_gb=cache_max_gb, flow=flow,
                          artifacts_keep=artifacts_keep,
                          seed_fn=seed_fn, backends=backends,
                          reset=reset)

    project = Project(proj_dir)
    repo = project.paths["remote"]
    baseline_sha = project.store.baseline_root()
    baseline_set = set(list_tree(repo, baseline_sha))

    # ---- commits -----------------------------------------------------
    # every non-ignored flow file of the first snapshot must be in the
    # seeded baseline — otherwise its later changes are invisible
    snap0 = phb_loader.flow_files(manifests[0])
    missing = sorted(set(snap0) - baseline_set)
    if missing:
        raise RuntimeError(
            f"baseline seed missing {len(missing)} paths "
            f"(first: {missing[:8]})")
    prev_entries = snap0

    commits = []
    uncaptured_changes = 0
    work = Path(tempfile.mkdtemp(prefix="gcreplay-"))
    try:
        subprocess.run(["git", "clone", "-q", str(repo), str(work)],
                       capture_output=True, text=True, check=True)
        for snap, manifest, smeta in zip(snapshots[1:], manifests[1:],
                                         snap_meta[1:]):
            delta = phb_loader.compute_delta(
                manifest, prev_entries, texts_root)
            uncaptured_changes += len(delta["uncaptured"])
            cur_entries = phb_loader.flow_files(manifest)
            for p in delta["uncaptured"]:
                e = cur_entries.get(p)
                if e is not None:
                    src = recovery.lookup(
                        phb_loader._untranslate(p),
                        e["size"], e["mtime"])
                    if src is not None:
                        try:
                            delta["writes"][p] = src.read_bytes()
                        except OSError:
                            pass
            prev_entries = cur_entries
            for p, text in delta["writes"].items():
                if isinstance(text, str) and text.startswith(
                        phb_loader.PLACEHOLDER_PREFIX):
                    placeholder_paths.add(p)
                else:
                    placeholder_paths.discard(p)
            for p in delta["deletes"]:
                placeholder_paths.discard(p)
            if not (delta["writes"] or delta["deletes"]):
                continue
            _apply_delta(
                work, delta,
                modes={p: cur_entries[p].get("mode")
                       for p in delta["writes"]
                       if p in cur_entries})
            _git(work, "add", "-A")
            if not _git(work, "status", "--porcelain").stdout.strip():
                continue
            env = dict(os.environ)
            iso = datetime.fromtimestamp(
                smeta["timestamp"], tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S")
            env["GIT_AUTHOR_DATE"] = iso
            env["GIT_COMMITTER_DATE"] = iso
            msg = (f"snap {snap['seq']:04d} {snap['name']} "
                   f"t={smeta['elapsed_seconds']:.0f}")
            _git(work, "-c", "user.name=guardci-replay", "-c",
                 "user.email=guardci-replay@localhost",
                 "commit", "-qm", msg, env=env)
            sha = _git(work, "rev-parse", "HEAD").stdout.strip()
            commits.append({
                "sha": sha, "snapshot": f"{snap['seq']:04d}",
                "event": snap["name"],
                "elapsed_s": round(smeta["elapsed_seconds"], 1),
                "changed_paths": (list(delta["writes"])
                                  + delta["deletes"]),
                "placeholders": sorted(placeholder_paths),
            })
        _git(work, "push", "origin", "HEAD:main")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(texts_root, ignore_errors=True)

    (episode_dir / "commits.json").write_text(
        json.dumps(commits, indent=1) + "\n")

    # ---- drain ---------------------------------------------------------
    policies = list(policies or ["routed"])
    prev = {}
    if (episode_dir / "summary.json").exists():
        try:
            prev = json.loads(
                (episode_dir / "summary.json").read_text())
        except (OSError, json.JSONDecodeError):
            prev = {}
    done_policies = {p for p, r in
                     (prev.get("policies") or {}).items()
                     if r.get("done")}

    static_mode = executor is None
    # §10.1: a placeholder inside a commit's recipe closure makes it
    # unreplayable — never queue a job for it (it would fail and land
    # in the cache as a bogus negative entry); it stays `verifying`.
    job_blocklist: set = set()
    if not static_mode:
        if seed_placeholders:
            # the baseline itself cannot be built (§10.1) — do not
            # queue any job for this episode
            job_blocklist = {c["sha"] for c in commits}
        else:
            btf = getattr(project.flow, "build_tree_new_files", None)
            for c in commits:
                ph = set(c.get("placeholders") or ())
                if not ph:
                    continue
                extra: set = set()
                if btf and baseline_sha:
                    try:
                        extra = set(btf(repo, c["sha"], baseline_sha))
                    except Exception as exc:  # noqa: BLE001 — record-only
                        logging.getLogger(__name__).debug(
                            "closure scan failed for %s: %s",
                            c["sha"], exc)
                try:
                    head_set = set(list_tree(repo, c["sha"]))
                except Exception:  # noqa: BLE001 — record-only
                    head_set = baseline_set
                if ph & ((baseline_set & head_set) | extra):
                    job_blocklist.add(c["sha"])
    per_policy_nodes: dict[str, list] = {}
    if static_mode:
        project = Project(proj_dir)
        try:
            while process_once(project):
                pass
        finally:
            project.close()
        per_policy_nodes["routed"] = _record_static(proj_dir, commits)
    else:
        for policy in policies:
            if policy in done_policies:
                continue
            project = Project(proj_dir)
            project.job_blocklist = job_blocklist
            try:
                if policy == "full":
                    _request_full_all(project)
                Scheduler(project).run(once=True)
                per_policy_nodes[policy] = _record_nodes(
                    project, commits, seed_placeholders)
            finally:
                project.close()
            (episode_dir / f"nodes.{policy}.jsonl").write_text(
                "".join(json.dumps(n) + "\n"
                        for n in per_policy_nodes[policy]))
            # checkpoint so a crash resumes after this policy
            done_policies.add(policy)
            _write_policy_checkpoint(episode_dir, prev,
                                     done_policies)
        for policy in policies:
            if policy not in per_policy_nodes:
                per_policy_nodes[policy] = [
                    json.loads(l) for l in
                    (episode_dir / f"nodes.{policy}.jsonl")
                    .read_text().splitlines() if l.strip()]

    last_policy = policies[-1] if policies else "routed"
    nodes = per_policy_nodes[last_policy]
    (episode_dir / "nodes.jsonl").write_text(
        "".join(json.dumps(n) + "\n" for n in nodes))

    counts = {}
    for n in nodes:
        counts[n["state"]] = counts.get(n["state"], 0) + 1

    first_fail = None
    for n in nodes:
        for v in n["verdicts"]:
            if v["severity"] == "hard" and v["status"] == "fail":
                first_fail = {
                    "snapshot": n["snapshot"],
                    "elapsed_s": n["elapsed_s"],
                    "clause": v["clause"],
                    "detail": v["detail"],
                    "paths": n["changed_paths"],
                }
                break
        if first_fail:
            break

    first_tag = {}
    for tag in ("physical_integrity", "library_refs", "unknown",
                "design_refs", "constraints"):
        for n in nodes:
            if tag in n["tags"]:
                first_tag[tag] = {"snapshot": n["snapshot"],
                                  "elapsed_s": n["elapsed_s"]}
                break

    # an .sdc change outside the recipe's own constraint file (the
    # flow builds with impl.sdc / the golden constraint.sdc)
    sdc_other = sum(
        1 for n in nodes
        if any(p.endswith(".sdc")
               and p.rsplit("/", 1)[-1] not in ("impl.sdc",
                                                "constraint.sdc")
               for p in n["changed_paths"]))

    summary = {
        "episode_id": episode_id or source_dir.name,
        "n_snapshots": len(snapshots),
        "n_commits": len(commits),
        "counts_by_state": counts,
        "first_hard_static_fail": first_fail,
        "first_tag": first_tag,
        "sdc_role_other_commits": sdc_other,
        "unknown_variables": sorted(_unknown_vars()),
        "uncaptured_changes": uncaptured_changes,
        "wall_seconds": (meta or {}).get("wall_seconds"),
        "replay_seconds": round(time.monotonic() - t0, 1),
    }
    if not static_mode:
        # §10.1: placeholders inside the recipe closure make the episode
        # unreplayable for builds — report them, never hide the failure
        summary["seed_placeholders"] = sorted(
            seed_placeholders or (placeholder_paths & baseline_set))
        summary["unreplayable_baseline"] = bool(
            summary["seed_placeholders"])
        summary["recovered"] = recovery.counts
        summary["unrecovered"] = sorted(
            placeholder_paths | seed_placeholders)[:50]
        summary["policies"] = {
            p: {"done": p in done_policies,
                "counts_by_state": _counts(per_policy_nodes.get(p, []))}
            for p in policies}
        summary["baseline"] = _baseline_record(proj_dir)
    (episode_dir / "summary.json").write_text(
        json.dumps(summary, indent=1) + "\n")
    return summary


def _init_project(proj_dir: Path, contract: str, platform: str,
                  design: str, seed_dir: Path, executor=None,
                  cache=None, cache_max_gb=None, flow="orfs",
                  seed_fn=None, backends=None,
                  artifacts_keep=None, reset=None):
    """Same as `guardci init --flow orfs` (duplicated on purpose: the CLI
    command is click-bound)."""
    from ..cli.main import _orfs_seed

    root = proj_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "contracts").mkdir(exist_ok=True)
    (root / "artifacts").mkdir(exist_ok=True)

    from ..contract.loader import _resolve
    src = _resolve(contract, Path.cwd())
    shutil.copyfile(src, root / "contracts" / src.name)

    cfg = dict(DEFAULTS)
    cfg["flow"] = flow
    cfg["contract"] = f"contracts/{src.name}"
    cfg["design"] = {"platform": platform, "design": design}
    if reset:
        cfg["design"]["reset"] = dict(reset)
    if executor is not None:
        cfg["executor"] = dict(executor)
    if cache is not None:
        cfg["cache"] = str(cache)
    if cache_max_gb is not None:
        cfg["cache_max_gb"] = cache_max_gb
    if backends:
        cfg["backends"] = dict(backends)
    if artifacts_keep is not None:
        cfg["artifacts_keep"] = artifacts_keep
    (root / "guardci.yaml").write_text(yaml.safe_dump(cfg,
                                                    sort_keys=False))

    remote = init_bare(root / cfg["remote"], root)

    # seed = ORFS-filtered copy of the agent's initial flow dir
    # (seed_fn overrides for non-ORFS flows, e.g. replay tests)
    with tempfile.TemporaryDirectory() as td:
        _git(td, "init", "-b", "main")
        if seed_fn is None:
            _orfs_seed(seed_dir, Path(td), platform, design)
        else:
            seed_fn(seed_dir, Path(td))
        _git(td, "add", "-A")
        _git(td, "-c", "user.name=guardci", "-c",
             "user.email=guardci@localhost", "commit", "-qm", "seed")
        seed_sha = _git(td, "rev-parse", "HEAD").stdout.strip()
        r = _git(td, "push", str(remote), "HEAD:main", check=False)
        if r.returncode != 0:
            raise click.ClickException(
                f"seed push failed:\n{r.stderr.strip()}")

    store = Store(root / cfg["state"])
    try:
        store.add_baseline(note="baseline 0 (init seed)",
                           root_sha=seed_sha, baseline_id=0)
    finally:
        store.close()


def _unknown_vars() -> set:
    from ..flows.orfs.adapter import unknown_vars
    return unknown_vars()


def load_meta(episode_dir: Path) -> dict:
    p = Path(episode_dir)
    if p.is_dir():
        p = p / "meta.json"
    return json.loads(p.read_text()) if p.exists() else {}


# ------------------------------------------------------------ M3 records

def _counts(nodes) -> dict:
    c = {}
    for n in nodes:
        c[n["state"]] = c.get(n["state"], 0) + 1
    return c


def _record_static(proj_dir: Path, commits: list) -> list:
    """M1 record shape (no executor): one object per commit."""
    project = Project(proj_dir)
    try:
        store = project.store
        nodes = []
        for c in commits:
            node = store.get_node(c["sha"])
            if node is None:
                continue
            verdicts = [{
                "clause": v.clause.name,
                "severity": v.clause.severity.value,
                "status": v.status,
                "detail": v.detail,
            } for v in store.verdicts(node.sha)]
            smetrics = {}
            for run in store.check_runs(node.sha):
                if run.checker in ("design_identity", "sdc_delta",
                                   "protected_refs"):
                    smetrics[run.checker] = run.metrics
            nodes.append({
                "sha": node.sha, "snapshot": c["snapshot"],
                "event": c["event"], "elapsed_s": c["elapsed_s"],
                "state": node.state.value,
                "tags": sorted(node.delta_tags),
                "verdicts": verdicts,
                "static_metrics": smetrics,
                "changed_paths": c["changed_paths"],
            })
        return nodes
    finally:
        project.close()


def _static_names(project) -> set:
    return {c.name for c in project.contract.clauses
            if _checker_cost(project, c.check) == Cost.STATIC}


def _statically_rejected(project, node) -> bool:
    """Rejected, and every hard fail is a static clause (§10.2: such a
    commit is never upgraded under the `full` policy)."""
    if node.state != State.REJECTED:
        return False
    static_names = _static_names(project)
    fails = [v for v in project.store.verdicts(node.sha)
             if v.status == "fail"
             and v.clause.severity.value == "hard"]
    return bool(fails) and all(v.clause.name in static_names
                               for v in fails)


def _request_full_all(project):
    """`full` policy: request-full every statically accepted commit —
    the same requeue `guardci request-full` performs (§10.2)."""
    from ..remote import refs as git_refs
    store = project.store
    for node in store.all_nodes():
        if node.state in (State.TAINTED, State.PENDING,
                          State.VERIFYING):
            continue
        if _statically_rejected(project, node):
            continue
        store.set_requested_full(node.sha, True)
        if node.sha not in store.queued():
            store.set_state(node.sha, State.PENDING)
            git_refs.set_state_ref(project.paths["remote"], node.sha,
                                   "pending")
            store.enqueue(node.sha)


def _write_policy_checkpoint(episode_dir: Path, prev: dict,
                             done_policies: set):
    """Merge finished-policy marks into summary.json so an interrupted
    episode resumes after the last completed policy."""
    data = prev if isinstance(prev, dict) else {}
    pols = dict(data.get("policies") or {})
    for p in done_policies:
        e = dict(pols.get(p) or {})
        e["done"] = True
        pols[p] = e
    data["policies"] = pols
    (episode_dir / "summary.json").write_text(
        json.dumps(data, indent=1) + "\n")


def _measurement_runs(project, runs) -> dict:
    """Measurement check runs are stored as ``measure:<name>`` so they
    cannot collide with same-named metric checkers (lec/drc)."""
    return {r.checker.split(":", 1)[-1]: r for r in runs
            if r.checker.startswith("measure:")}


def _job_record(project, node, mruns, build_run):
    """job object of §10.3 for the job that decided the node's **latest
    attempt**; ``measurement_results`` maps measurement → {seconds,
    memo_hit, key} (reused stages keep their recorded seconds)."""
    jobrow = next(
        (j for j in reversed(project.store.jobs_for(node.sha))
         if j["state"] == "done" and j["attempt"] == node.attempt),
        None)
    if jobrow is None:
        return None
    bm = build_run.metrics if build_run else {}
    return {
        "upto_stage": jobrow["upto_stage"],
        "measurements": (jobrow["measurements"]
                         if isinstance(jobrow["measurements"], list)
                         else json.loads(jobrow["measurements"])),
        "stages": [{"stage": r.get("stage"), "key": r.get("key"),
                    "reused": r.get("reused"), "cutoff": r.get("cutoff"),
                    "seconds": r.get("seconds"), "rc": r.get("rc")}
                   for r in (bm.get("stages") or [])],
        "stage_cutoff": bm.get("stage_cutoff") or [],
        "build_seconds": bm.get("build_seconds"),
        "accept_seconds": bm.get("accept_seconds"),
        "measurement_results": {
            m: {"seconds": r.seconds,
                "memo_hit": r.metrics.get("memo_hit"),
                "key": r.metrics.get("memo_key")}
            for m, r in mruns.items()},
    }


def _record_nodes(project, commits: list,
                  seed_placeholders: set | None = None) -> list:
    """§10.3 record: one object per commit in commit order."""
    seed_placeholders = seed_placeholders or set()
    store = project.store
    repo = str(project.paths["remote"])
    baseline_sha = store.baseline_root()
    flow = project.flow
    image = project.executor.image_id() if project.executor else None
    baseline_set = set(list_tree(repo, baseline_sha)) \
        if baseline_sha else set()
    btf = getattr(flow, "build_tree_new_files", None)
    nodes = []
    for c in commits:
        node = store.get_node(c["sha"])
        if node is None:
            continue
        verdicts = [{
            "clause": v.clause.name,
            "severity": v.clause.severity.value,
            "status": v.status,
            "detail": v.detail,
        } for v in store.verdicts(node.sha)]
        runs = store.check_runs(node.sha)
        build_run = next((r for r in runs if r.checker == "build"),
                         None)
        edge_run = next((r for r in runs if r.checker == "edges"),
                        None)
        mruns = _measurement_runs(project, runs)
        smetrics = {}
        for r in runs:
            if r.checker in ("design_identity", "sdc_delta",
                             "protected_refs"):
                smetrics[r.checker] = r.metrics
        accepted = not _statically_rejected(project, node)
        skeys = None
        if image is not None and accepted and baseline_sha:
            skeys = dict(flow.stage_keys(repo, node.sha, baseline_sha,
                                         image))
        try:
            head_set = set(list_tree(repo, node.sha))
        except (subprocess.CalledProcessError, ValueError,
                KeyError, OSError):
            head_set = baseline_set
        # paths actually materialized: baseline files still present
        # plus reachable new files
        closure_paths = set(baseline_set) & head_set
        if btf is not None and baseline_sha:
            try:
                closure_paths |= set(
                    btf(repo, node.sha, baseline_sha))
            except (subprocess.CalledProcessError, ValueError,
                    KeyError, OSError):
                pass  # record must not crash on a bad tree
        job = _job_record(project, node, mruns, build_run)
        # every measurement the latest attempt took from the memo —
        # inline decision or memo hits inside a job
        memo = {m: {"seconds": r.seconds,
                    "key": r.metrics.get("memo_key")}
                for m, r in mruns.items()
                if r.metrics.get("memo_hit")} or None
        decided = node.state in (State.VERIFIED, State.WARNED,
                                 State.REJECTED)
        decided_by = None
        if decided:
            decided_by = ("job" if job is not None else
                          "static" if _statically_rejected(
                              project, node) else "memo")
        # public vocabulary + findings + violating ancestor (paper
        # evaluation counts by public verdict and finding kind)
        from ..core.status import public_status
        pub = public_status(project, node.sha)
        void_ancestor = None
        if node.state == State.TAINTED:
            for anc in ancestors(store, node.sha):
                anode = store.get_node(anc)
                if anode is not None and                         anode.state == State.REJECTED:
                    void_ancestor = anc
                    break
        nodes.append({
            "sha": node.sha, "parent": node.parent,
            "snapshot": c["snapshot"], "event": c["event"],
            "elapsed_s": c["elapsed_s"],
            "state": node.state.value,
            "verdict": public_state(node.state),
            "grade": (pub or {}).get("grade"),
            "findings": (pub or {}).get("findings"),
            "warnings": (pub or {}).get("warnings"),
            "gaps": (node.metrics or {}).get("_gaps"),
            "void_ancestor": void_ancestor,
            "coverage": node_coverage(project, node.sha),
            "tags": sorted(node.delta_tags),
            "attempt": node.attempt,
            "decided_by": decided_by,
            "verdicts": verdicts,
            "metrics": node.metrics,
            "static_metrics": smetrics,
            "stage_keys": skeys,
            "job": job,
            "memo": memo,
            "edges": ({"reference": edge_run.metrics.get("reference"),
                       "rows": edge_run.metrics.get("rows")}
                      if edge_run else
                      {"reference": None, "rows": []}),
            "uncaptured_in_closure": sorted(
                (set(c.get("placeholders") or ())
                 | (seed_placeholders & baseline_set))
                & closure_paths),
            # `unreplayable` also when the baseline itself cannot
            # build — every job for the episode is suppressed
            "unreplayable": bool(
                ((set(c.get("placeholders") or ())
                  | (seed_placeholders & baseline_set))
                 & closure_paths) or seed_placeholders),
            "changed_paths": c["changed_paths"],
        })
    return nodes


def _baseline_record(proj_dir: Path) -> dict | None:
    """§10.3 baseline record: sha, tree, stage_keys, stage_seconds,
    flow_seconds, metrics (from the shared golden dir)."""
    project = Project(proj_dir)
    try:
        store = project.store
        sha = store.baseline_root()
        if not sha:
            return None
        repo = str(project.paths["remote"])
        tree = _git(repo, "rev-parse", f"{sha}^{{tree}}"
                    ).stdout.strip()
        rec = {"sha": sha, "tree": tree}
        bj = project.cache.golden_dir(tree) / "baseline.json"
        if bj.exists():
            b = json.loads(bj.read_text())
            rec.update({"stage_seconds": b.get("stage_seconds"),
                        "flow_seconds": b.get("flow_seconds"),
                        "metrics": b.get("metrics")})
        if project.executor is not None:
            rec["stage_keys"] = dict(project.flow.stage_keys(
                repo, sha, sha, project.executor.image_id()))
        return rec
    finally:
        project.close()
