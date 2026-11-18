"""PHB replay loader + driver tests."""

import json
import subprocess
from pathlib import Path

from guardci.replay import phb
from guardci.replay.driver import replay_episode

FIXTURE = Path(__file__).parent / "fixtures" / "orfs-mini"


def _entry(path, cls="text-stored", size=10, mtime=1,
           stored_in=None, type_="file"):
    e = {"path": path, "type": type_, "class": cls,
         "size": size, "mtime": mtime}
    if stored_in is not None:
        e["stored_in"] = stored_in
    return e


# ------------------------------------------------------------ translation

def test_translate_path_constraint_to_impl():
    assert phb.translate_path(
        "designs/nangate45/gcd/constraint.sdc") == \
        "designs/nangate45/gcd/impl.sdc"
    assert phb.translate_path(
        "designs/sky130hd/ibex/constraint.sdc") == \
        "designs/sky130hd/ibex/impl.sdc"


def test_translate_path_other_unchanged():
    for p in ("designs/nangate45/gcd/config.mk",
              "designs/src/gcd_rtl/gcd.v",
              "platforms/nangate45/config.mk",
              "scripts/synth.tcl", "Makefile",
              "designs/nangate45/gcd/impl.sdc"):
        assert phb.translate_path(p) == p


def test_translate_content_sdc_file_line():
    before = ("export DESIGN_NAME = gcd\n"
              "export SDC_FILE = $(DESIGN_HOME)/$(PLATFORM)/"
              "$(DESIGN_NICKNAME)/constraint.sdc\n"
              "export CORE_UTILIZATION = 55\n")
    after = phb.translate_content(
        "designs/nangate45/gcd/config.mk", before)
    assert "constraint.sdc" not in after
    assert "$(DESIGN_NICKNAME)/impl.sdc" in after
    # non-config files and non-SDC_FILE lines untouched
    assert phb.translate_content("scripts/x.tcl", before) == before
    kept = phb.translate_content(
        "designs/nangate45/gcd/config.mk",
        "FOO = constraint.sdc\nOTHER = x\n")
    assert "FOO = constraint.sdc" in kept


# ------------------------------------------------------------ state diff

def _manifest(seq, entries):
    return {"seq": seq, "event": {"label": "x"}, "entries": entries}


def test_compute_delta(tmp_path):
    texts = tmp_path / "texts"
    (texts / "0001" / "." / "flow").mkdir(parents=True)
    (texts / "0001" / "." / "flow" / "a.v").write_text("aaa")
    (texts / "0002" / "." / "flow").mkdir(parents=True)
    (texts / "0002" / "." / "flow" / "b.v").write_text("bbb")
    (texts / "0002" / "." / "flow" / "a.v").write_text("AAA")

    m1 = _manifest(1, [
        _entry("./flow/a.v", size=3, mtime=1),
        _entry("./flow/bin.gds", cls="binary-metadata-only",
               size=99, mtime=1),
        _entry("./flow/results/r.txt"),   # ignored dir
        _entry("./TASK.md"),              # outside flow
        _entry("./flow/dir", type_="dir"),
    ])
    d1 = phb.compute_delta(m1, {}, texts)
    assert set(d1["writes"]) == {"a.v", "bin.gds"}
    assert d1["writes"]["a.v"] == "aaa"
    assert d1["writes"]["bin.gds"].startswith("GUARDCI-REPLAY")
    assert d1["uncaptured"] == ["bin.gds"]
    e1 = phb.flow_files(m1)

    m2 = _manifest(2, [
        _entry("./flow/a.v", size=3, mtime=2, stored_in=2),
        _entry("./flow/b.v", size=3, mtime=2, stored_in=2),
        _entry("./flow/bin.gds", cls="binary-metadata-only",
               size=99, mtime=1),          # unchanged → no uncaptured
    ])
    d2 = phb.compute_delta(m2, e1, texts)
    assert d2["uncaptured"] == []
    assert sorted(d2["writes"]) == ["a.v", "b.v"]   # added + modified
    assert d2["writes"]["a.v"] == "AAA"
    assert d2["deletes"] == []        # unchanged uncaptured stays

    m3 = _manifest(3, [_entry("./flow/bin.gds",
                              cls="binary-metadata-only",
                              size=100, mtime=3)])
    e2 = phb.flow_files(m2)
    d3 = phb.compute_delta(m3, e2, texts)
    assert d3["uncaptured"] == ["bin.gds"]
    assert sorted(d3["deletes"]) == ["a.v", "b.v"]
    assert d3["writes"]["bin.gds"].startswith("GUARDCI-REPLAY")


