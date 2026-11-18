"""Node scheduler (DESIGN.md §9.1/§9.8).

Phases per node: ``static`` (inline, in the scheduler thread) →
``build``+``accept`` (a persisted job on an executor slot) → ``verdict``
(inline). Fail-closed: any clause whose checker is unavailable or errors,
and any infrastructure error, leaves the node in ``verifying`` with an
``awaiting``/``job_error`` event.

``Scheduler`` owns a worker pool of ``executor.slots()``; only the
scheduler thread writes SQLite/refs/notes. Jobs live in the ``jobs``
table so a restarted server re-runs unfinished ones.
"""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import time
from pathlib import Path

from ..remote import refs as git_refs
from . import edges as edges_mod
from . import tree
from . import verify as verify_mod
from .diff import build_diff
from .models import CheckRun, Clause, Cost, Severity, State, Tag, Verdict, public_state
from .paths import match_any
from .router import select_checks


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True,
        text=True).stdout


def baseline_diff_paths(repo: str, sha: str,
                        baseline_sha: str | None) -> list:
    """All paths differing between the baseline commit and ``sha``."""
    if baseline_sha is None:
        return []
    out = _git(repo, "diff", "--name-only", baseline_sha, sha)
    return [p for p in out.splitlines() if p]


def clean_paths(repo: str, sha: str, baseline_sha: str | None,
                recipe) -> list:
    """Paths that differ between the baseline and sha and are NOT clean.

    A path is clean iff it matches a `recipe` glob or lives under
    `.guardci/` (the CI's own namespace; §9.8/§11.3). Empty list ⇒
    clean w.r.t. the baseline.
    """
    dirty = []
    for p in baseline_diff_paths(repo, sha, baseline_sha):
        if p.startswith(".guardci/") or match_any(p, recipe):
            continue
        dirty.append(p)
    return dirty


def _is_protected(path: str, globs) -> bool:
    return match_any(path, globs)


def _rejected_protected(store, sha: str) -> bool:
    """Was this node rejected for a protected-set (tainting) violation?"""
    for ev in store.events(sha):
        if ev["kind"] == "rejected":
            return json.loads(ev["payload"]).get("taint") == "protected"
    return False


def _set_state(project, sha: str, state: State):
    project.store.set_state(sha, state)
    git_refs.set_state_ref(project.paths["remote"], sha, state.value)


def _is_clean(project, sha: str) -> bool:
    """clean(sha): every baseline-vs-sha changed path is in recipe/
    (DESIGN.md §3.2 'tainted until restored')."""
    anc = tree.ancestors(project.store, sha)
    baseline = project.store.baseline_for(sha, anc)
    if baseline is None:
        return False
    return not clean_paths(project.paths["remote"], sha,
                           baseline["root_sha"], project.contract.recipe)


def _reject_not_baseline(project, sha: str):
    """Reject a node whose history does not contain the baseline root."""
    detail = "root commit is not the project baseline"
    repo = project.paths["remote"]
    store = project.store
    _set_state(project, sha, State.REJECTED)
    desc = tree.descendants(store, sha)
    tree.propagate_taint(store, sha, "protected",
                         clean_fn=lambda s: _is_clean(project, s))
    for tdesc in desc:
        tnode = store.get_node(tdesc)
        if tnode is not None:
            git_refs.set_state_ref(repo, tdesc, tnode.state.value)
    store.log_event("rejected", sha,
                    {"fails": [], "taint": "protected", "detail": detail})
    git_refs.write_note(repo, sha, {"state": public_state(State.REJECTED), "detail": detail})


def _build_clause() -> Clause:
    return Clause(check="build", severity=Severity.HARD)


def _checker_cost(project, check: str) -> Cost | None:
    cls = project.checkers.get(check)
    return getattr(cls, "cost", None) if cls is not None else None


def _clause_kind(project, check: str) -> str:
    """§11.1 clause kind: contract ``kind`` wins, else the checker
    class default; the implicit ``build`` clause and unknown checkers
    are ``exit``. Edge (ratchet) clauses behave like ``exit``."""
    if check == "build" or check.startswith("edge:"):
        return "exit"
    clause = next((c for c in project.contract.clauses
                   if c.name == check), None)
    if clause is not None and clause.kind:
        return clause.kind
    cls = project.checkers.get(check)
    return getattr(cls, "kind", "exit") if cls is not None else "exit"


_TRIVIAL_IN_REBUILD = {"library_closure", "provenance", "reproducible"}


def _split_clauses(project, clauses):
    """(static_runnable, nonstatic, unavailable) clause groups.
    §12.4: under ``source: rebuild`` the checkpoint checkers have no
    artifact to read — they run in the static phase and evaluate
    trivially (``rebuilt``/by-construction pass)."""
    rebuild = (project.contract.verification or {}).get(
        "source", "rebuild") == "rebuild"
    static = [c for c in clauses
              if project.checkers.get(c.check) is None
              or _checker_cost(project, c.check) == Cost.STATIC
              or (rebuild and c.check in _TRIVIAL_IN_REBUILD)]
    nonstatic = [c for c in clauses
                 if project.checkers.get(c.check) is not None
                 and _checker_cost(project, c.check) in
                 (Cost.LIGHT, Cost.HEAVY)
                 and not (rebuild and c.check in _TRIVIAL_IN_REBUILD)]
    return static, nonstatic


def _clause_needs(cls, params):
    """Measurement names a clause requires (metric_bound's depends on
    its ``metric`` param)."""
    fn = getattr(cls, "needs_for", None)
    if callable(fn):
        out = fn(params)
        if isinstance(out, str):
            return [out] if out else []
        return list(out or [])
    n = getattr(cls, "needs", None)
    return [n] if n else []


def _measurements_for(project, clauses, sha=None):
    """{measurement: merged params} for the selected non-static
    clauses, plus the job's build depth. The signoff measurement params
    carry no contract-derived values (§11.3): its facts are measured at
    the golden constraint period, resolved at job execution."""
    needs: dict[str, dict] = {}
    heavy = False
    for c in clauses:
        cls = project.checkers[c.check]
        if getattr(cls, "cost", None) == Cost.HEAVY:
            heavy = True
        for meas in _clause_needs(cls, c.params):
            needs.setdefault(meas, {}).update(c.params)
    signoff = needs.get("signoff")
    if signoff is not None:
        needs["signoff"] = {k: v for k, v in signoff.items()
                            if k == "timeout"}
    upto = (project.flow.stages()[-1] if heavy
            else getattr(project.flow, "light_stage", "synth"))
    return needs, upto


def _job_view(project) -> verify_mod.JobView:
    measurements = ("signoff", "drc", "lec", "facts")
    backends = {}
    for m in measurements:
        b = project.backend_for(m)
        if b is not None:
            backends[m] = b
    eb = project.backend_for("estimate")
    if eb is not None:
        backends["estimate"] = eb
    return verify_mod.JobView(
        repo=project.paths["remote"],
        artifacts=Path(project.paths["artifacts"]),
        flow=project.flow,
        executor=project.executor,
        cache=project.cache,
        protected=list(project.contract.protected),
        backends=backends,
        artifacts_keep=project.cfg.get("artifacts_keep", "all"),
        reset=(project.cfg.get("design") or {}).get("reset"),
        clock_port=(project.cfg.get("design") or {}).get("clock_port"),
        design_name=(project.cfg.get("design") or {}).get("design"),
        predict=getattr(project.contract, "predict", None))


def _stage_keys(project, sha, baseline_sha):
    """{stage: key|None} — the §11.4.2 chain resolved through the
    cache index; ``None`` for stages whose key cannot be computed yet."""
    resolve = getattr(project.flow, "resolve_keys", None)
    if callable(resolve):
        pairs, _keyer = resolve(
            project.paths["remote"], sha, baseline_sha,
            project.executor.image_id(), cache=project.cache)
        return dict(pairs)
    return dict(project.flow.stage_keys(
        project.paths["remote"], sha, baseline_sha,
        project.executor.image_id()))


