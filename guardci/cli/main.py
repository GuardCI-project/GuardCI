"""guardci command-line interface.

Commands: init · serve · status · log · request-full · deliver
(replay lands in M3).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import click
import yaml

from ..contract import loader as cl
from ..core.models import State, public_state
from ..core.project import DEFAULTS, Project, find_project
from ..core.rejudge import rejudge as core_rejudge
from ..core.store import Store
from ..remote import refs as git_refs
from ..remote.repo import init_bare

# log annotations use the paper's public state words
SYMBOLS = {
    "accepted": "✓",
    "unfinished": "○",
    "violation": "✗",
    "void": "⊘",
    "pending": "·",
}


def _project(ctx) -> Project:
    opt = ctx.obj.get("project") if ctx.obj else None
    if not opt and ctx.params.get("project"):
        opt = ctx.params["project"]
    root = Path(opt).resolve() if opt else find_project()
    if root is None:
        raise click.UsageError(
            "no GuardCI project found: pass --project, set GUARDCI_PROJECT, "
            "or run inside a project/clone directory")
    return Project(root)


def _git(repo: str, *args: str, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, check=check)


@click.group()
@click.option("--project", "project_dir", default=None,
              type=click.Path(exists=True, file_okay=False),
              help="project directory (default: GUARDCI_PROJECT or upward "
                   "search for guardci.yaml)")
@click.pass_context
def cli(ctx, project_dir):
    """GuardCI — a git remote with continuous verification."""
    ctx.ensure_object(dict)
    ctx.obj["project"] = project_dir


# ---------------------------------------------------------------- init

@cli.command()
@click.argument("directory", type=click.Path())
@click.option("--contract", "contract_ref", default="orfs-default",
              help="contract pack: builtin name or YAML path")
@click.option("--flow", default="null", help="flow adapter name")
@click.option("--from", "from_dir", default=None,
              type=click.Path(exists=True, file_okay=False),
              help="seed the remote with this directory as the root commit")
@click.option("--platform", default=None,
              help="ORFS platform (required with --flow orfs)")
@click.option("--design", default=None,
              help="ORFS design name (required with --flow orfs)")
@click.option("--reset-port", default=None,
              help="design reset port (enables equivalence tier 3)")
@click.option("--reset-active", "reset_active", default=None,
              type=click.Choice(["high", "low"]),
              help="reset polarity (default high)")
@click.option("--reset-cycles", default=None, type=int,
              help="cycles the reset is held in co-simulation (def 2)")
@click.option("--image", default=None,
              help="executor image (pinned docker image id)")
@click.option("--slots", default=None, type=int,
              help="executor slot count")
@click.option("--cpus-per-slot", default=None, type=int)
@click.option("--memory", default=None, help="container memory limit")
@click.option("--lock-dir", default=None,
              help="CPU-slot lock directory")
@click.option("--cache", "cache_dir", default=None,
              help="stage cache directory (may be shared)")
@click.option("--cache-max-gb", default=None, type=int,
              help="stage cache bound in GiB (LRU)")
@click.option("--recorder", "recorder_type", default=None,
              help="checkpoint recorder type (§12.2): null | workspace")
@click.option("--workspace", "workspace_dir", default=None,
              help="workspace directory for the workspace recorder")
def init(directory, contract_ref, flow, from_dir, platform, design,
         reset_port, reset_active, reset_cycles,
         image, slots, cpus_per_slot, memory, lock_dir, cache_dir,
         cache_max_gb, recorder_type, workspace_dir):
    """Create a GuardCI project directory."""
    if flow == "orfs" and (not platform or not design):
        raise click.UsageError("--flow orfs requires --platform and --design")
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "contracts").mkdir(exist_ok=True)
    (root / "artifacts").mkdir(exist_ok=True)

    # copy the contract into the project (resolves builtins too)
    from ..contract.loader import _resolve
    src = _resolve(contract_ref, Path.cwd())
    dst = root / "contracts" / src.name
    shutil.copyfile(src, dst)

    cfg = dict(DEFAULTS)
    cfg["flow"] = flow
    cfg["contract"] = f"contracts/{src.name}"
    if flow == "orfs":
        cfg["design"] = {"platform": platform, "design": design}
        if reset_port:
            cfg["design"]["reset"] = {
                "port": reset_port,
                "active": 1 if (reset_active or "high") == "high"
                else 0,
                "cycles": reset_cycles if reset_cycles is not None
                else 2}
    executor = dict(cfg.get("executor") or {})
    if image:
        executor["image"] = image
    for key, val in (("slots", slots), ("cpus_per_slot", cpus_per_slot),
                     ("memory", memory), ("lock_dir", lock_dir)):
        if val is not None:
            executor[key] = val
    cfg["executor"] = executor
    if cache_dir:
        cfg["cache"] = cache_dir
    if cache_max_gb is not None:
        cfg["cache_max_gb"] = cache_max_gb
    if recorder_type or workspace_dir:
        rec = {"type": recorder_type or "workspace"}
        if workspace_dir:
            rec["path"] = str(Path(workspace_dir).resolve())
        cfg["recorder"] = rec
    (root / "guardci.yaml").write_text(
        yaml.safe_dump(cfg, sort_keys=False))

    remote = init_bare(root / cfg["remote"], root)

    # always seed: the pushed seed becomes baseline 0 and the only
    # legitimate root commit
    store = Store(root / cfg["state"])
    try:
        seed_sha = _seed_remote(remote, Path(from_dir).resolve()
                                if from_dir else None,
                                flow=flow, platform=platform, design=design)
        store.add_baseline(note="baseline 0 (init seed)",
                           root_sha=seed_sha, baseline_id=0)
    finally:
        store.close()

    ver = yaml.safe_load(dst.read_text()).get("verification") or {}
    if ver.get("source") == "artifact" and \
            (cfg.get("recorder") or {}).get("type", "null") == "null":
        click.echo("warning: contract uses source: artifact but no "
                   "recorder is configured — every node will be "
                   "unfinished with gap `checkpoint`", err=True)
    click.echo(f"guardci project: {root}")
    click.echo(f"clone with:      git clone {remote}")


def _seed_remote(remote: Path, src: Path | None, flow: str = "null",
                 platform: str | None = None,
                 design: str | None = None) -> str:
    """Commit the seed as the root commit, push it through the hook,
    and return its sha (recorded as baseline 0)."""
    with tempfile.TemporaryDirectory() as td:
        _git(td, "init", "-b", "main")
        if src is None:
            (Path(td) / ".guardci-seed").write_text("")
        elif flow == "orfs":
            _orfs_seed(src, Path(td), platform, design)
        else:
            _copy_seed(src, Path(td))
        _git(td, "add", "-A")
        _git(td, "-c", "user.name=guardci", "-c",
             "user.email=guardci@localhost", "commit", "-m", "seed",
             "--quiet")
        sha = _git(td, "rev-parse", "HEAD").stdout.strip()
        r = _git(td, "push", str(remote), "HEAD:main", check=False)
        if r.returncode != 0:
            raise click.ClickException(
                f"seed push failed:\n{r.stderr.strip()}")
        return sha


_SEED_SKIP_DIRS = {"results", "logs", "reports", "objects", ".git"}
_SEED_SKIP_RE = re.compile(r"^core\.\d+$")


def _copy_seed(src: Path, dst: Path):
    """Copy the whole source tree, excluding build outputs, crash dumps
    and .git — the baseline must mirror the agent's workspace exactly
    (anything not seeded is invisible to the contract)."""
    def ignore(_dir, names):
        return {n for n in names
                if n in _SEED_SKIP_DIRS or _SEED_SKIP_RE.match(n)}

    for item in Path(src).iterdir():
        name = item.name
        if name in _SEED_SKIP_DIRS or _SEED_SKIP_RE.match(name):
            continue
        out = Path(dst) / name
        if item.is_dir():
            shutil.copytree(item, out, ignore=ignore)
        elif item.is_file():
            shutil.copyfile(item, out)


def _orfs_seed(src: Path, dst: Path, platform: str, design: str):
    """Seed an ORFS flow directory: whole workspace + impl.sdc wiring."""
    from ..flows.orfs import config as cfg_mod

    _copy_seed(src, dst)

    # .gitignore: output dirs are not tracked state
    gi = dst / ".gitignore"
    entries = {"results/", "logs/", "reports/", "objects/"}
    if gi.exists():
        entries |= {l.strip() for l in gi.read_text().splitlines()
                    if l.strip()}
    gi.write_text("\n".join(sorted(entries)) + "\n")

    # impl.sdc: mutable copy of the golden constraint; point SDC_FILE at it
    design_dir = dst / f"designs/{platform}/{design}"
    sdc = design_dir / "constraint.sdc"
    if sdc.exists() and not (design_dir / "impl.sdc").exists():
        shutil.copyfile(sdc, design_dir / "impl.sdc")
    cfg_path = design_dir / "config.mk"
    if cfg_path.exists():
        lines = cfg_path.read_text().splitlines()
        out, done = [], False
        for ln in lines:
            if not done and cfg_mod.lhs_name(ln) == "SDC_FILE":
                out.append("export SDC_FILE = $(DESIGN_DIR)/impl.sdc")
                done = True
            else:
                out.append(ln)
        if not done:
            out.append("export SDC_FILE = $(DESIGN_DIR)/impl.sdc")
        cfg_path.write_text("\n".join(out) + "\n")


def _project_opt(f):
    """Allow --project after the subcommand as well as at group level."""
    return click.option("--project", default=None, expose_value=True,
                        type=click.Path(exists=True, file_okay=False),
                        help="project directory")(f)


# ---------------------------------------------------------------- serve

@cli.command()
@_project_opt
@click.option("--once", is_flag=True, help="drain the queue and exit")
@click.option("--idle-heavy", is_flag=True,
              help="with --once, also run idle-leaf heavy upgrades "
                   "(t_leaf = 0)")
@click.option("--interval", default=2.0, show_default=True,
              help="poll interval in seconds")
@click.pass_context
def serve(ctx, once, interval, idle_heavy, project):
    """Run the scheduler."""
    from ..core.scheduler import Scheduler
    project = _project(ctx)
    try:
        Scheduler(project, idle_heavy=idle_heavy).run(
            once=once, interval=interval)
    finally:
        project.close()


# ---------------------------------------------------------------- status

def _status_payload(project: Project, sha: str) -> dict:
    node = project.store.get_node(sha)
    if node is None:
        return {"sha": sha, "state": "unknown"}
    verdicts = [{
        "check": v.clause.name, "severity": v.clause.severity.value,
        "status": v.status, "detail": v.detail,
    } for v in project.store.verdicts(node.sha)]
    awaiting = []
    job_state = None
    for ev in project.store.events(node.sha):
        if ev["kind"] == "awaiting":
            awaiting = json.loads(ev["payload"]).get("checkers", [])
        elif ev["kind"] == "job_error":
            job_state = {"error": json.loads(ev["payload"]).get("error")}
    jobs = project.store.jobs_for(node.sha)
    if jobs and jobs[-1]["state"] in ("queued", "running"):
        job_state = {"state": jobs[-1]["state"],
                     "upto_stage": jobs[-1]["upto_stage"],
                     "measurements": jobs[-1]["measurements"]}
    edges_rows = None
    build = None
    accept_seconds = 0.0
    for run in project.store.check_runs(node.sha):
        if run.checker == "edges":
            edges_rows = run.metrics
        elif run.checker == "build":
            build = run.metrics
        elif run.checker.startswith("measure:"):
            accept_seconds += run.seconds or 0.0
    cost = None
    if build:
        cost = {"build_seconds": build.get("build_seconds"),
                "stages_built": build.get("stages_built"),
                "stages_reused": build.get("stages_reused"),
                "saved_seconds": build.get("saved_seconds"),
                "accept_seconds": round(accept_seconds, 3)}
    from ..core.scheduler import node_coverage
    from ..core.status import public_status
    m = node.metrics or {}
    pub = public_status(project, sha) or {}
    verdicts_out = []
    for v in verdicts:
        v = dict(v)
        if v["status"] == "fail":
            from ..core.status import finding_for
            v["finding"] = finding_for(project, v["check"])
        verdicts_out.append(v)
    return {
        "sha": node.sha,
        "stage": m.get("stage"),
        "verdict": pub.get("verdict"),
        "grade": pub.get("grade"),
        "warnings": pub.get("warnings"),
        "findings": pub.get("findings"),
        "provenance": m.get("provenance"),
        "pending": sorted({v.clause.name
                           for v in project.store.verdicts(node.sha)
                           if v.status == "pending"}),
        "parent": node.parent,
        "state": node.state.value,
        "gaps": m.get("_gaps"),
        "min_period": m.get("min_period"),
        "attempt": node.attempt,
        "coverage": node_coverage(project, node.sha),
        "tags": sorted(node.delta_tags),
        "metrics": node.metrics,
        "requested_full": node.requested_full,
        "verdicts": verdicts_out,
        "awaiting": awaiting,
        "edges": edges_rows,
        "cost": cost,
        "job": job_state,
    }


@cli.command()
@_project_opt
@click.argument("sha", required=False)
@click.option("--wait", is_flag=True,
              help="poll until the node leaves pending/verifying")
@click.option("--json", "as_json", is_flag=True)
@click.option("--interval", default=2.0, show_default=True)
@click.pass_context
def status(ctx, sha, wait, as_json, interval, project):
    """Show a node's verification status (default: git HEAD of cwd)."""
    project = _project(ctx)
    try:
        if not sha:
            r = subprocess.run(["git", "rev-parse", "HEAD"],
                               check=False, capture_output=True, text=True)
            if r.returncode != 0:
                raise click.UsageError(
                    "no SHA given and cwd is not a git repo")
            sha = r.stdout.strip()
        while True:
            payload = _status_payload(project, sha)
            st = payload.get("state")
            if as_json:
                out = json.dumps(payload, indent=1, sort_keys=True)
            else:
                from ..core.status import verdict_text
                lines = [f"{sha[:12]}  {verdict_text(payload)}"
                         + (f" ({payload['coverage']} coverage)"
                            if payload.get("coverage") else "")]
                if payload.get("stage"):
                    lines.append(f"  stage: {payload['stage']}")
                if payload.get("provenance"):
                    lines.append(
                        f"  provenance: {payload['provenance']}")
                if payload.get("pending"):
                    lines.append("  pending: "
                                 + ", ".join(payload["pending"]))
                if payload.get("tags"):
                    lines.append(f"  tags: {', '.join(payload['tags'])}")
                vmark = {"pass": "✓", "fail": "✗", "error": "!",
                         "unavailable": "?", "skipped": "·"}
                for v in payload.get("verdicts", []):
                    lines.append(
                        f"  {vmark.get(v['status'], '?')}"
                        f" {v['check']} ({v['severity']}): {v['status']}"
                        + (f" — {v['detail']}" if v["detail"] else ""))
                if payload.get("awaiting"):
                    lines.append("  awaiting: "
                                 + ", ".join(payload["awaiting"]))
                if payload.get("gaps"):
                    for name, gap in payload["gaps"].items():
                        lines.append(f"  gap {name}: "
                                     + json.dumps(gap, sort_keys=True))
                if payload.get("min_period") is not None:
                    lines.append(
                        "  min_period: "
                        f"{payload['min_period']:.4g}")
                m = payload.get("metrics") or {}
                if m:
                    key = ["clock_period", "setup_wns", "hold_wns",
                           "drc_count", "cell_area", "power_total",
                           "lec_result"]
                    shown = {k: m[k] for k in key if k in m}
                    lines.append("  metrics: "
                                 + json.dumps(shown, sort_keys=True))
                if payload.get("edges"):
                    for row in payload["edges"].get("rows", []):
                        mark = " ↓" if row["worse"] else ""
                        lines.append(
                            f"  {row['dimension']:<24} "
                            f"{row['parent']:>10.4g} → "
                            f"{row['node']:>10.4g} "
                            f"(Δ{row['delta']:+.4g}){mark}")
                if payload.get("cost"):
                    c = payload["cost"]
                    lines.append(
                        f"  build {c['build_seconds']:.0f}s "
                        f"({c['stages_built']} built, "
                        f"{c['stages_reused']} reused, saved "
                        f"~{c['saved_seconds']:.0f}s)"
                        f" · accept {c['accept_seconds']:.0f}s")
                if payload.get("job"):
                    lines.append(f"  job: {json.dumps(payload['job'])}")
                out = "\n".join(lines)
            click.echo(out)
            if not wait or st not in (State.PENDING.value,
                                      State.VERIFYING.value):
                break
            time.sleep(interval)
    finally:
        project.close()


