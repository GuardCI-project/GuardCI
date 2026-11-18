"""Real Git + continuous worker demo; static checks only, no EDA tools."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="new, non-existing directory")
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    project, work, source = root / "project", root / "work", root / "source"
    (source / "golden").mkdir(parents=True)
    (source / "golden/design.v").write_text("module top; endmodule\n")
    (source / "config.mk").write_text("UTIL = 50\n")
    contract = root / "static.yaml"
    contract.write_text(yaml.safe_dump({
        "name": "static-demo",
        "protected": ["golden/**"],
        "recipe": ["config.mk"],
        "clauses": [{"check": "recipe_scope", "kind": "invariant", "severity": "hard"}],
    }))
    cli = str(Path(sys.executable).parent / "guardci")
    env = dict(os.environ, GIT_AUTHOR_NAME="GuardCI test",
               GIT_AUTHOR_EMAIL="test@localhost", GIT_COMMITTER_NAME="GuardCI test",
               GIT_COMMITTER_EMAIL="test@localhost")
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    report = {"ok": False, "scope": "static-only; not EDA signoff", "steps": []}
    worker = None

    def run(*cmd):
        result = subprocess.run(list(map(str, cmd)), env=env, capture_output=True,
                                text=True, timeout=60, check=False)
        with (root / "commands.log").open("a") as output:
            output.write("$ " + " ".join(map(str, cmd)) + "\n")
            output.write(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"{cmd[0]} failed ({result.returncode}): {result.stderr}")
        return result.stdout

    def git(*cmd):
        return run("git", "-C", work, *cmd)

    def commit(path, content, message):
        (work / path).write_text(content)
        git("add", path)
        git("commit", "-m", message)
        return git("rev-parse", "HEAD").strip()

    def status(sha):
        return json.loads(run(cli, "status", sha, "--json", "--project", project))

    def wait_for(label, sha, verdict):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if worker.poll() is not None:
                raise RuntimeError("Continuous worker exited")
            result = status(sha)
            if result.get("state") not in ("pending", "verifying", "unknown"):
                assert result.get("verdict") == verdict, result
                report["steps"].append({"label": label, **result})
                print(label, sha, verdict, flush=True)
                return
            time.sleep(0.2)
        raise TimeoutError(f"No terminal verdict for {sha}")

    def stop():
        if worker is not None and worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()

    with (root / "worker.log").open("w") as log:
        def start():
            return subprocess.Popen(
                [cli, "serve", "--project", str(project), "--interval", "0.1"],
                env=env, stdout=log, stderr=log)

        try:
            run(cli, "init", project, "--contract", contract, "--flow", "null", "--from", source)
            run("git", "clone", project / "remote.git", work)
            worker = start()
            good = commit("config.mk", "UTIL = 55\n", "legal recipe")
            git("push", "origin", "HEAD:main")
            wait_for("legal_recipe", good, "accepted")
            bad = commit("golden/design.v", "module top; wire changed; endmodule\n", "protected edit")
            git("push", "origin", "HEAD:main")
            wait_for("protected_change", bad, "violation")
            child = commit("config.mk", "UTIL = 60\n", "invalid descendant")
            git("push", "origin", "HEAD:main")
            wait_for("invalid_descendant", child, "void")
            git("checkout", "-b", "recovery", good)
            recovered = commit("config.mk", "UTIL = 52\n", "clean recovery")
            git("push", "origin", "HEAD:recovery")
            wait_for("clean_branch", recovered, "accepted")
            stop()
            queued = commit("config.mk", "UTIL = 51\n", "queued during worker stop")
            git("push", "origin", "HEAD:recovery")
            assert status(queued)["state"] == "pending"
            worker = start()
            wait_for("restart_queue", queued, "accepted")
            report["ok"] = True
        finally:
            stop()
            (root / "report.json").write_text(json.dumps(report, indent=2))
    print("PASS: real Git pushes, continuous verification, invalid ancestry, recovery and restart")


if __name__ == "__main__":
    main()