def _memos_for(view, keys: dict, upto: str, needs: dict,
               constraints) -> dict | None:
    """{measurement: memo} when every needed measurement is memoized
    for this node's stage keys; else None."""
    depth_key = keys.get(upto)
    if depth_key is None:
        return None
    out = {}
    for m, params in needs.items():
        memo = verify_mod.memo_lookup(view, depth_key, m, params,
                                      constraints)
        if memo is None:
            return None
        out[m] = memo
    return out


def _golden_constraints(project, baseline_sha) -> dict:
    try:
        return project.flow.golden_constraints(
            project.paths["remote"], baseline_sha)
    except Exception:  # noqa: BLE001 — memo lookup must not crash static
        return {}


def _attach_metrics(project, sha: str, metrics: dict):
    node = project.store.get_node(sha)
    merged = dict(node.metrics)
    merged.update(metrics)
    project.store.set_metrics(sha, merged)


def _baseline_metrics(project, baseline_sha) -> dict:
    """Metrics recorded on the baseline root node — or, for a project
    sharing a cache that another project already populated, read from
    ``<cache>/golden/<baseline tree sha>/baseline.json`` and copy them
    onto the root node."""
    if not baseline_sha:
        return {}
    node = project.store.get_node(baseline_sha)
    if node and node.metrics and \
            node.metrics.get("min_period") is not None:
        return dict(node.metrics)
    # a copy made before the cache's baseline.json was refreshed to the
    # current signoff backend is stale — re-read the file
    try:
        tree = subprocess.run(
            ["git", "-C", project.paths["remote"], "rev-parse",
             f"{baseline_sha}^{{tree}}"],
            check=True, capture_output=True, text=True).stdout.strip()
        bjson = project.cache.golden_dir(tree) / "baseline.json"
        if bjson.exists():
            metrics = json.loads(bjson.read_text()).get("metrics") or {}
            if metrics:
                if node is not None:
                    project.store.set_metrics(baseline_sha, metrics)
                return metrics
    except Exception as exc:  # noqa: BLE001 — absent baseline is not fatal
        project.store.log_event(
            "baseline_metrics_unavailable", baseline_sha,
            {"error": str(exc)})
    return dict(node.metrics) if node and node.metrics else {}


def _clause_stage(project, clause, cls) -> str:
    """§12.4: the stage whose facts decide the clause — contract
    ``stage:`` param wins, else the checker's native stage, else the
    last stage."""
    last = project.flow.stages()[-1]
    return (clause.params.get("stage")
            or getattr(cls, "native_stage", "") or last)


def _run_metric_checkers(project, sha, attempt, clauses) -> tuple:
    """Run the selected non-static checkers against the node's metrics.
    Returns (runs, verdicts, awaiting, hard_fails, warn_fails).
    §12.4: a clause above the node's stage is `pending` — neither a
    failure nor awaiting."""
    node = project.store.get_node(sha)
    baseline_sha = _baseline_sha_for(project, sha)
    metrics = node.metrics or {}
    stages_facts = metrics.get("stages") or {}
    order = {s: i for i, s in enumerate(project.flow.stages())}
    node_stage = metrics.get("stage")
    node_rank = order.get(node_stage, len(order)) if node_stage \
        else len(order)
    ctx = {
        "project": project, "node": node,
        "metrics": metrics,
        "baseline_metrics": _baseline_metrics(project, baseline_sha),
        "golden": _golden_constraints(project, baseline_sha),
        "repo": project.paths["remote"],
        "contract": project.contract,
    }
    runs, verdicts, awaiting = [], [], []
    hard_fails, warn_fails = [], []
    for clause in clauses:
        cls = project.checkers.get(clause.check)
        if cls is None:
            verdicts.append(Verdict(
                sha=sha, attempt=attempt, clause=clause,
                status="unavailable", detail="checker not registered"))
            awaiting.append(clause.name)
            continue
        cstage = _clause_stage(project, clause, cls)
        crank = order.get(cstage, len(order))
        if crank > node_rank:
            # §12.4: clauses beyond the node's stage are `pending`
            verdicts.append(Verdict(
                sha=sha, attempt=attempt, clause=clause,
                status="pending",
                detail=f"stage {cstage} not reached"))
            continue
        sctx = dict(ctx)
        if stages_facts:
            # §12.4: a clause pinned with `stage:` reads that stage's
            # facts; a checker-native clause reads the node's deepest
            # measured stage (the design state is judged on its own —
            # an edit at a later stage must not hide behind the
            # earlier stage's facts)
            pinned = clause.params.get("stage") is not None
            eff = crank if pinned else node_rank
            last_rank = len(order) - 1
            sfacts = {}
            bstage = cstage
            for s in sorted(stages_facts, key=lambda x: order.get(x, 0)):
                if order.get(s, 0) <= eff:
                    sfacts = dict(stages_facts[s])
                    bstage = s
            if order.get(bstage, 0) == last_rank:
                # at the last stage the signoff measurement decides
                # any key both it and the stage facts emit
                sctx["metrics"] = {**sfacts, **metrics}
            else:
                sctx["metrics"] = {**metrics, **sfacts}
            bm = _baseline_metrics(project, baseline_sha) or {}
            bs = (bm.get("stages") or {}).get(bstage) or {}
            if order.get(bstage, 0) == last_rank:
                sctx["baseline_metrics"] = {**bs, **bm}
            else:
                sctx["baseline_metrics"] = {**bm, **bs}
        backend = project.backend_for(getattr(cls, "needs", "") or "")
        try:
            result = cls().run(sctx, clause.params, backend)
            runs.append(CheckRun(
                sha=sha, attempt=attempt, checker=clause.name,
                backend=backend.tool if backend else None,
                cost=getattr(cls, "cost", None),
                status=result.status, seconds=result.seconds,
                metrics=result.metrics,
                evidence=[str(e) for e in result.evidence],
                error=result.detail if result.status == "error"
                else None))
            status, detail = result.status, result.detail
        except Exception as exc:  # noqa: BLE001 — fail-closed
            runs.append(CheckRun(
                sha=sha, attempt=attempt, checker=clause.name,
                backend=None, cost=getattr(cls, "cost", None),
                status="error", error=str(exc)))
            status, detail = "error", str(exc)
        verdicts.append(Verdict(sha=sha, attempt=attempt, clause=clause,
                                status=status, detail=detail))
        if status == "fail":
            if clause.severity == Severity.HARD:
                hard_fails.append(clause.name)
            elif clause.severity == Severity.WARN:
                warn_fails.append(clause.name)
        elif status == "pending":
            pass                       # §12.4: not failing, not awaiting
        elif status in ("error", "unavailable") and \
                clause.severity != Severity.INFO:
            awaiting.append(clause.name)
    return runs, verdicts, awaiting, hard_fails, warn_fails


def node_coverage(project, sha) -> str:
    """``full`` when every contract clause (static and non-static) has
    a pass/fail verdict in the node's latest attempt — including
    free clauses evaluated on measured metrics (§9.6).  §12.5: with
    ``stages`` evidence, ``pending`` clauses above the node stage do
    not block coverage — below the last stage coverage is ``stage``."""
    store = project.store
    node = store.get_node(sha)
    try:
        last = project.flow.stages()[-1]
    except (NotImplementedError, KeyError):
        last = None
    node_stage = (node.metrics or {}).get("stage") if node else None
    pending_ok = node_stage is not None
    verdicted = {v.clause.name for v in store.verdicts(sha)
                 if v.status in ("pass", "fail")}
    pending = {v.clause.name for v in store.verdicts(sha)
               if v.status == "pending"}
    missing = [c for c in project.contract.clauses
               if c.name not in verdicted
               and not (pending_ok and c.name in pending)]
    if not missing:
        return ("full" if node_stage in (None, last) else "stage")
    # static clauses keep their verdict across attempts — an upgrade
    # job starts a new attempt without re-running the static phase,
    # and the node's content cannot have changed
    older = {v.clause.name for v in store.verdicts(sha,
                                                    all_attempts=True)
             if v.status in ("pass", "fail")}
    for c in missing:
        cls = project.checkers.get(c.check)
        if getattr(cls, "cost", None) == Cost.STATIC and \
                c.name in older:
            continue
        return "partial"
    return ("full" if node_stage in (None, last) else "stage")