# ---------------------------------------------------------------- log

@cli.command(name="log")
@_project_opt
@click.option("-n", "limit", default=50, show_default=True)
@click.pass_context
def log_cmd(ctx, limit, project):
    """git log --graph --oneline over recorded nodes, state-annotated."""
    project = _project(ctx)
    try:
        r = _git(project.paths["remote"], "log", "--graph", "--oneline",
                 "--glob=refs/guardci/nodes/*", "--glob=refs/heads/*",
                 f"-{limit}", check=False)
        baseline_roots = {b["root_sha"] for b in project.store.baselines()
                          if b["root_sha"]}
        lines = []
        for line in r.stdout.splitlines():
            sha = None
            for tok in line.replace("|", " ").replace("\\", " ") \
                    .replace("/", " ").split():
                if len(tok) >= 7 and all(c in "0123456789abcdef"
                                         for c in tok):
                    node = project.store.get_node(tok)
                    if node:
                        sha = node
                        break
            sym = " "
            extra = ""
            if sha is not None:
                if sha.sha in baseline_roots:
                    sym = "B"
                else:
                    from ..core.scheduler import node_coverage
                    sym = SYMBOLS.get(public_state(sha.state), "?")
                    if sha.state in (State.VERIFIED, State.WARNED) and \
                            node_coverage(project, sha.sha) == "partial":
                        sym += "°"
                m = sha.metrics or {}
                if m.get("clock_period") is not None or \
                        m.get("cell_area") is not None:
                    extra = (f"  period={m.get('clock_period')}"
                             f" wns={m.get('setup_wns')}"
                             f" area={m.get('cell_area')}")
            lines.append(f"{sym} {line}{extra}")
        click.echo("\n".join(lines))
    finally:
        project.close()