# ------------------------------------------------------------ driver e2e

def _mk_snapshot(root: Path, seq: int, name: str,
                 files: dict, t: float, elapsed: float):
    """Create snapshots/NNNN-<name>/ with manifest + text.tar.zst."""
    d = root / "snapshots" / f"{seq:04d}-{name}"
    d.mkdir(parents=True)
    stage = d / "_stage"
    for rel, content in files.items():
        f = stage / "flow" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    subprocess.run(
        f"tar -cf - -C {stage} . | zstd -q -o {d/'text.tar.zst'}",
        shell=True, check=True)
    entries = [{
        "path": f"./flow/{rel}", "type": "file",
        "class": "text-stored", "size": len(content),
        "mtime": t, "stored_in": seq,
    } for rel in files]
    manifest = {"seq": seq, "capture_started": t,
                "event": {"label": name, "elapsed_seconds": elapsed},
                "entries": entries}
    (d / "manifest.json").write_text(json.dumps(manifest))
    import shutil
    shutil.rmtree(stage)


def test_replay_driver_mini(tmp_path):
    src = tmp_path / "src"
    fixture_files = {
        str(p.relative_to(FIXTURE)): p.read_text()
        for p in sorted(FIXTURE.rglob("*")) if p.is_file()
    }
    _mk_snapshot(src, 1, "initial", fixture_files, 1000, 0)

    # snap2: recipe change (CORE_UTILIZATION → floorplan tag)
    f2 = dict(fixture_files)
    cfg = "designs/nangate45/gcd/config.mk"
    f2[cfg] = f2[cfg].replace("CORE_UTILIZATION = 55",
                              "CORE_UTILIZATION = 60")
    _mk_snapshot(src, 2, "tool-000001", f2, 2000, 60)

    # snap3: protected write → recipe_scope hard fail
    f3 = dict(f2)
    f3["designs/src/gcd_rtl/gcd.v"] = "module hacked; endmodule\n"
    _mk_snapshot(src, 3, "tool-000002", f3, 3000, 120)

    out = tmp_path / "ep"
    summary = replay_episode(src, out, episode_id="mini",
                             platform="nangate45", design="gcd",
                             meta={"platform": "nangate45",
                                   "design": "gcd",
                                   "wall_seconds": 120})
    assert summary["n_snapshots"] == 3
    assert summary["n_commits"] == 2
    fail = summary["first_hard_static_fail"]
    assert fail is not None and fail["clause"] == "recipe_scope"
    assert fail["snapshot"] == "0003"
    nodes = [json.loads(l)
             for l in (out / "nodes.jsonl").read_text().splitlines()]
    assert len(nodes) == 2
    n2, n3 = nodes
    assert "floorplan" in n2["tags"]
    assert n3["state"] == "rejected"
    assert any(v["clause"] == "recipe_scope" and v["status"] == "fail"
               for v in n3["verdicts"])


# ------------------------------------------------------------ policies

def _seed_all(seed_dir: Path, workdir: Path):
    import shutil
    shutil.copytree(seed_dir, workdir, dirs_exist_ok=True)