def _baseline_sha_for(project, sha) -> str | None:
    anc = tree.ancestors(project.store, sha)
    b = project.store.baseline_for(sha, anc)
    return b["root_sha"] if b else None


def _free_clauses(project, nonstatic_clauses, measured) -> list:
    """Contract non-static clauses not routed but whose ``needs``
    measurement ran for this node — evaluated for free (§9.6 "Free
    clauses and coverage")."""
    have = {c.name for c in nonstatic_clauses}
    out = []
    for c in project.contract.clauses:
        if c.name in have:
            continue
        cls = project.checkers.get(c.check)
        if cls is None:
            continue
        if getattr(cls, "cost", None) not in (Cost.LIGHT, Cost.HEAVY):
            continue
        needs = getattr(cls, "needs", None)
        if needs and needs in measured:
            out.append(c)
    return out


def _decide(project, sha, attempt, nonstatic_clauses,
            build_verdict=None, build_run=None,
            measurement_runs=None, tags=(),
            predicted=None) -> State:
    """Verdict phase after metrics exist (job or memo): metric checkers
    + differential table + edge verdicts + state + descendant re-eval."""
    store = project.store
    verdicts = []
    if build_verdict is not None:
        verdicts.append(build_verdict)
    if build_run is not None:
        store.add_check_run(build_run)
    for run in (measurement_runs or []):
        store.add_check_run(run)

    measured = {r.checker.split(":", 1)[-1]
                   for r in (measurement_runs or [])}
    nonstatic_clauses = list(nonstatic_clauses) + \
        _free_clauses(project, nonstatic_clauses, measured)

    runs, cverdicts, awaiting, hard_fails, warn_fails = \
        _run_metric_checkers(project, sha, attempt, nonstatic_clauses)
    for r in runs:
        store.add_check_run(r)
    verdicts += cverdicts

    node = store.get_node(sha)
    state = None
    inv_fails = [f for f in hard_fails
                 if _clause_kind(project, f) == "invariant"]
    exit_fails = [f for f in hard_fails
                  if _clause_kind(project, f) != "invariant"]
    build_failed = build_verdict is not None and         build_verdict.status == "fail"
    # §11.1: ratchet verdicts only when every hard exit clause passes
    exits_ok = not exit_fails and not build_failed and not awaiting
    if node.metrics is not None:
        erun, everdicts = edges_mod.evaluate(
            store, sha, node, project.contract, attempt,
            verdicts_allowed=exits_ok)
        store.replace_check_run(erun)
        store.delete_verdicts_where(sha, attempt, '%"check": "edge:%')
        verdicts += everdicts
        for v in everdicts:
            if v.status == "fail":
                if v.clause.severity == Severity.HARD:
                    exit_fails.append(v.clause.name)
                elif v.clause.severity == Severity.WARN:
                    warn_fails.append(v.clause.name)

    if predicted is not None:
        # §11.4 predicted open — a synthetic hard exit fail on
        # `sta_setup` carrying the estimated gap; never a pass
        clause = next((c for c in project.contract.clauses
                       if c.check == "sta_setup"), None)
        v = Verdict(sha=sha, attempt=attempt,
                    clause=clause or Clause(check="sta_setup",
                                            severity=Severity.HARD,
                                            kind="exit"),
                    status="fail",
                    detail=f"predicted: est_setup_wns "
                           f"{predicted.get('est_setup_wns')}")
        store.add_verdict(v)
        exit_fails.append("sta_setup")
        exits_ok = False

    for v in verdicts:
        store.add_verdict(v)

    gaps = _collect_gaps(project, sha, attempt,
                         build_verdict if build_failed else None)
    if predicted is not None:
        gaps["sta_setup"] = dict(predicted.get("gap") or {})
    if inv_fails:
        state = State.REJECTED
        kind = ("protected" if "recipe_scope" in inv_fails
                else "metric")
        _propagate(project, sha, kind, inv_fails)
        store.log_event("rejected", sha,
                        {"fails": inv_fails, "taint": kind})
    elif build_failed or exit_fails:
        # a hard exit/ratchet fail is decisive even when other clauses
        # are awaiting — the node is `open`, never tainted (§11.1)
        state = State.OPEN
        fails = (["build"] if build_failed else []) + exit_fails
        store.log_event("open", sha,
                        {"fails": fails, "gaps": gaps})
    elif awaiting:
        store.log_event("awaiting", sha, {"checkers": sorted(awaiting)})
    elif warn_fails:
        state = State.WARNED
        store.log_event("warned", sha, {"fails": warn_fails})
    else:
        state = State.VERIFIED
        store.log_event("verified", sha, {})
    if state is not None:
        _set_state(project, sha, state)
    if gaps is not None:
        _attach_gaps(project, sha, gaps)

    node = store.get_node(sha)
    git_refs.write_note(project.paths["remote"], sha, {
        "state": public_state(node.state),
        "tags": sorted(node.delta_tags),
        "metrics": node.metrics,
        "coverage": node_coverage(project, sha),
        "checks": [{"checker": r.checker, "status": r.status}
                   for r in store.check_runs(sha)],
    })
    _reevaluate_descendant_edges(project, sha)
    _bisect_advance(project, sha, attempt)
    return state


def _collect_gaps(project, sha: str, attempt: int,
                  build_verdict=None) -> dict:
    """§11.1 gaps for this attempt: ``{clause: gap}`` per unmet exit
    clause — each failing exit checker reports ``metrics["gap"]``; the
    implicit ``build`` clause reports the failed stage."""
    gaps: dict = {}
    if build_verdict is not None:
        detail = build_verdict.detail or ""
        stage = detail.split()[1] if detail.startswith(
            ("timeout at", "stage ")) else detail
        gaps["build"] = {"stage": stage}
    for run in project.store.check_runs(sha):
        if run.attempt != attempt:
            continue
        gap = (run.metrics or {}).get("gap")
        if gap is not None and run.checker not in gaps:
            gaps[run.checker] = gap
        egaps = (run.metrics or {}).get("gaps")
        if isinstance(egaps, dict):
            gaps.update(egaps)
    return gaps


def _attach_gaps(project, sha: str, gaps: dict):
    """Store ``_gaps`` under reserved metric keys (§11.1; not
    canonical metrics, never ratchet dimensions). ``min_period``
    itself is a measured canonical metric (§11.3)."""
    node = project.store.get_node(sha)
    merged = dict(node.metrics or {})
    if gaps:
        merged["_gaps"] = gaps
    project.store.set_metrics(sha, merged)


def _propagate(project, sha, kind, fails):
    desc = tree.descendants(project.store, sha)
    if getattr(project.contract, "void_mode",
             "descendants") == "descendants":
        kind = "void"
    tree.propagate_taint(project.store, sha, kind,
                         clean_fn=lambda s: _is_clean(project, s))
    for tdesc in desc:
        tnode = project.store.get_node(tdesc)
        if tnode is not None:
            git_refs.set_state_ref(project.paths["remote"], tdesc,
                                   tnode.state.value)