# ---------------------------------------------------------------- request-full

@cli.command(name="request-full")
@_project_opt
@click.argument("sha")
@click.pass_context
def request_full(ctx, sha, project):
    """Ask for the full (heavy) check set on a node."""
    project = _project(ctx)
    try:
        node = project.store.get_node(sha)
        if node is None:
            raise click.UsageError(f"unknown node {sha}")
        project.store.set_requested_full(node.sha, True)
        if node.sha not in project.store.queued():
            if node.state != State.PENDING:
                project.store.set_state(node.sha, State.PENDING)
                git_refs.set_state_ref(project.paths["remote"], node.sha,
                                       "pending")
            project.store.enqueue(node.sha)
        click.echo(f"{node.sha[:12]}: requested full verification")
    finally:
        project.close()


# ---------------------------------------------------------------- deliver

@cli.command()
@_project_opt
@click.option("--no-rebuild", is_flag=True,
              help="print the candidate without deciding "
              "reproducibility (§12.5)")
@click.option("--out", "out_dir", default=None,
              type=click.Path(file_okay=False),
              help="write the delivery bundle (rebuilt layout, "
              "verdict.json, MANIFEST.json) under DIR")
@click.pass_context
def deliver(ctx, project, no_rebuild, out_dir):
    """Print the deliverable node (newest accepted at the last stage)."""
    if no_rebuild and out_dir:
        raise click.UsageError("--out cannot be combined with --no-rebuild")
    project = _project(ctx)
    try:
        from ..core.scheduler import node_coverage
        nodes = [n for n in project.store.all_nodes()
                 if n.state in (State.VERIFIED, State.WARNED)]
        if not nodes:
            if out_dir:
                raise click.ClickException("no accepted nodes to export")
            click.echo("no accepted nodes")
            return

        # §12.5 candidates: signoff-reliable nodes, newest first
        full = [n for n in nodes
                if node_coverage(project, n.sha) == "full"]
        ordered = [n for n in reversed(full)
                   if n.state == State.VERIFIED] + \
                  [n for n in reversed(full)
                   if n.state == State.WARNED]
        artifact_src = ((project.contract.verification or {})
                        .get("source") == "artifact")
        best = None
        repro = None
        if (artifact_src or out_dir) and not no_rebuild:
            from ..core.deliver import deliverable
            best, repro = deliverable(project, ordered,
                                      keep_workdir=bool(out_dir))
        else:
            best = ordered[0] if ordered else None
        if best is None:
            if out_dir:
                raise click.ClickException("no signoff-accepted node to export")
            partial = [n for n in nodes
                       if node_coverage(project, n.sha) == "partial"
                       and n.state == State.VERIFIED]
            if partial:
                click.echo(
                    "no signoff-accepted node; newest accepted "
                    f"(stage coverage): {partial[-1].sha} — run "
                    f"`guardci request-full {partial[-1].sha}`")
            else:
                click.echo("no accepted node")
            return
        from ..core.status import public_status, verdict_text
        pub = public_status(project, best.sha) or {}
        click.echo(f"deliverable: {best.sha}  ({verdict_text(pub)})")
        if artifact_src and repro is not None:
            click.echo(f"  reproducible_exact: {repro.get('exact')}")
        elif artifact_src and no_rebuild:
            click.echo("  reproducible_exact: undecided (--no-rebuild)")
        if out_dir:
            from ..core.deliver import write_bundle
            res = (repro or {}).get("result")
            try:
                manifest = write_bundle(project, best, res, out_dir)
                click.echo(f"  bundle: {out_dir} "
                           f"({len(manifest['files'])} files)")
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
            finally:
                res_wd = getattr(res, "workdir", None)
                if res_wd:
                    shutil.rmtree(res_wd, ignore_errors=True)
        if best.metrics:
            m = {k: v for k, v in best.metrics.items()
                 if not k.startswith("_") and k != "stages"}
            click.echo("  metrics: "
                       + json.dumps(m, sort_keys=True, default=str))
        for v in project.store.verdicts(best.sha):
            click.echo(f"  {v.status:>11}  {v.clause.name}"
                       + (f" — {v.detail}" if v.detail else ""))
    finally:
        project.close()