def test_replay_two_policies(tmp_path):
    import test_pipeline as tp  # registers fake flow/executor/backends
    import yaml as _yaml

    tp.FakeFlow.reset()
    src = tmp_path / "src"
    files = {str(p.relative_to(FIXTURE)): p.read_text()
             for p in sorted(FIXTURE.rglob("*")) if p.is_file()}
    files["metrics.json"] = tp.metrics_json("baseline")
    _mk_snapshot(src, 1, "initial", files, 1000, 0)
    f2 = dict(files)
    f2["notes.txt"] = "touched\n"
    _mk_snapshot(src, 2, "tool-000001", f2, 2000, 60)
    # heavy- + synthesis-tagged change: routed already routes to every
    # measurement-bearing clause, so the full-policy upgrade decides
    # entirely from the memo (job=None at attempt 2)
    f3 = dict(f2)
    f3["heavy_x.txt"] = "x\n"
    f3["notes.txt"] = "touched2\n"
    f3["metrics.json"] = tp.metrics_json("n2")
    _mk_snapshot(src, 3, "tool-000002", f3, 3000, 120)

    contract = tmp_path / "fake.yaml"
    contract.write_text(_yaml.safe_dump(tp.CONTRACT))
    out = tmp_path / "ep"
    kwargs = {
        "episode_id": "pol", "platform": "nangate45", "design": "gcd",
        "meta": {"platform": "nangate45", "design": "gcd"},
        "contract": str(contract), "flow": "fake", "seed_fn": _seed_all,
        "backends": {"signoff": "fsign", "drc": "fdrc", "lec": "flec"},
        "executor": {"type": "fake", "image": "img-1", "slots": 2},
        "cache": str(tmp_path / "cache"),
        "policies": ["routed", "full"]}

    summary = replay_episode(src, out, **kwargs)
    assert summary["policies"]["routed"]["done"]
    assert summary["policies"]["full"]["done"]
    assert (out / "nodes.routed.jsonl").exists()
    assert (out / "nodes.full.jsonl").exists()
    assert summary["baseline"]["sha"]
    assert summary["baseline"]["stage_keys"]

    routed = [json.loads(l) for l in
              (out / "nodes.routed.jsonl").read_text().splitlines()]
    full = [json.loads(l) for l in
            (out / "nodes.full.jsonl").read_text().splitlines()]
    assert len(routed) == len(full) == 2
    r, f = routed[0], full[0]
    # routed: only fake_light ran on the synthesis tag → partial
    assert r["coverage"] == "partial"
    # full upgraded the node to the complete clause set
    assert f["coverage"] == "full" and f["attempt"] == r["attempt"] + 1
    for n in (r, f):
        assert set(n["stage_keys"]) == {"a", "b", "c"}
        job = n["job"]
        assert job is not None
        assert [s["stage"] for s in job["stages"]]
        assert all("seconds" in s for s in job["stages"])
        assert job["build_seconds"] is not None
        assert n["decided_by"] == "job"

    # snap 3 routed to every clause at attempt 1 (job decided it);
    # its full upgrade is decided inline by memo → job must be the
    # LATEST-attempt job (null), not the attempt-1 job
    r3, f3 = routed[1], full[1]
    assert r3["job"] is not None and r3["decided_by"] == "job"
    assert f3["attempt"] == r3["attempt"] + 1
    assert f3["job"] is None
    assert f3["decided_by"] == "memo"
    assert set(f3["memo"]) == {"signoff", "lec"}
    for v in f3["memo"].values():
        assert set(v) == {"seconds", "key"} and v["key"]
    assert (out / "nodes.jsonl").read_text() == \
        (out / "nodes.full.jsonl").read_text()

    # resume: finished policies are skipped, records untouched
    before = (out / "nodes.full.jsonl").stat().st_mtime_ns
    summary2 = replay_episode(src, out, **kwargs)
    assert (out / "nodes.full.jsonl").stat().st_mtime_ns == before
    assert summary2["policies"]["full"]["done"]


# ------------------------------------------------------------ recovery

def _add_metadata_only(src: Path, seq: int, rel: str,
                       size: int, mtime: int):
    d = next((src / "snapshots").glob(f"{seq:04d}-*"))
    m = json.loads((d / "manifest.json").read_text())
    m["entries"].append({
        "path": f"./flow/{rel}", "type": "file",
        "class": "metadata-only", "size": size, "mtime": mtime})
    (d / "manifest.json").write_text(json.dumps(m))