def _exits_passed(store, sha, attempt) -> bool:
    """No stored hard-fail verdict on an exit-kind clause — the gate
    for producing ratchet verdicts (§11.1)."""
    for v in store.verdicts(sha):
        if v.status == "fail" and v.clause.severity == Severity.HARD:
            return False
    return True


def _reevaluate_descendant_edges(project, sha):
    """Recompute descendants' ratchet tables when this node's state or
    metrics may have changed their reference — down to (and including)
    the next deliverable on each path (§11.1)."""
    store = project.store
    decided = (State.VERIFIED, State.WARNED, State.REJECTED, State.OPEN)
    stack = list(store.children(sha))
    seen = set(stack)
    while stack:
        desc = stack.pop()
        dnode = store.get_node(desc)
        if dnode is None or dnode.state not in decided:
            continue
        deliverable = dnode.state in (State.VERIFIED, State.WARNED)
        if dnode.metrics:
            allowed = _exits_passed(store, desc, dnode.attempt)
            erun, everdicts = edges_mod.evaluate(
                store, desc, dnode, project.contract, dnode.attempt,
                verdicts_allowed=allowed)
            prev = [r for r in store.check_runs(desc)
                    if r.checker == edges_mod.EDGE_CHECKER]
            old = prev[-1].metrics if prev else None
            if old != erun.metrics:
                store.replace_check_run(erun)
                store.delete_verdicts_where(desc, dnode.attempt,
                                            '%"check": "edge:%')
                for v in everdicts:
                    store.add_verdict(v)
                _restate_from_verdicts(project, desc)
        if not deliverable:
            for ch in store.children(desc):
                if ch not in seen:
                    seen.add(ch)
                    stack.append(ch)


def _restate_from_verdicts(project, sha):
    """Recompute a decided node's state from its latest-attempt
    verdicts under the §11.1 order (invariant → exit → warn)."""
    store = project.store
    node = store.get_node(sha)
    fails = [v for v in store.verdicts(sha) if v.status == "fail"]
    inv = [v for v in fails if v.clause.severity == Severity.HARD
           and _clause_kind(project, v.clause.name) == "invariant"]
    exh = [v for v in fails if v.clause.severity == Severity.HARD
           and _clause_kind(project, v.clause.name) != "invariant"]
    warn = [v for v in fails if v.clause.severity == Severity.WARN]
    if inv and node.state != State.REJECTED:
        _set_state(project, sha, State.REJECTED)
        kind = ("protected" if any(v.clause.check == "recipe_scope"
                                   for v in inv) else "metric")
        _propagate(project, sha, kind,
                   [v.clause.name for v in inv])
        store.log_event("rejected", sha,
                        {"fails": [v.clause.name for v in inv],
                         "taint": kind})
    elif exh and node.state != State.OPEN:
        _set_state(project, sha, State.OPEN)
        store.log_event("open", sha,
                        {"fails": [v.clause.name for v in exh]})
    elif warn and node.state in (State.VERIFIED, State.OPEN):
        _set_state(project, sha, State.WARNED)
        store.log_event("warned", sha,
                        {"fails": [v.clause.name for v in warn]})
    elif not fails and node.state in (State.WARNED, State.OPEN,
                                      State.REJECTED):
        _set_state(project, sha, State.VERIFIED)
        store.log_event("verified", sha, {})


# ------------------------------------------------------------ static phase


def process_once(project) -> bool:
    """Process one queued node's static phase. Returns True if a node
    was handled."""
    sha = project.store.dequeue()
    if sha is None:
        return False
    node = project.store.get_node(sha)
    if node is None or node.state != State.PENDING:
        # dropped from pending by taint/amend while queued — leave it alone
        return True
    _process_sha(project, sha)
    return True


