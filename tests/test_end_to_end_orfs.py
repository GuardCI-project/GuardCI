"""End-to-end M1: ORFS seeding, static checks, taint-clearing.

Real `git push` fires the post-receive hook; the scheduler is driven by
`guardci serve --once`.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(sys.executable).parent
GUARDCI = BIN / "guardci"
FIXTURE = Path(__file__).parent / "fixtures" / "orfs-mini"
STATIC_ONLY = Path(__file__).parent / "fixtures" / "orfs-static-only.yaml"
LEGACY_REBUILD = Path(__file__).parent / "fixtures" / "orfs-rebuild-legacy.yaml"
CONFIG = "designs/nangate45/gcd/config.mk"
GOLDEN_SDC = "designs/nangate45/gcd/constraint.sdc"
IMPL_SDC = "designs/nangate45/gcd/impl.sdc"
RTL = "designs/src/gcd_rtl/gcd.v"


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check)


def guardci(*args, cwd=None, check=True):
    return subprocess.run([str(GUARDCI), *args], capture_output=True,
                          text=True, check=check, cwd=cwd)


def commit(repo, path, content, msg):
    p = Path(repo) / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit",
        "-qm", msg)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def serve_once(project):
    guardci("serve", "--once", "--project", str(project))


def state_of(project, sha):
    r = guardci("status", sha, "--project", str(project), "--json")
    return json.loads(r.stdout)


def push(clone_, refspec="HEAD:main"):
    return git(clone_, "push", "origin", refspec)


def _init(tmp_path, contract):
    proj = tmp_path / "proj"
    r = guardci("init", str(proj), "--flow", "orfs",
                "--platform", "nangate45", "--design", "gcd",
                "--contract", str(contract), "--from", str(FIXTURE))
    assert r.returncode == 0, r.stderr
    work = tmp_path / "work"
    git(tmp_path, "clone", str(proj / "remote.git"), str(work))
    return proj, work


@pytest.fixture
def project_full(tmp_path):
    return _init(tmp_path, LEGACY_REBUILD)


@pytest.fixture
def project_static(tmp_path):
    return _init(tmp_path, STATIC_ONLY)


def test_orfs_seed_layout(tmp_path):
    proj, work = _init(tmp_path, STATIC_ONLY)
    assert (work / IMPL_SDC).exists()
    # RTL dir name differs from the design name on purpose: the seed
    # must copy the whole workspace, not just designs/src/<design>/
    assert (work / RTL).exists()
    assert (work / ".gitignore").exists()
    cfg = (work / CONFIG).read_text()
    assert "$(DESIGN_DIR)/impl.sdc" in cfg
    for d in ("results/", "logs/", "reports/", "objects/"):
        assert d in (work / ".gitignore").read_text()
    gcfg = yaml_safe(proj / "guardci.yaml")
    assert gcfg["flow"] == "orfs"
    assert gcfg["design"] == {"platform": "nangate45", "design": "gcd"}


def yaml_safe(path):
    import yaml
    return yaml.safe_load(Path(path).read_text())


def _verdicts(payload):
    return {v["check"]: v for v in payload["verdicts"]}


def test_full_contract_verifying(project_full):
    """(a) CORE_UTILIZATION with the full contract: statics pass, heavy
    clauses are unavailable → fail-closed verifying."""
    proj, work = project_full
    sha = commit(work, CONFIG,
                 (work / CONFIG).read_text().replace(
                     "CORE_UTILIZATION = 55", "CORE_UTILIZATION = 60"),
                 "util 60")
    r = push(work)
    assert "guardci: queued" in r.stderr
    serve_once(proj)
    p = state_of(proj, sha)
    assert p["state"] == "verifying"
    assert "floorplan" in p["tags"]
    assert "physical_integrity" in p["tags"]
    v = _verdicts(p)
    assert v["recipe_scope"]["status"] == "pass"
    assert v["design_identity"]["status"] == "pass"
    assert v["protected_refs"]["status"] == "pass"
    assert v["sdc_delta"]["status"] == "pass"
    assert p["awaiting"]  # sta_hold/electrical/... unavailable


def test_static_only_verified(project_static):
    """(a) same change under the static-only contract → verified."""
    proj, work = project_static
    sha = commit(work, CONFIG,
                 (work / CONFIG).read_text().replace(
                     "CORE_UTILIZATION = 55", "CORE_UTILIZATION = 60"),
                 "util 60")
    push(work)
    serve_once(proj)
    p = state_of(proj, sha)
    assert p["state"] == "verified"
    assert {"floorplan", "physical_integrity"} <= set(p["tags"])


def test_verilog_files_rejected(project_static):
    """(b) VERILOG_FILES change → design_identity hard fail → rejected."""
    proj, work = project_static
    sha = commit(work, CONFIG,
                 (work / CONFIG).read_text().replace(
                     "/src/gcd_rtl/gcd.v", "/src/gcd_rtl/other.v"),
                 "swap rtl")
    push(work)
    serve_once(proj)
    p = state_of(proj, sha)
    assert p["state"] == "rejected"
    v = _verdicts(p)
    assert v["design_identity"]["status"] == "fail"
    assert "rtl_files" in v["design_identity"]["detail"]


def test_taint_and_restore(project_static):
    """(c) protected RTL change → rejected; child → tainted; grandchild
    restoring the RTL is clean and re-verified."""
    proj, work = project_static
    orig = (work / RTL).read_text()
    sha_bad = commit(work, RTL, orig + "// hacked\n", "hack rtl")
    push(work)
    serve_once(proj)
    assert state_of(proj, sha_bad)["state"] == "rejected"
    v = _verdicts(state_of(proj, sha_bad))
    assert v["recipe_scope"]["status"] == "fail"

    sha_child = commit(work, CONFIG,
                       (work / CONFIG).read_text().replace(
                           "55", "60", 1), "child")
    push(work)
    serve_once(proj)
    assert state_of(proj, sha_child)["state"] == "tainted"

    sha_fix = commit(work, RTL, orig, "restore rtl")
    push(work)
    serve_once(proj)
    p = state_of(proj, sha_fix)
    assert p["state"] == "verified", p


def _check_metrics(proj, sha):
    """check_run metrics via the sqlite store (status --json lacks them)."""
    import sqlite3
    db = sqlite3.connect(proj / "state.db")
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT metrics FROM check_runs WHERE sha=? AND checker=?",
        (sha, "sdc_delta")).fetchall()
    db.close()
    for r in rows:
        yield json.loads(r["metrics"])


def test_impl_sdc_tighter_metrics(project_static):
    """(e) impl.sdc period 1.0 → 0.8 → sdc_delta metrics say tighter."""
    proj, work = project_static
    impl = (work / IMPL_SDC).read_text()
    sha = commit(work, IMPL_SDC,
                 impl.replace("-period $clk_period", "-period 0.8"),
                 "tighten impl")
    push(work)
    serve_once(proj)
    p = state_of(proj, sha)
    assert p["state"] == "verified", p
    metrics = list(_check_metrics(proj, sha))
    assert metrics
    row = metrics[0]["classes"]["clock_period"][0]
    assert row["direction"] == "tighter"
    assert metrics[0]["summary"]["tighter"] >= 1


def test_variant_design_dir_ignored(project_static):
    """(f) designs/nangate45/exp1/config.mk is scratch under §9.3:
    static checkers read only the built config. Its basename collides
    with the Makefile's `config.mk` token, so §9.3 names it
    (conservatively reachable) and the diff tags {unknown} by
    extension — the node is still decided normally."""
    proj, work = project_static
    sha = commit(
        work, "designs/nangate45/exp1/config.mk",
        "export DESIGN_NAME = exp1\n"
        "export VERILOG_FILES = designs/src/exp1/x.v\n"
        "export SDC_FILE = $(DESIGN_DIR)/other.sdc\n",
        "scratch variant config")
    push(work)
    serve_once(proj)
    p = state_of(proj, sha)
    assert p["state"] == "verified", p
    assert set(p["tags"]) == {"unknown"}
    v = _verdicts(p)
    assert v["design_identity"]["status"] == "pass"
    assert v["sdc_delta"]["status"] == "pass"
    assert v["protected_refs"]["status"] == "pass"


def test_deleted_config_rejected(project_static):
    """(g) deleting the built config.mk → design_identity hard fail
    with a clear message → rejected."""
    proj, work = project_static
    (work / CONFIG).unlink()
    git(work, "add", "-A")
    git(work, "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-qm", "delete config")
    sha = git(work, "rev-parse", "HEAD").stdout.strip()
    push(work)
    serve_once(proj)
    p = state_of(proj, sha)
    assert p["state"] == "rejected", p
    v = _verdicts(p)
    assert v["design_identity"]["status"] == "fail"
    assert "missing" in v["design_identity"]["detail"]