# ---------------------------------------------------------------- checkpoint

@cli.command()
@_project_opt
@click.argument("sha")
@click.pass_context
def checkpoint(ctx, sha, project):
    """Print the captured checkpoint for a node (§12.2)."""
    project = _project(ctx)
    try:
        cp = (Path(project.paths["artifacts"]) / sha
              / "checkpoint" / "checkpoint.json")
        if not cp.exists():
            click.echo(f"no checkpoint captured for {sha}")
            return
        data = json.loads(cp.read_text())
        click.echo(json.dumps(data, indent=1, sort_keys=True))
    finally:
        project.close()


# ---------------------------------------------------------------- rejudge

@cli.command()
@_project_opt
@click.option("--contract", "contract_ref", default=None,
              help="replace the project's contract copy first")
@click.pass_context
def rejudge(ctx, contract_ref, project):
    """Re-evaluate stored metrics under the (new) contract (§11.7)."""
    project = _project(ctx)
    try:
        if contract_ref:
            src = cl._resolve(contract_ref, Path(project.root))
            dst = Path(project.root) / project.cfg["contract"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            project._contract = None
        counts = core_rejudge(project)
        click.echo(json.dumps(counts, indent=1, sort_keys=True))
    finally:
        project.close()


# ---------------------------------------------------------------- bisect

@cli.command()
@_project_opt
@click.argument("sha")
@click.option("--clause", "clause", default=None)
@click.pass_context
def bisect(ctx, sha, clause, project):
    """Start a bisection by hand (§11.6, mode `superseded: skip`)."""
    from ..core import scheduler as sched_mod
    project = _project(ctx)
    try:
        node = project.store.get_node(sha)
        if node is None:
            raise click.UsageError(f"unknown node {sha}")
        clause = clause or sched_mod._bisect_signature(
            project, sha, node.attempt)
        if clause is None:
            click.echo("no bisectable failure on this node")
            return
        payload = sched_mod.bisect_step(project, node.sha, clause)
        if payload is None:
            click.echo("verification.superseded != skip — bisection "
                       "is only active in mode 'skip'")
        elif "first_bad" in payload:
            click.echo(json.dumps(payload, sort_keys=True))
        else:
            click.echo(f"queued midpoint {payload['mid'][:12]} "
                       f"({payload['how']})")
    finally:
        project.close()


# ------------------------------------------------------------- replay

@cli.command()
@click.argument("episode_source_dir",
                type=click.Path(exists=True, file_okay=False))
@click.option("--out", "out_dir", required=True,
              type=click.Path(file_okay=False))
@click.option("--episode-id", default=None)
@click.option("--platform", default=None)
@click.option("--design", default=None)
@click.option("--meta", "meta_path", default=None,
              type=click.Path(exists=True))
def replay(episode_source_dir, out_dir, episode_id, platform, design,
           meta_path):
    """Replay a snapshot episode directory through GuardCI."""
    from ..replay.driver import load_meta, replay_episode

    src = Path(episode_source_dir)
    meta = load_meta(src)
    if meta_path:
        meta = load_meta(Path(meta_path)) or meta
    summary = replay_episode(src, Path(out_dir),
                             episode_id=episode_id or src.name,
                             platform=platform, design=design,
                             meta=meta)
    click.echo(json.dumps(summary, indent=1))


if __name__ == "__main__":
    cli()