def _process_sha(project, sha: str):
    """Static phase for one node (§9.1): clean/taint, tags, static
    clauses, then reject/open/defer/decide. Also drives `rejudge`
    (§11.7): re-judgement from stored metrics without a job."""
    node = project.store.get_node(sha)
    store = project.store
    repo = project.paths["remote"]
    contract = project.contract

    attempt = store.next_attempt(sha)
    _set_state(project, sha, State.VERIFYING)

    ancestors = tree.ancestors(store, sha)
    baseline = store.baseline_for(sha, ancestors)
    baseline_sha = baseline["root_sha"] if baseline else None
    if baseline:
        store.set_baseline(sha, baseline["id"])

    # clean(sha): every path differing from the baseline must stay inside
    # the recipe. A node with no baseline root in its ancestry can never
    # be clean.
    baseline_paths = baseline_diff_paths(repo, sha, baseline_sha)
    dirty = [p for p in baseline_paths
             if not (p.startswith(".guardci/")
                     or match_any(p, contract.recipe))]
    if baseline_sha is None and node.parent is not None:
        dirty = dirty or ["<no baseline root in ancestry>"]

    # void propagation (verification.void):
    #  "descendants"    — every descendant of a rejected node is void,
    #                     permanently; the delta does not matter
    #  "until_restored" — only protected-set violations taint, and a
    #                     descendant is void only while its cumulative
    #                     delta is still dirty
    descendants = getattr(contract, "void_mode",
                          "descendants") == "descendants"
    for anc in ancestors:
        anode = store.get_node(anc)
        if anode is None:
            continue
        void_anc = anode.state == State.TAINTED or (
            anode.state == State.REJECTED and
            (descendants or _rejected_protected(store, anc)))
        if void_anc:
            if descendants or dirty:
                _set_state(project, sha, State.TAINTED)
                store.log_event("tainted", sha,
                                {"ancestor": anc, "dirty_paths": dirty})
                return True
            store.log_event("taint_cleared", sha, {"ancestor": anc})
            break

    # every node must descend from some baseline root — an orphan-pushed
    # history (`git checkout --orphan`) is rejected.
    if node.parent is None:
        if baseline_sha is not None and sha == baseline_sha:
            _set_state(project, sha, State.VERIFIED)
            store.log_event("baseline", sha, {"note": "baseline root"})
            git_refs.write_note(repo, sha, {"state": public_state(State.VERIFIED),
                                            "note": "baseline root"})
            return True
        _reject_not_baseline(project, sha)
        return True
    if baseline_sha is None:
        _reject_not_baseline(project, sha)
        return True

    diff = build_diff(repo, sha, node.parent)
    diff.baseline = baseline_sha
    paths = diff.paths()

    # core owns the `protected` tag; the flow adapter owns the rest
    tags = set()
    if any(_is_protected(p, contract.protected) for p in paths):
        tags.add(Tag.PROTECTED)
    tags |= set(project.flow.classify_delta(diff))
    store.set_tags(sha, tags)
    node.delta_tags = tags

    # §12.2: the checkpoint was captured at ingest (hook) — load it
    from .checkpoint import load_stored_checkpoint
    checkpoint = load_stored_checkpoint(project, sha)
    if checkpoint:
        node = store.get_node(sha)

    clauses = select_checks(contract, node, project.checkers)
    store.log_event("checks_selected", sha,
                    {"clauses": [c.check for c in clauses],
                     "tags": sorted(tags),
                     "attempt": attempt})

    awaiting: list[str] = []
    hard_fails: list[Verdict] = []
    warn_fails: list[Verdict] = []
    runs: list[CheckRun] = []

    static_clauses, nonstatic = _split_clauses(project, clauses)

    for clause in static_clauses:
        checker_cls = project.checkers.get(clause.check)
        verdict_status = None
        detail = ""
        if checker_cls is None:
            run = CheckRun(sha=sha, attempt=attempt,
                           checker=clause.name, backend=None,
                           cost=None, status="unavailable")
            verdict_status = "unavailable"
            detail = "checker not registered"
            if clause.severity != Severity.INFO:
                awaiting.append(clause.name)
        else:
            cost = getattr(checker_cls, "cost", None)
            try:
                checker = checker_cls()
                ctx = {
                    "project": project,
                    "node": node,
                    "checkpoint": checkpoint,
                    "metrics": node.metrics or {},
                    "changed_paths": paths,
                    "baseline_paths": baseline_paths,
                    "diff": diff,
                    "repo": repo,
                    "baseline_sha": baseline_sha,
                    "contract": contract,
                    "flow": project.flow,
                    "design": (project.cfg.get("design") or {}).get("design"),
                    "artifacts": None,
                }
                backend = project.backend_for(clause.check)
                result = checker.run(ctx, clause.params, backend)
                run = CheckRun(
                    sha=sha, attempt=attempt, checker=clause.name,
                    backend=backend.tool if backend else None,
                    cost=cost, status=result.status,
                    seconds=result.seconds, metrics=result.metrics,
                    evidence=[str(e) for e in result.evidence],
                    error=result.detail if result.status == "error" else None)
                verdict_status = result.status
                detail = result.detail
            except Exception as exc:  # noqa: BLE001  # fail-closed
                run = CheckRun(sha=sha, attempt=attempt,
                               checker=clause.name, backend=None,
                               cost=cost, status="error", error=str(exc))
                verdict_status = "error"
                detail = str(exc)
                if clause.severity != Severity.INFO:
                    awaiting.append(clause.name)
        store.add_check_run(run)
        runs.append(run)

        if verdict_status == "fail" and clause.severity == Severity.ROUTE:
            # force heavy verification for this node
            for c in contract.clauses:
                cc = project.checkers.get(c.check)
                if getattr(cc, "cost", None) == Cost.HEAVY and \
                        all(c.check != r.checker for r in runs) and \
                        all(c.check != n.check for n in nonstatic):
                    nonstatic.append(c)
            store.add_verdict(Verdict(sha=sha, attempt=attempt,
                                      clause=clause,
                                      status="fail", detail=detail))
            continue

        store.add_verdict(Verdict(sha=sha, attempt=attempt,
                                  clause=clause,
                                  status=verdict_status, detail=detail))
        if verdict_status == "fail":
            if clause.severity == Severity.HARD:
                hard_fails.append(Verdict(sha=sha, clause=clause,
                                          status="fail", detail=detail))
            elif clause.severity == Severity.WARN:
                warn_fails.append(Verdict(sha=sha, clause=clause,
                                          status="fail", detail=detail))
            # info: annotation only, does not affect the state
        elif verdict_status in ("unavailable", "error") and \
                clause.severity != Severity.INFO:
            # an info-severity clause records its error verdict but
            # cannot change the node's state — fail-closed does not
            # apply to annotations
            awaiting.append(clause.name)

    awaiting = sorted(set(awaiting))

    inv_fails = [v for v in hard_fails
                 if _clause_kind(project, v.clause.name) == "invariant"]
    exit_fails = [v for v in hard_fails
                  if _clause_kind(project, v.clause.name) != "invariant"]
    if inv_fails:
        _set_state(project, sha, State.REJECTED)
        protected_hit = any(v.clause.check == "recipe_scope"
                            for v in inv_fails)
        kind = "protected" if protected_hit else "metric"
        _propagate(project, sha, kind,
                   [v.clause.name for v in inv_fails])
        store.log_event("rejected", sha,
                        {"fails": [v.clause.name for v in inv_fails],
                         "taint": kind})
    elif exit_fails and nonstatic:
        # a hard exit fail decides `open`, but the job still runs so
        # the node carries every gap (§11.1)
        _defer_nonstatic(project, sha, attempt, baseline_sha,
                         nonstatic)
        node = store.get_node(sha)
        git_refs.write_note(repo, sha, {
            "state": public_state(node.state),
            "tags": sorted(node.delta_tags),
            "metrics": node.metrics,
            "coverage": node_coverage(project, sha),
            "checks": [{"checker": r.checker, "status": r.status}
                       for r in runs],
        })
        return True
    elif exit_fails:
        _set_state(project, sha, State.OPEN)
        store.log_event("open", sha,
                        {"fails": [v.clause.name
                                   for v in exit_fails]})
    elif awaiting:
        # stay in verifying, do not dequeue again automatically
        store.log_event("awaiting", sha, {"checkers": awaiting})
    elif nonstatic:
        # build+accept phase decides — see below
        _defer_nonstatic(project, sha, attempt, baseline_sha,
                         nonstatic)
        node = store.get_node(sha)
        git_refs.write_note(repo, sha, {
            "state": public_state(node.state),
            "tags": sorted(node.delta_tags),
            "metrics": node.metrics,
            "coverage": node_coverage(project, sha),
            "checks": [{"checker": r.checker, "status": r.status}
                       for r in runs],
        })
        return True
    else:
        # all clauses static: memoized metrics may be attached for
        # free, and every contract clause whose measurement ran is
        # evaluated too (§9.6 free clauses)
        memos = _attach_full_memo(project, sha, baseline_sha)
        node = store.get_node(sha)
        if node.metrics:
            mruns = [CheckRun(
                sha=sha, attempt=attempt, checker=f"measure:{m}",
                backend=memo.get("backend"),
                cost=(Cost.LIGHT if m == "lec" else Cost.HEAVY),
                status="pass", seconds=memo.get("seconds", 0.0),
                metrics={**(memo.get("metrics") or {}),
                         "memo_hit": True,
                         "memo_key": memo.get("key")},
                evidence=memo.get("evidence") or [])
                for m, memo in memos.items()]
            _decide(project, sha, attempt, [],
                    measurement_runs=mruns)
            return True
        if warn_fails:
            _set_state(project, sha, State.WARNED)
            store.log_event("warned", sha,
                            {"fails": [v.clause.name
                                       for v in warn_fails]})
        else:
            _set_state(project, sha, State.VERIFIED)
            store.log_event("verified", sha, {})

    node = store.get_node(sha)
    git_refs.write_note(repo, sha, {
        "state": public_state(node.state),
        "tags": sorted(node.delta_tags),
        "metrics": node.metrics,
        "coverage": node_coverage(project, sha),
        "checks": [{"checker": r.checker, "status": r.status}
                   for r in runs],
    })
    return True


# ------------------------------------------------------- §11.6 bisection


def _bisect_signature(project, sha, attempt) -> str | None:
    """The clause driving a bisectable failure at this attempt: a hard
    non-static invariant (``equivalence``) or a hard ratchet fail.
    ``open`` results never start a bisection."""
    for v in project.store.verdicts(sha):
        if v.attempt != attempt or v.status != "fail" or \
                v.clause.severity != Severity.HARD:
            continue
        c = v.clause.name
        if c.startswith("edge:"):
            return c
        if _clause_kind(project, c) == "invariant" and \
                _checker_cost(project, c) in (Cost.LIGHT, Cost.HEAVY):
            return c
    return None


def _node_fails_clause(store, sha, clause) -> bool:
    return any(v.status == "fail" and v.clause.name == clause
               for v in store.verdicts(sha))


def _queue_bisect_mid(project, mid: str):
    """Dispatch the bisection midpoint above normal priority: bump its
    queued job, or requeue the node like request-full."""
    store = project.store
    if store.queued_job_for(mid) is not None:
        store.job_bump(mid, 3)
        return "bumped"
    node = store.get_node(mid)
    if node is None or node.state not in (State.PENDING,
                                          State.VERIFYING):
        return "skipped"
    if node.state == State.VERIFYING:
        # verifying with no queued job: requeue through the static
        # phase so a job is (re)created under the current contract
        store.set_requested_full(mid, True)
        store.set_state(mid, State.PENDING)
        git_refs.set_state_ref(project.paths["remote"], mid, "pending")
    if mid not in store.queued():
        store.enqueue(mid, priority=3)
    return "enqueued"


