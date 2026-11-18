"""End-to-end: real git pushes fire the post-receive hook."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

BIN = Path(sys.executable).parent
GUARDCI = BIN / "guardci"

TEST_CONTRACT = {
    "name": "test",
    "protected": ["designs/src/**", "constraints/signoff.sdc"],
    "recipe": ["config.mk", "constraints/impl.sdc", "scripts/**"],
    "clauses": [{"check": "recipe_scope", "severity": "hard"}],
    "routing": {},
}


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


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "src"
    (src / "designs" / "src").mkdir(parents=True)
    (src / "constraints").mkdir(parents=True)
    (src / "config.mk").write_text("DESIGN = top\n")
    (src / "designs" / "src" / "top.v").write_text("module top; endmodule\n")
    (src / "constraints" / "signoff.sdc").write_text("create_clock -period 1\n")

    contract = tmp_path / "contract.yaml"
    contract.write_text(yaml.safe_dump(TEST_CONTRACT))

    proj = tmp_path / "proj"
    r = guardci("init", str(proj), "--contract", str(contract),
                "--from", str(src))
    assert r.returncode == 0, r.stderr
    return proj


@pytest.fixture
def clone(project, tmp_path):
    work = tmp_path / "work"
    git(tmp_path, "clone", str(project / "remote.git"), str(work))
    return work


def serve_once(project):
    guardci("serve", "--once", "--project", str(project))


def state_of(project, sha):
    r = guardci("status", sha, "--project", str(project), "--json")
    return json.loads(r.stdout)


def push(clone_):
    return git(clone_, "push", "origin", "HEAD:main")


def test_e2e(project, clone, tmp_path):
    remote = str(project / "remote.git")

    # commit 1: recipe change -> verified
    push(clone)  # seed already pushed by init; this pushes nothing new
    sha1 = commit(clone, "config.mk", "DESIGN = top\nUTIL = 55\n", "c1")
    r = push(clone)
    assert "guardci: queued" in r.stderr
    serve_once(project)
    assert state_of(project, sha1)["state"] == "verified"

    # commit 2: protected change -> rejected
    sha2 = commit(clone, "designs/src/top.v", "module top; // hacked\n",
                  "c2")
    push(clone)
    serve_once(project)
    assert state_of(project, sha2)["state"] == "rejected"

    # commit 3 on top of 2: recipe change -> tainted
    sha3 = commit(clone, "config.mk", "DESIGN = top\nUTIL = 60\n", "c3")
    push(clone)
    serve_once(project)
    assert state_of(project, sha3)["state"] == "tainted"

    # commit 4 branched from 1: recipe change -> verified
    git(clone, "checkout", "-q", "-b", "fix", sha1)
    sha4 = commit(clone, "config.mk", "DESIGN = top\nUTIL = 50\n", "c4")
    git(clone, "push", "origin", "fix:refs/heads/fix")
    serve_once(project)
    assert state_of(project, sha4)["state"] == "verified"

    # commit 5 on branch: proposals/ is an ordinary path now — a new
    # file there is just scratch content, judged like any other
    sha5 = commit(clone, "proposals/signoff.sdc", "create_clock -p 0.9\n",
                  "c5")
    git(clone, "push", "origin", "HEAD:refs/heads/prop")
    serve_once(project)
    assert state_of(project, sha5)["state"] == "verified"

    # refs agree with the store — public state names (§4 vocabulary)
    refs = git(remote, "for-each-ref", "--format=%(refname)",
               "refs/guardci/").stdout
    for sha, st in ((sha1, "accepted"), (sha2, "violation"),
                    (sha3, "void"), (sha4, "accepted"),
                    (sha5, "accepted")):
        assert f"refs/guardci/{st}/{sha}" in refs

    # `guardci log` shows the public state symbols
    r = guardci("log", "--project", str(project))
    out = r.stdout
    for sym in ("✓", "✗", "⊘"):
        assert sym in out

    # status --json structure
    payload = state_of(project, sha2)
    assert payload["sha"] == sha2
    assert payload["state"] == "rejected"
    assert payload["tags"] == ["protected", "unknown"] or \
        "protected" in payload["tags"]
    assert any(v["check"] == "recipe_scope" and v["status"] == "fail"
               for v in payload["verdicts"])


def test_orphan_root_rejected(project, clone, tmp_path):
    # orphan history containing modified protected content must not be
    # accepted as a new root
    git(clone, "checkout", "-q", "--orphan", "evil")
    git(clone, "rm", "-rfq", ".")
    (Path(clone) / "designs" / "src").mkdir(parents=True)
    (Path(clone) / "designs" / "src" / "top.v").write_text(
        "module top; /* evil */ endmodule\n")
    sha_evil = commit(clone, "config.mk", "DESIGN = top\n", "evil root")
    git(clone, "push", "origin", "HEAD:refs/heads/evil")
    serve_once(project)
    payload = state_of(project, sha_evil)
    assert payload["state"] == "rejected"

    sha_child = commit(clone, "config.mk", "DESIGN = top\nUTIL=1\n",
                       "evil child")
    git(clone, "push", "origin", "HEAD:refs/heads/evil")
    serve_once(project)
    assert state_of(project, sha_child)["state"] == "tainted"


def test_info_fail_keeps_verified(project, clone, tmp_path):
    # a failing `info` clause annotates but does not change the verdict;
    # the stub checker is registered in-process, so the node is driven
    # through process_once directly (the push hook itself is real).
    from guardci.checks.base import Checker, CheckResult, register
    from guardci.core.models import Cost
    from guardci.core.project import Project
    from guardci.core.scheduler import process_once

    @register
    class FailInfo(Checker):
        name = "fail_info"
        cost = Cost.STATIC

        def run(self, ctx, params, backend):
            return CheckResult(status="fail", detail="always fails")

    # extend the project's contract with the info clause
    cfile = next((project / "contracts").glob("*.yaml"))
    doc = yaml.safe_load(cfile.read_text())
    doc["clauses"].append({"check": "fail_info", "severity": "info"})
    cfile.write_text(yaml.safe_dump(doc))

    sha = commit(clone, "config.mk", "DESIGN = top\nUTIL = 42\n",
                 "info-fail")
    git(clone, "push", "origin", "HEAD:refs/heads/info")
    proj = Project(project)
    try:
        while process_once(proj):
            pass
    finally:
        proj.close()
    payload = state_of(project, sha)
    assert payload["state"] == "verified"
    assert any(v["check"] == "fail_info" and v["status"] == "fail"
               for v in payload["verdicts"])


def test_continuous_worker_and_restart(project, clone, tmp_path):
    """Pushes are processed without a manual serve --once, including restart."""
    import time

    worker = None
    with (tmp_path / "worker.log").open("w+") as log:
        def start():
            return subprocess.Popen(
                [str(GUARDCI), "serve", "--interval", "0.05",
                 "--project", str(project)], stdout=log, stderr=log)

        def wait_for(sha, expected):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert worker.poll() is None, "CI worker exited"
                payload = state_of(project, sha)
                if payload["state"] not in ("pending", "verifying"):
                    assert payload["state"] == expected, payload
                    return
                time.sleep(0.05)
            log.flush()
            log.seek(0)
            pytest.fail(f"CI did not finish {sha}: {payload}\n{log.read()}")

        def stop():
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=10)

        try:
            worker = start()
            good = commit(clone, "config.mk", "DESIGN = top\nUTIL = 55\n", "good")
            assert "guardci: queued" in push(clone).stderr
            wait_for(good, "verified")
            bad = commit(clone, "designs/src/top.v", "module changed; endmodule\n", "bad")
            push(clone)
            wait_for(bad, "rejected")
            child = commit(clone, "config.mk", "DESIGN = top\nUTIL = 60\n", "child")
            push(clone)
            wait_for(child, "tainted")
            stop()
            git(clone, "checkout", "-b", "clean", good)
            clean = commit(clone, "config.mk", "DESIGN = top\nUTIL = 50\n", "clean")
            git(clone, "push", "origin", "HEAD:refs/heads/clean")
            assert state_of(project, clean)["state"] == "pending"
            worker = start()
            wait_for(clean, "verified")
            assert state_of(project, bad)["state"] == "rejected"
            refs = git(project / "remote.git", "for-each-ref",
                       "--format=%(refname)", "refs/guardci/").stdout
            for sha, state in ((good, "accepted"), (bad, "violation"),
                               (child, "void"), (clean, "accepted")):
                assert f"refs/guardci/{state}/{sha}" in refs
            assert clean in guardci("deliver", "--project", str(project)).stdout
        finally:
            if worker is not None and worker.poll() is None:
                stop()


def test_export_without_build_products_fails(project, clone, tmp_path):
    sha = commit(clone, "config.mk", "DESIGN = top\nUTIL = 55\n", "valid recipe")
    push(clone)
    serve_once(project)
    assert state_of(project, sha)["state"] == "verified"
    out = tmp_path / "delivery"
    result = guardci("deliver", "--out", str(out), "--project", str(project), check=False)
    assert result.returncode != 0
    assert "products" in result.stderr
    assert not out.exists()
