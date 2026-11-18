"""Unit tests for the static checkers against a real git object store."""

import shutil
import subprocess
from pathlib import Path

import pytest

from guardci.checks.design_identity import DesignIdentity
from guardci.checks.protected_refs import ProtectedRefs
from guardci.contract.loader import load
from guardci.core.diff import Diff
from guardci.core.models import Node
from guardci.flows.orfs.adapter import OrfsFlow

FIXTURE = Path(__file__).parent / "fixtures" / "orfs-mini"
CONFIG = "designs/nangate45/gcd/config.mk"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def _commit_all(repo, msg="c"):
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A work repo whose first commit is the seeded ORFS fixture."""
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    git(tmp_path, "init", "-qb", "main")
    baseline = _commit_all(tmp_path, "baseline")
    return tmp_path, baseline


def _ctx(repo, sha, baseline, contract=None):
    return {
        "repo": str(repo),
        "node": Node(sha=sha),
        "baseline_sha": baseline,
        "contract": contract or load("orfs-rebuild"),
        "diff": Diff(base=baseline, head=sha, entries=[], repo=str(repo)),
        "changed_paths": [],
        "flow": OrfsFlow(),
        "design": "gcd",
    }


def _edit(repo, path, mutate):
    p = Path(repo) / path
    p.write_text(mutate(p.read_text()))


# --------------------------------------------------------- design_identity

def test_identity_passes_baseline_identical(repo):
    work, base = repo
    ctx = _ctx(work, base, base)
    r = DesignIdentity().run(ctx, {}, None)
    assert r.status == "pass", r.detail


def test_identity_fails_changed_verilog_files(repo):
    work, base = repo
    _edit(work, CONFIG, lambda t: t.replace(
        "$(DESIGN_HOME)/src/gcd_rtl/gcd.v",
        "designs/src/gcd_rtl/other.v"))
    sha = _commit_all(work)
    r = DesignIdentity().run(_ctx(work, sha, base), {}, None)
    assert r.status == "fail"
    assert "rtl_files" in r.detail


def test_identity_fails_changed_design_name(repo):
    work, base = repo
    _edit(work, CONFIG, lambda t: t.replace(
        "export DESIGN_NAME = gcd", "export DESIGN_NAME = aes"))
    sha = _commit_all(work)
    r = DesignIdentity().run(_ctx(work, sha, base), {}, None)
    assert r.status == "fail"
    assert "top" in r.detail


def test_identity_fails_foreign_sdc(repo):
    work, base = repo
    _edit(work, CONFIG, lambda t: t.replace(
        "$(DESIGN_HOME)/$(PLATFORM)/$(DESIGN_NICKNAME)/constraint.sdc",
        "scripts/foo.sdc"))
    sha = _commit_all(work)
    r = DesignIdentity().run(_ctx(work, sha, base), {}, None)
    assert r.status == "fail"
    assert "SDC_FILE" in r.detail


def test_identity_passes_equivalent_expressions(repo):
    """$(DESIGN_HOME)/$(PLATFORM)/$(DESIGN_NICKNAME)/constraint.sdc and the
    literal resolved path must compare equal."""
    work, base = repo
    _edit(work, CONFIG, lambda t: t.replace(
        "$(DESIGN_HOME)/src/gcd_rtl/gcd.v",
        "designs/src/gcd_rtl/gcd.v"))
    sha = _commit_all(work)
    r = DesignIdentity().run(_ctx(work, sha, base), {}, None)
    assert r.status == "pass", r.detail


# --------------------------------------------------------- protected_refs

def _refs_with(repo, mutate):
    work, base = repo
    _edit(work, CONFIG, mutate)
    sha = _commit_all(work)
    return ProtectedRefs().run(_ctx(work, sha, base), {}, None)


def test_refs_absolute_path_fails(repo):
    r = _refs_with(repo, lambda t: t.replace(
        "$(PLATFORM_DIR)/lib/NangateOpenCellLibrary_typical.lib",
        "/abs/x.lib"))
    assert r.status == "fail"
    assert "absolute" in r.detail


def test_refs_parent_traversal_fails(repo):
    r = _refs_with(repo, lambda t: t.replace(
        "$(PLATFORM_DIR)/lib/NangateOpenCellLibrary_typical.lib",
        "../x.lib"))
    assert r.status == "fail"
    assert "traversal" in r.detail


def test_refs_output_dir_fails(repo):
    r = _refs_with(repo, lambda t: t.replace(
        "$(PLATFORM_DIR)/lib/NangateOpenCellLibrary_typical.lib",
        "results/x.lib"))
    assert r.status == "fail"


def test_refs_missing_file_fails(repo):
    r = _refs_with(repo, lambda t: t.replace(
        "$(PLATFORM_DIR)/lib/NangateOpenCellLibrary_typical.lib",
        "$(PLATFORM_DIR)/lib/nonexistent.lib"))
    assert r.status == "fail"
    assert "not in repo" in r.detail


def test_refs_platform_lib_passes(repo):
    work, base = repo
    ctx = _ctx(work, base, base)
    r = ProtectedRefs().run(ctx, {}, None)
    assert r.status == "pass", r.detail


def test_refs_new_protected_lib_fails(repo):
    """A brand-new file under platforms/ may be committed (recipe_scope
    no longer rejects additions), but a config reference to it is
    foreign — it is not part of the protected baseline."""
    work, base = repo
    (Path(work) / "platforms/nangate45/lib/y_fast.lib").write_text(
        "library stub\n")
    _edit(work, CONFIG, lambda t: t.replace(
        "$(PLATFORM_DIR)/lib/NangateOpenCellLibrary_typical.lib",
        "$(PLATFORM_DIR)/lib/y_fast.lib"))
    sha = _commit_all(work)
    r = ProtectedRefs().run(_ctx(work, sha, base), {}, None)
    assert r.status == "fail"
    assert "not in protected baseline" in r.detail


# --------------------------------------------------------- recipe_scope

def test_scratch_file_not_rejected(repo):
    from guardci.checks.recipe_scope import RecipeScope
    work, base = repo
    (Path(work) / "designs/nangate45/gcd/exp.mk").write_text("X = 1\n")
    sha = _commit_all(work)
    ctx = _ctx(work, sha, base)
    ctx["baseline_paths"] = ["designs/nangate45/gcd/exp.mk"]
    r = RecipeScope().run(ctx, {}, None)
    assert r.status == "pass"
    assert r.metrics["scratch_paths"] == [
        "designs/nangate45/gcd/exp.mk"]


def test_modify_protected_rejected(repo):
    from guardci.checks.recipe_scope import RecipeScope
    work, base = repo
    _edit(work, "platforms/nangate45/lib/"
                "NangateOpenCellLibrary_typical.lib",
          lambda t: t + "# tampered\n")
    sha = _commit_all(work)
    ctx = _ctx(work, sha, base)
    ctx["baseline_paths"] = [
        "platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib"]
    r = RecipeScope().run(ctx, {}, None)
    assert r.status == "fail"
    assert "protected" in r.detail


# --------------------------------------------------------- sdc roles

def test_identity_own_sdc_is_impl(repo):
    """SDC_FILE pointing at an agent-created sdc in the design dir
    (in tree, not protected) classifies as impl and passes."""
    work, base = repo
    (Path(work) / "designs/nangate45/gcd/fast.sdc").write_text(
        (Path(work) / "designs/nangate45/gcd/constraint.sdc")
        .read_text())
    _edit(work, CONFIG, lambda t: t.replace(
        "$(DESIGN_HOME)/$(PLATFORM)/$(DESIGN_NICKNAME)/constraint.sdc",
        "$(DESIGN_DIR)/fast.sdc"))
    sha = _commit_all(work)
    r = DesignIdentity().run(_ctx(work, sha, base), {}, None)
    assert r.status == "pass", r.detail
    assert r.metrics["sdc_role"] == "impl"


def test_identity_protected_sdc_is_other(repo):
    work, base = repo
    _edit(work, CONFIG, lambda t: t.replace(
        "$(DESIGN_HOME)/$(PLATFORM)/$(DESIGN_NICKNAME)/constraint.sdc",
        "$(PLATFORM_DIR)/x.sdc"))
    sha = _commit_all(work)
    r = DesignIdentity().run(_ctx(work, sha, base), {}, None)
    assert r.status == "fail"
    assert r.metrics["sdc_role"] == "other"


# --------------------------------------------------------- config include

def test_identity_include_override_fails(repo):
    """`include exp.mk` in config.mk whose body overrides VERILOG_FILES
    must be visible to design_identity."""
    work, base = repo
    ddir = Path(work) / "designs/nangate45/gcd"
    (ddir / "exp.mk").write_text(
        "export VERILOG_FILES = designs/src/gcd/evil.v\n")
    (ddir / "config.mk").write_text(
        (ddir / "config.mk").read_text() + "\ninclude exp.mk\n")
    sha = _commit_all(work)
    r = DesignIdentity().run(_ctx(work, sha, base), {}, None)
    assert r.status == "fail"
    assert "rtl_files" in r.detail


def test_references_counts_includes(repo):
    work, _ = repo
    ddir = Path(work) / "designs/nangate45/gcd"
    (ddir / "exp.mk").write_text("X = 1\n")
    (ddir / "config.mk").write_text(
        (ddir / "config.mk").read_text() + "\ninclude exp.mk\n")
    sha = _commit_all(work)
    refs = OrfsFlow().references(str(work), sha)
    assert any(r.var == "include"
               and r.path == "designs/nangate45/gcd/exp.mk"
               and r.kind == "other" for r in refs)


def test_identity_dot_slash_prefix_normalized(repo):
    """SDC_FILE resolving to './designs/.../constraint.sdc' must be
    normalized before role/tree checks."""
    work, base = repo
    _edit(work, CONFIG, lambda t: t.replace(
        "$(DESIGN_HOME)/$(PLATFORM)/$(DESIGN_NICKNAME)/constraint.sdc",
        "./$(DESIGN_HOME)/$(PLATFORM)/$(DESIGN_NICKNAME)"
        "/constraint.sdc"))
    sha = _commit_all(work)
    r = DesignIdentity().run(_ctx(work, sha, base), {}, None)
    assert r.status == "pass", r.detail
    assert r.metrics["sdc_role"] == "golden"