def bisect_step(project, n_sha: str, clause: str) -> dict | None:
    """§11.6: between ``n_sha`` (fails ``clause``) and its nearest
    decided first-parent ancestor not failing it, dispatch the middle
    unverified commit. Returns the step payload or None."""
    store = project.store
    if (project.contract.verification or {}).get(
            "superseded", "defer") != "skip":
        return None
    undecided = []
    for anc in tree.ancestors(store, n_sha):
        anode = store.get_node(anc)
        if anode is None:
            continue
        decided = anode.state not in (State.PENDING, State.VERIFYING)
        if decided and not _node_fails_clause(store, anc, clause):
            break                       # bracket lower bound found
        if not decided:
            undecided.append(anc)
        # decided *with* the same failure: the transition lies below —
        # keep walking
    if not undecided:
        payload = {"first_bad": n_sha, "clause": clause}
        store.log_event("bisect_result", n_sha, payload)
        return payload
    mid = undecided[len(undecided) // 2]
    how = _queue_bisect_mid(project, mid)
    payload = {"of": n_sha, "mid": mid, "clause": clause,
               "open_bracket": len(undecided), "how": how}
    store.log_event("bisect_step", n_sha, payload)
    return payload


def _bisect_advance(project, sha, attempt):
    """After a decide: (a) start a bisection if this node newly shows
    a bisectable failure; (b) re-split every open bisection — a
    midpoint that just decided narrows the bracket."""
    store = project.store
    sig = _bisect_signature(project, sha, attempt)
    if sig is not None:
        node = store.get_node(sha)
        if node is not None and node.state == State.REJECTED:
            bisect_step(project, sha, sig)
    # open bisections: bisect_step events without a bisect_result
    open_bs: dict[str, str] = {}
    for e in store.events():
        if e["kind"] == "bisect_step":
            open_bs[e["sha"]] = (json.loads(e["payload"] or "{}")
                                 .get("clause"))
        elif e["kind"] == "bisect_result":
            open_bs.pop(e["sha"], None)
    for n_sha, clause in open_bs.items():
        if clause and n_sha != sha:
            bisect_step(project, n_sha, clause)


def _defer_nonstatic(project, sha, attempt, baseline_sha, nonstatic):
    """Non-static clauses: memo hit → decide inline; else queue a job
    (or await an executor)."""
    store = project.store
    if getattr(project, "rejudging", False):
        # §11.7 rejudge: judge every contract clause on the stored
        # metrics — no executor, no job, no memo lookup. The stored
        # build outcome is carried into the new attempt: a failed
        # build is the whole story (open with the build gap; no
        # measurement clause is evaluated or awaited).
        all_nonstatic = [c for c in project.contract.clauses
                         if project.checkers.get(c.check) is not None
                         and _checker_cost(project, c.check)
                         in (Cost.LIGHT, Cost.HEAVY)]
        prev_build = [r for r in store.check_runs(sha,
                                                  all_attempts=True)
                      if r.checker == "build" and r.attempt < attempt]
        prev_bv = [v for v in store.verdicts(sha, all_attempts=True)
                   if v.clause.check == "build"
                   and v.attempt < attempt]
        if prev_build:
            br = prev_build[-1]
            bv = next((v for v in reversed(prev_bv)
                       if v.attempt == br.attempt), None)
            build_run = CheckRun(
                sha=sha, attempt=attempt, checker="build",
                backend=br.backend, cost=br.cost, status=br.status,
                seconds=br.seconds, metrics=br.metrics,
                evidence=br.evidence)
            build_verdict = Verdict(
                sha=sha, attempt=attempt, clause=_build_clause(),
                status=(bv.status if bv else "pass"),
                detail=(bv.detail if bv else ""))
            if build_verdict.status == "fail":
                _decide(project, sha, attempt, [],
                        build_verdict=build_verdict,
                        build_run=build_run)
                return
            _decide(project, sha, attempt, all_nonstatic or nonstatic,
                    build_verdict=build_verdict, build_run=build_run)
            return
        _decide(project, sha, attempt, all_nonstatic or nonstatic)
        return
    if project.executor is None:
        store.log_event("awaiting", sha,
                        {"checkers": sorted(c.name for c in nonstatic),
                         "reason": "no executor configured"})
        return
    block = getattr(project, "job_blocklist", None)
    if block and sha in block:
        # replay driver: an uncaptured placeholder sits in this
        # commit's recipe closure — a job would fail on corrupt
        # inputs and poison the shared cache; stay `verifying` (§10.1)
        store.log_event("awaiting", sha,
                        {"checkers": sorted(c.name for c in nonstatic),
                         "reason": "unreplayable placeholders"})
        return
    verification = project.contract.verification or {}
    source = verification.get("source", "rebuild")
    if source == "artifact":
        cp = (node.metrics or {}).get("_checkpoint")             if (node := store.get_node(sha)) else None
        if cp is not None:
            _queue_artifact_job(project, sha, attempt, baseline_sha,
                                nonstatic, node)
            return
        fb = verification.get("fallback", "rebuild")
        if fb == "none":
            _attach_metrics(project, sha, {
                "_gaps": {**(node.metrics or {}).get("_gaps", {}),
                          "checkpoint": {"reason": "no usable "
                                         "checkpoint"}}})
            _set_state(project, sha, State.OPEN)
            store.log_event("open", sha,
                            {"gaps": {"checkpoint":
                                      {"reason": "no usable "
                                       "checkpoint"}}})
            return
        # fallback rebuild → today's job below
    view = _job_view(project)
    needs, upto = _measurements_for(project, nonstatic, sha)
    keys = _stage_keys(project, sha, baseline_sha)
    constraints = _golden_constraints(project, baseline_sha)
    memos = _memos_for(view, keys, upto, needs, constraints)
    if memos is not None and not _baseline_metrics(project,
                                                   baseline_sha):
        # all measurement memos hit, but the golden metrics are not
        # available yet (shared cache, baseline.json not populated):
        # still queue a job so ensure_baseline fills them in — it
        # will hit the same memos and cost no extra builds
        memos = None
    if memos is not None:
        metrics = {}
        runs = []
        for m, memo in memos.items():
            metrics.update(memo["metrics"])
            runs.append(CheckRun(
                sha=sha, attempt=attempt, checker=f"measure:{m}",
                backend=memo.get("backend"),
                cost=(Cost.LIGHT if m == "lec" else Cost.HEAVY),
                status="pass", seconds=memo.get("seconds", 0.0),
                metrics={**(memo.get("metrics") or {}),
                         "memo_hit": True,
                         "memo_key": memo.get("key")},
                evidence=memo.get("evidence") or []))
        _attach_metrics(project, sha, metrics)
        store.log_event("memo_hit", sha,
                        {"measurements": sorted(memos)})
        _decide(project, sha, attempt, nonstatic,
                measurement_runs=runs)
        return
    node = store.get_node(sha)
    prio = 2 if node.requested_full else \
        (1 if not store.children(sha) else 0)
    store.add_job(sha=sha, upto_stage=upto,
                  measurements=sorted(needs), params=needs,
                  source="rebuild",
                  clauses=[c.name for c in nonstatic],
                  baseline_sha=baseline_sha, attempt=attempt,
                  prio_class=prio,
                  draft=False,
                  node_created=node.created_at or time.time())
    store.log_event("job_queued", sha,
                    {"upto_stage": upto,
                     "measurements": sorted(needs)})


def _attach_full_memo(project, sha, baseline_sha) -> dict:
    """All-static node with a memo for its full-recipe key: attach the
    memoized metrics for free (§9.1). Returns {measurement: memo}."""
    if project.executor is None:
        return {}
    view = _job_view(project)
    last = project.flow.stages()[-1]
    keys = _stage_keys(project, sha, baseline_sha)
    nonstatic = [c for c in project.contract.clauses
                 if project.checkers.get(c.check) is not None
                 and _checker_cost(project, c.check) in
                 (Cost.LIGHT, Cost.HEAVY)]
    needs, _ = _measurements_for(project, nonstatic, sha)
    depth_key = keys.get(last)
    constraints = _golden_constraints(project, baseline_sha)
    memos = {}
    if depth_key:
        # free metric attach (§9.6): whatever the stage key / artifact
        # digests already prove is attached without a job
        for m, params in needs.items():
            memo = verify_mod.memo_lookup(view, depth_key, m, params,
                                          constraints)
            if memo is not None:
                memos[m] = memo
    metrics = {}
    for memo in memos.values():
        metrics.update(memo.get("metrics") or {})
    if metrics:
        _attach_metrics(project, sha, metrics)
    return memos


# -------------------------------------------------------------- Scheduler


class Scheduler:
    """`serve` driver: drain static queue, dispatch jobs to the worker
    pool, collect finished futures, apply the idle policy (§9.8)."""

    def __init__(self, project, idle_heavy: bool = False):
        self.project = project
        self.idle_heavy = idle_heavy
        n = project.executor.slots() if project.executor else 0
        self.nslots = max(n, 1)
        self.pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.nslots)
        self.futures: dict = {}       # future -> job row
        self.view = None              # lazily built JobView
        self._desc_cache: dict = {}   # sha -> has descendant (per tick)
        self._superseded_logged: set = set()
        self._bisects: dict = {}      # sha -> clause (open bisections)
        project.store.reset_running_jobs()

    def _job_view(self):
        if self.view is None:
            self.view = _job_view(self.project)
        return self.view

    def _superseded_mode(self) -> str:
        return (self.project.contract.verification or {}).get(
            "superseded", "defer")

    def _has_descendant(self, sha: str) -> bool:
        """Queued job is superseded when its commit already has a
        descendant node (§11.6)."""
        if sha in self._desc_cache:
            return self._desc_cache[sha]
        out = bool(tree.descendants(self.project.store, sha))
        self._desc_cache[sha] = out
        return out

    def _dispatch(self):
        if self.project.executor is None:
            return
        free = self.nslots - len(self.futures)
        if free <= 0:
            return
        mode = self._superseded_mode()
        jobs = []
        for j in self.project.store.queued_jobs():
            # a voided node is never measured — a violation that
            # propagates while its job sits in the queue cancels it
            get_node = getattr(self.project.store, "get_node", None)
            jn = get_node(j["sha"]) if get_node else None
            if jn is not None and jn.state == State.TAINTED:
                self.project.store.job_state(j["id"], "skipped")
                continue
            jobs.append(j)
        forced, normal, superseded = [], [], []
        for j in jobs:
            if j["prio_class"] >= 2:
                forced.append(j)
            elif self._has_descendant(j["sha"]):
                superseded.append(j)
            else:
                normal.append(j)
        runnable = forced + normal
        if not runnable and mode == "defer":
            runnable = superseded
        elif mode == "skip":
            if self.idle_heavy and not runnable:
                # §11.6: superseded jobs run on the idle policy
                runnable = superseded
            else:
                for j in superseded:
                    if j["id"] not in self._superseded_logged:
                        self._superseded_logged.add(j["id"])
                        self.project.store.log_event(
                            "superseded", j["sha"],
                            {"job": j["id"]})
        for job in runnable[:free]:
            self.project.store.job_state(job["id"], "running")
            spec = verify_mod.JobSpec(
                sha=job["sha"], baseline_sha=job["baseline_sha"],
                upto_stage=job["upto_stage"],
                measurements=[m for m in job["measurements"]
                              if not m.startswith("_")],
                params=job["params"], attempt=job["attempt"],
                # request-full (prio 2) and bisection (prio 3)
                # complete a predicted-open node — a prediction never
                # yields a pass but may also never mask a full run
                no_predict=job["prio_class"] >= 2)
            spec.source = job.get("source") or "rebuild"
            spec.checkpoint_path = (job.get("params") or {}).get(
                "_checkpoint")
            runner = (verify_mod.run_artifact_job
                      if spec.source == "artifact"
                      else verify_mod.run_job)
            fut = self.pool.submit(runner, self._job_view(), spec)
            self.futures[fut] = job

    def _collect(self):
        collected = False
        for fut, job in list(self.futures.items()):
            if not fut.done():
                continue
            del self.futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001 — infra failure
                self._job_error(job, exc)
                continue
            finalize(self.project, job, result)
            collected = True
        # bound the stage cache (§9.10) after finished jobs only —
        # gc walks the whole cache
        if collected:
            limit_gb = self.project.cfg.get("cache_max_gb")
            if limit_gb:
                self.project.cache.gc(int(limit_gb) * (1 << 30))

    def _job_error(self, job, exc):
        store = self.project.store
        store.log_event("job_error", job["sha"],
                        {"error": str(exc), "job": job["id"]})
        if job["retries"] < 1:
            store.job_retry(job["id"])
        else:
            store.job_state(job["id"], "error", error=str(exc))

    def _idle_candidate(self):
        """Newest verified/warned leaf with *partial* coverage that has
        been a leaf ≥ t_leaf_seconds (§9.8 idle policy)."""
        store = self.project.store
        if self.idle_heavy:
            # a manual --idle-heavy run treats t_leaf as 0 (§9.8)
            t_leaf = 0.0
        else:
            t_leaf_cfg = self.project.cfg.get("t_leaf_seconds")
            t_leaf = float(600 if t_leaf_cfg is None else t_leaf_cfg)
        now = time.time()
        best = None
        for node in store.all_nodes():
            if node.state == State.OPEN and \
                    (node.metrics or {}).get("_predicted"):
                # §11.4: a predicted-open node completes on the idle
                # policy (and on request-full)
                if best is None or node.created_at > best.created_at:
                    best = node
                continue
            if node.state not in (State.VERIFIED, State.WARNED):
                continue
            if node_coverage(self.project, node.sha) == "full":
                continue
            if store.children(node.sha):
                continue
            if now - (node.updated_at or node.created_at) < t_leaf:
                continue
            if best is None or node.created_at > best.created_at:
                best = node
        return best

    def _idle_dispatch(self):
        if not self.idle_heavy or self.project.executor is None:
            return
        if self.futures or self.project.store.queued_jobs():
            return
        node = self._idle_candidate()
        if node is None:
            return
        # full check set: every non-static contract clause
        clauses = [c for c in self.project.contract.clauses
                   if _checker_cost(self.project, c.check)
                   in (Cost.LIGHT, Cost.HEAVY)]
        needs, upto = _measurements_for(self.project, clauses,
                                        node.sha)
        attempt = self.project.store.next_attempt(node.sha)
        _set_state(self.project, node.sha, State.VERIFYING)
        self.project.store.add_job(
            sha=node.sha, upto_stage=upto,
            measurements=sorted(needs), params=needs,
            clauses=[c.name for c in clauses],
            baseline_sha=_baseline_sha_for(self.project, node.sha),
            attempt=attempt,
            prio_class=(2 if (node.metrics or {}).get("_predicted")
                        else 0),
            draft=False,
            node_created=node.created_at or time.time())
        self.project.store.log_event(
            "job_queued", node.sha,
            {"upto_stage": upto, "measurements": sorted(needs),
             "idle": True})

    def tick(self) -> bool:
        """One scheduler iteration; True when work happened."""
        self._desc_cache = {}
        did = False
        while process_once(self.project):
            did = True
        self._collect()
        self._dispatch()
        self._idle_dispatch()
        self._dispatch()
        return did or bool(self.futures) or \
            bool(self.project.store.queued_jobs())

    def busy(self) -> bool:
        return (self.project.store.queue_len() > 0
                or bool(self.project.store.queued_jobs())
                or any(not f.done() for f in self.futures))

    def run(self, once: bool, interval: float = 2.0):
        try:
            while True:
                self.tick()
                if once:
                    if self.busy():
                        time.sleep(min(interval, 0.5))
                        continue
                    # nothing queued — but a future may have finished
                    # without being collected yet
                    if self.futures:
                        continue
                    break
                else:
                    time.sleep(interval)
        finally:
            self.pool.shutdown(wait=False)