def test_recovery_template_and_tar(tmp_path):
    """Metadata-only entries recover from (a) the task template
    workspace (size match) and (b) final-workspace.tar.zst (size +
    mtime match); whatever stays unrecovered marks the baseline
    unreplayable — no job is queued and nodes stay verifying."""
    import io
    import tarfile as T

    import test_pipeline as tp  # registers fake flow/executor/backends
    import yaml as _yaml

    tp.FakeFlow.reset()
    src = tmp_path / "src"
    files = {str(p.relative_to(FIXTURE)): p.read_text()
             for p in sorted(FIXTURE.rglob("*")) if p.is_file()}
    files["metrics.json"] = tp.metrics_json("baseline")
    _mk_snapshot(src, 1, "initial", files, 1000, 0)
    f2 = dict(files)
    f2["notes.txt"] = "touched\n"
    _mk_snapshot(src, 2, "tool-000001", f2, 2000, 60)
    _add_metadata_only(src, 1, "platforms/nangate45/lib/x.lib",
                       100, 777)
    _add_metadata_only(src, 1, "platforms/nangate45/gds/y.gds",
                       50, 888)
    _add_metadata_only(src, 1, "platforms/nangate45/z.bin", 30, 999)

    templates = tmp_path / "templates"
    tf = (templates / "nangate45-gcd" / "workspace" / "flow"
          / "platforms" / "nangate45" / "lib" / "x.lib")
    tf.parent.mkdir(parents=True)
    tf.write_bytes(b"L" * 100)

    buf = io.BytesIO()
    with T.open(fileobj=buf, mode="w") as t:
        info = T.TarInfo("./flow/platforms/nangate45/gds/y.gds")
        info.size, info.mtime = 50, 888
        t.addfile(info, io.BytesIO(b"G" * 50))
        info = T.TarInfo("./flow/platforms/nangate45/wrong.gds")
        info.size, info.mtime = 40, 888   # size mismatch — not used
        t.addfile(info, io.BytesIO(b"W" * 40))
    with open(src / "final-workspace.tar.zst", "wb") as fh:
        subprocess.run(["zstd", "-q"], input=buf.getvalue(),
                       stdout=fh, check=True)

    contract = tmp_path / "fake.yaml"
    contract.write_text(_yaml.safe_dump(tp.CONTRACT))
    out = tmp_path / "ep"
    summary = replay_episode(
        src, out, episode_id="rec", platform="nangate45",
        design="gcd",
        meta={"platform": "nangate45", "design": "gcd"},
        contract=str(contract), flow="fake", seed_fn=_seed_all,
        backends={"signoff": "fsign", "drc": "fdrc", "lec": "flec"},
        executor={"type": "fake", "image": "img-1", "slots": 2},
        cache=str(tmp_path / "cache"), policies=["routed"],
        templates=templates)

    assert summary["recovered"] == {"template": 1,
                                    "final_workspace": 1}
    assert summary["unreplayable_baseline"] is True
    assert "platforms/nangate45/z.bin" in summary["unrecovered"]
    # recovered bytes actually reached the baseline tree
    from guardci.core.project import Project
    project = Project(out / "project")
    try:
        base = project.store.baseline_root()
        blob = subprocess.run(
            ["git", "-C", str(project.paths["remote"]), "show",
             f"{base}:platforms/nangate45/lib/x.lib"],
            capture_output=True, check=True)
        assert blob.stdout == b"L" * 100
    finally:
        project.close()
    # the unrecovered baseline file makes every commit unreplayable:
    # no job queued, node stays verifying
    nodes = [json.loads(l) for l in
             (out / "nodes.routed.jsonl").read_text().splitlines()]
    assert nodes and all(n["state"] == "verifying" for n in nodes)
    assert all(n["job"] is None and n["unreplayable"] for n in nodes)


def test_measurement_record_shape(tmp_path):
    """§10.3: job.measurement_results / memo{} contain exactly the
    measurements ({seconds, memo_hit, key}), keyed by bare name — never
    a same-named checker row."""
    import test_pipeline as tp
    import yaml as _yaml

    tp.FakeFlow.reset()
    src = tmp_path / "src"
    files = {str(p.relative_to(FIXTURE)): p.read_text()
             for p in sorted(FIXTURE.rglob("*")) if p.is_file()}
    files["metrics.json"] = tp.metrics_json("baseline")
    _mk_snapshot(src, 1, "initial", files, 1000, 0)
    f2 = dict(files)
    f2["notes.txt"] = "touched\n"
    f2["metrics.json"] = tp.metrics_json("n1")
    _mk_snapshot(src, 2, "tool-000001", f2, 2000, 60)

    contract = tmp_path / "fake.yaml"
    contract.write_text(_yaml.safe_dump(tp.CONTRACT))
    out = tmp_path / "ep"
    replay_episode(src, out, episode_id="shp",
                   platform="nangate45", design="gcd",
                   meta={"platform": "nangate45", "design": "gcd"},
                   contract=str(contract), flow="fake",
                   seed_fn=_seed_all,
                   backends={"signoff": "fsign", "drc": "fdrc",
                             "lec": "flec"},
                   executor={"type": "fake", "image": "img-1",
                             "slots": 2},
                   cache=str(tmp_path / "cache"),
                   policies=["routed"])

    nodes = [json.loads(l) for l in
             (out / "nodes.routed.jsonl").read_text().splitlines()]
    for n in nodes:
        mr = ((n.get("job") or {}).get("measurement_results") or {})
        assert set(mr) <= {"signoff", "drc", "lec"}
        for name, v in mr.items():
            assert set(v) == {"seconds", "memo_hit", "key"}, (name, v)
            assert v["key"], name
            assert v["seconds"] is not None
        memo = n.get("memo") or {}
        assert set(memo) <= {"signoff", "drc", "lec"}
        for name, v in memo.items():
            assert set(v) == {"seconds", "key"}, (name, v)
            assert v["key"], name
        if n.get("job") is None and n["state"] in (
                "verified", "warned"):
            assert memo, n["snapshot"]