def finalize(project, job: dict, result: verify_mod.JobResult):
    """Apply a finished job's result to the ledger (§9.1 verdict
    phase). Runs only in the scheduler thread."""
    store = project.store
    sha = job["sha"]
    attempt = job["attempt"]

    # a node voided while its job was running is never measured —
    # discard the result and keep the void state
    fnode = store.get_node(sha)
    if fnode is not None and fnode.state == State.TAINTED:
        store.job_state(job["id"], "skipped")
        store.log_event("job_discarded", sha,
                        {"job": job["id"], "reason": "void"})
        return

    # baseline root: attach golden metrics whenever the node lacks
    # them — populated by this job or already present in a shared
    # cache (§9.4)
    bnode = store.get_node(job["baseline_sha"])
    if bnode is not None and not bnode.metrics:
        bmetrics = result.baseline_metrics or \
            (result.baseline or {}).get("metrics")
        if bmetrics:
            store.set_metrics(job["baseline_sha"], bmetrics)
            _reevaluate_descendant_edges(project, job["baseline_sha"])

    if result.checkpoint_missing:
        # §12.1: unreadable/missing artifact → verification.fallback
        store.job_state(job["id"], "done")
        fb = (project.contract.verification or {}).get(
            "fallback", "rebuild")
        store.log_event("checkpoint_fallback", sha,
                        {"reason": result.checkpoint_missing,
                         "fallback": fb})
        if fb == "none":
            _attach_metrics(project, sha, {
                "_gaps": {"checkpoint":
                          {"reason": result.checkpoint_missing}}})
            _set_state(project, sha, State.OPEN)
            store.log_event("open", sha, {
                "gaps": {"checkpoint":
                         {"reason": result.checkpoint_missing}}})
            git_refs.write_note(project.paths["remote"], sha,
                                {"state": public_state(State.OPEN),
                                 "gaps": {"checkpoint":
                                          result.checkpoint_missing}})
            return
        # fallback rebuild → queue today's build job
        nonstatic = [c for c in project.contract.clauses
                     if project.checkers.get(c.check) is not None
                     and _checker_cost(project, c.check)
                     in (Cost.LIGHT, Cost.HEAVY)]
        needs, upto = _measurements_for(project, nonstatic, sha)
        node = store.get_node(sha)
        store.add_job(sha=sha, upto_stage=upto,
                      measurements=sorted(needs), params=needs,
                      clauses=[c.name for c in nonstatic],
                      baseline_sha=job["baseline_sha"], attempt=attempt,
                      prio_class=job["prio_class"], draft=False,
                      node_created=node.created_at or time.time(),
                      source="rebuild")
        return
    if result.error:
        store.job_state(job["id"], "error", error=result.error)
        store.log_event("job_error", sha,
                        {"error": result.error, "job": job["id"]})
        if job["retries"] < 1:
            store.job_retry(job["id"])
        return

    cost_metrics = {
        "stages": result.stages,
        "build_seconds": result.build_seconds,
        "accept_seconds": result.accept_seconds,
        "stages_built": result.stages_built,
        "stages_reused": result.stages_reused,
        "stage_cutoff": result.stage_cutoff,
        "saved_seconds": result.saved_seconds,
        "flow_seconds": (result.baseline or {}).get("flow_seconds"),
    }
    if not result.ok:
        # build failure is a verdict (§9.3): implicit hard clause `build`
        detail = (f"timeout at stage {result.failed_stage}"
                  if result.timed_out else
                  f"stage {result.failed_stage} failed")
        rc = next((r["rc"] for r in result.stages
                   if r["stage"] == result.failed_stage), None)
        if rc is not None:
            detail += f" rc={rc}"
        if result.logs.get(result.failed_stage):
            detail += f" (log {result.logs[result.failed_stage]})"
        store.job_state(job["id"], "done")
        run = CheckRun(sha=sha, attempt=attempt, checker="build",
                       backend=None, cost=None, status="fail",
                       seconds=result.build_seconds,
                       metrics=cost_metrics,
                       evidence=[str(p) for p in result.logs.values()])
        verdict = Verdict(sha=sha, attempt=attempt,
                          clause=_build_clause(), status="fail",
                          detail=detail)
        _decide(project, sha, attempt, [], build_verdict=verdict,
                build_run=run)
        return

    store.job_state(job["id"], "done")
    _attach_metrics(project, sha, result.metrics)

    mruns = []
    if result.predicted is not None:
        mruns.append(CheckRun(
            sha=sha, attempt=attempt, checker="measure:estimate",
            backend="openroad", cost=None, status="fail",
            seconds=None,
            metrics={"est_setup_wns":
                     result.predicted.get("est_setup_wns")}))
    for m, info in result.measurements.items():
        mruns.append(CheckRun(
            sha=sha, attempt=attempt, checker=f"measure:{m}",
            backend=info.get("backend"),
            cost=(Cost.LIGHT if m == "lec" else Cost.HEAVY),
            status="pass", seconds=info.get("seconds", 0.0),
            metrics={**(info.get("metrics") or {}),
                     "memo_hit": info.get("memo_hit"),
                     "memo_key": info.get("key")},
            evidence=info.get("evidence") or []))
    build_run = CheckRun(sha=sha, attempt=attempt, checker="build",
                         backend=None, cost=None, status="pass",
                         seconds=result.build_seconds,
                         metrics=cost_metrics,
                         evidence=[str(p) for p in result.logs.values()])
    build_verdict = Verdict(sha=sha, attempt=attempt,
                            clause=_build_clause(), status="pass",
                            detail="")
    clause_by_name = {c.name: c for c in project.contract.clauses}
    nonstatic = [clause_by_name[n] for n in job["clauses"]
                 if n in clause_by_name]
    _decide(project, sha, attempt, nonstatic,
            build_verdict=build_verdict, build_run=build_run,
            measurement_runs=mruns,
            predicted=result.predicted)


def _queue_artifact_job(project, sha, attempt, baseline_sha, nonstatic,
                        node):
    """§12.4 job: measure stage facts from the captured checkpoint —
    no build.  The stage facts themselves are the measurement;
    signoff/drc run only when the node's stage is the last stage."""
    store = project.store
    last = project.flow.stages()[-1]
    upto = (node.metrics or {}).get("stage") or last
    needs = {"facts": {}, "_checkpoint":
             (node.metrics or {}).get("_checkpoint")}
    # clause needs run at every stage — `lec` chains from the synth
    # artifact onward; signoff/drc only execute at the last stage
    # (run_artifact_job gates on it)
    for c in nonstatic:
        cls = project.checkers.get(c.check)
        if cls is None:
            continue
        for meas in _clause_needs(cls, c.params):
            needs.setdefault(meas, {}).update(c.params)
    signoff = needs.get("signoff")
    if signoff is not None:
        # §11.3: signoff facts are measured at the golden constraint
        # period, resolved at job execution — no contract params here
        needs["signoff"] = {k: v for k, v in signoff.items()
                            if k == "timeout"}
    prio = 2 if node.requested_full else \
        (1 if not store.children(sha) else 0)
    store.add_job(sha=sha, upto_stage=upto,
                  measurements=sorted(needs), params=needs,
                  clauses=[c.name for c in nonstatic],
                  baseline_sha=baseline_sha, attempt=attempt,
                  prio_class=prio, draft=False,
                  node_created=node.created_at or time.time(),
                  source="artifact")
    store.log_event("job_queued", sha,
                    {"upto_stage": upto, "source": "artifact",
                     "measurements": sorted(needs)})
