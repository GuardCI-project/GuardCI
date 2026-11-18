"""stage_keys attribution tests (DESIGN.md §9.3) on the orfs-mini repo."""

from __future__ import annotations

from gitrepo import commit_all, edit, head, make_repo

from guardci.flows.orfs.adapter import OrfsFlow

IMAGE = "sha256:test"
PROTECTED = ["designs/src/**", "platforms/**",
             "designs/*/*/constraint.sdc"]
STAGES = ["synth", "floorplan", "place", "cts", "route", "finish"]


def _flow():
    return OrfsFlow(platform="nangate45", design="gcd",
                    protected=PROTECTED)


FIXED_PREV = "0" * 64


def _keys(flow, repo, sha, base, image=IMAGE):
    """§11.4.2 per-stage input key with a fixed predecessor digest —
    key(s) differences therefore show exactly the stages whose
    *inputs* (or header/unattributable set) changed."""
    keyer = flow._keyer(str(repo), sha, base, image, PROTECTED)
    return {stage: keyer.key(i, FIXED_PREV)
            for i, stage in enumerate(STAGES)}


def _base_and_head(tmp_path):
    repo = make_repo(tmp_path)
    base = head(repo)
    return repo, base


def _diff_keys(flow, repo, base):
    """stage name -> (base_key, head_key) after a commit."""
    k0 = _keys(flow, repo, base, base)
    sha = commit_all(repo)
    k1 = _keys(flow, repo, sha, base)
    return {s: (k0[s], k1[s]) for s in STAGES}


def _equal_from(diff, stage):
    i = STAGES.index(stage)
    return [s for s in STAGES[:i + 1]
            if diff[s][0] == diff[s][1]]


def test_comment_only_config_edit(tmp_path):
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    edit(repo, "designs/nangate45/gcd/config.mk",
         lambda t: t + "\n# just a comment\n")
    diff = _diff_keys(flow, repo, base)
    assert all(a == b for a, b in diff.values())


def test_floorplan_var_change(tmp_path):
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    edit(repo, "designs/nangate45/gcd/config.mk",
         lambda t: t.replace("CORE_UTILIZATION = 55",
                             "CORE_UTILIZATION = 60"))
    diff = _diff_keys(flow, repo, base)
    for s in STAGES:
        assert (diff[s][0] != diff[s][1]) == (s == "floorplan"), s


def test_cts_var_change(tmp_path):
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    edit(repo, "designs/nangate45/gcd/config.mk",
         lambda t: t + "\nexport CTS_BUF_DISTANCE = 60\n")
    diff = _diff_keys(flow, repo, base)
    for s in STAGES:
        assert (diff[s][0] != diff[s][1]) == (s == "cts"), s


def test_override_var_change(tmp_path):
    """`override export` assignments are real recipe inputs: an
    unhashed one let two commits share a stage key while their builds
    differed (cache-soundness mismatch)."""
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    edit(repo, "designs/nangate45/gcd/config.mk",
         lambda t: t + "\noverride export ABC_LOAD_IN_FF = 1.5\n")
    diff = _diff_keys(flow, repo, base)
    assert all(a != b for a, b in diff.values())


def test_cts_script_edit(tmp_path):
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    edit(repo, "scripts/cts.tcl", lambda t: t + "\nputs 2\n")
    diff = _diff_keys(flow, repo, base)
    for s in STAGES:
        assert (diff[s][0] != diff[s][1]) == (s == "cts"), s


def test_makefile_edit_all_differ(tmp_path):
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    edit(repo, "Makefile", lambda t: t + "\n# touch\n")
    diff = _diff_keys(flow, repo, base)
    assert all(a != b for a, b in diff.values())


def test_new_notes_md_all_equal(tmp_path):
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    (repo / "notes.md").write_text("scratch\n")
    diff = _diff_keys(flow, repo, base)
    assert all(a == b for a, b in diff.values())


def test_new_unreferenced_tcl_no_key_change(tmp_path):
    """§9.3 "New files": scratch the recipe never names enters no key."""
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    (repo / "foo.tcl").write_text("puts hi\n")
    diff = _diff_keys(flow, repo, base)
    assert all(a == b for a, b in diff.values())


def test_new_scratch_set_unreferenced(tmp_path):
    """config_v2.mk + constraint_tight.sdc + sweep.sh unreferenced:
    closure says unreachable — no key changes."""
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    d = repo / "designs/nangate45/gcd"
    (d / "config_v2.mk").write_text("export X = 1\n")
    (d / "constraint_tight.sdc").write_text("create_clock -period 0.1\n")
    (d / "sweep.sh").write_text("#!/bin/sh\n")
    diff = _diff_keys(flow, repo, base)
    assert all(a == b for a, b in diff.values())


def test_sdc_var_pointed_new_file_reachable(tmp_path):
    """SDC_FILE pointed at a new file → reachable at synth, every key
    differs, and the file materializes."""
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    d = repo / "designs/nangate45/gcd"
    (d / "constraint_tight.sdc").write_text("create_clock -period 0.1\n")
    edit(repo, "designs/nangate45/gcd/config.mk",
         lambda t: t.replace("constraint.sdc",
                             "constraint_tight.sdc"))
    diff = _diff_keys(flow, repo, base)
    assert all(a != b for a, b in diff.values())
    workdir = repo.parent / "mat"
    flow.materialize(str(repo), head(repo), base, PROTECTED, workdir)
    assert (workdir / "designs/nangate45/gcd/constraint_tight.sdc") \
        .exists()


def test_new_helper_named_by_hook(tmp_path):
    """hook.tcl via PRE_CTS_TCL sources helper.tcl — both reachable at
    cts; earlier stage keys unchanged."""
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    d = repo / "designs/nangate45/gcd"
    (d / "helper.tcl").write_text("puts helper\n")
    # the common idiom: base name as a path component — still names
    # the file under the component-set rule
    (d / "hook.tcl").write_text(
        "source $::env(DESIGN_DIR)/helper.tcl\n")
    edit(repo, "designs/nangate45/gcd/config.mk",
         lambda t: t + "\nexport PRE_CTS_TCL = $(DESIGN_DIR)/hook.tcl\n")
    diff = _diff_keys(flow, repo, base)
    for s in STAGES:
        assert (diff[s][0] != diff[s][1]) == (s == "cts"), s
    # both files are in the build tree (helper only via the hook)
    new_files = set(flow.build_tree_new_files(str(repo), head(repo),
                                              base))
    assert {"designs/nangate45/gcd/helper.tcl",
            "designs/nangate45/gcd/hook.tcl"} <= new_files


def test_new_file_named_by_baseline_script(tmp_path):
    """A new file whose base name occurs in a baseline script is
    reachable — it inherits the namer's stage (cts for cts.tcl)."""
    repo, _base = _base_and_head(tmp_path)
    flow = _flow()
    # the namer must be in the baseline: commit the script reference
    # first, then add the named file
    edit(repo, "scripts/cts.tcl", lambda t: t + "\nsource magic.tcl\n")
    base2 = commit_all(repo)
    (repo / "magic.tcl").write_text("puts m\n")
    diff = _diff_keys(flow, repo, base2)
    for s in STAGES:
        assert (diff[s][0] != diff[s][1]) == (s == "cts"), s


def test_new_file_named_unattributable(tmp_path):
    """Named only by an unattributable baseline file (util/) → reachable
    but unattributable: every key changes."""
    repo, _base = _base_and_head(tmp_path)
    flow = _flow()
    (repo / "util").mkdir()
    (repo / "util" / "notes.txt").write_text("see magic.tcl\n")
    base2 = commit_all(repo)
    (repo / "magic.tcl").write_text("puts m\n")
    diff = _diff_keys(flow, repo, base2)
    assert all(a != b for a, b in diff.values())


def test_new_variant_config_not_parsed(tmp_path):
    """designs/nangate45/gcd_v2/vars.mk is an ordinary (new) file —
    its variables are never parsed. (A literal `config.mk` basename
    would be *named* by the Makefile's `config.mk` token — base-name
    collisions are conservatively reachable under §9.3.)"""
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    d = repo / "designs/nangate45/gcd_v2"
    d.mkdir(parents=True)
    (d / "vars.mk").write_text(
        "export CORE_UTILIZATION = 99\nexport SDC_FILE = foo.sdc\n")
    diff = _diff_keys(flow, repo, base)
    assert all(a == b for a, b in diff.values())


def test_editing_unreachable_new_file_no_key_change(tmp_path):
    """An unreferenced file stays new vs the baseline after being edited
    in a later commit — still unreachable, still no key change."""
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    d = repo / "designs/nangate45/gcd"
    (d / "sweep.sh").write_text("echo 1\n")
    commit_all(repo)
    edit(repo, "designs/nangate45/gcd/sweep.sh", lambda t: t + "echo 2\n")
    diff = _diff_keys(flow, repo, base)
    assert all(a == b for a, b in diff.values())


def test_editing_unreferenced_baseline_file_all_differ(tmp_path):
    """A baseline non-protected file that nothing references is still
    materialized — editing it is unattributable (every key changes)."""
    repo, _base = _base_and_head(tmp_path)
    flow = _flow()
    d = repo / "designs/nangate45/gcd"
    (d / "extra_baseline.tcl").write_text("# baseline scratch\n")
    base2 = commit_all(repo)
    edit(repo, "designs/nangate45/gcd/extra_baseline.tcl",
         lambda t: t + "# edit\n")
    diff = _diff_keys(flow, repo, base2)
    assert all(a != b for a, b in diff.values())


def test_deleted_baseline_tcl_changes_all(tmp_path):
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    (repo / "designs/nangate45/gcd/pdn.tcl").unlink()
    diff = _diff_keys(flow, repo, base)
    assert all(a != b for a, b in diff.values())


def test_include_extra_mk(tmp_path):
    """`include extra.mk`: the new include is reachable and its
    variables are parsed into the built config."""
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    d = repo / "designs/nangate45/gcd"
    (d / "extra.mk").write_text("export CTS_BUF_DISTANCE = 60\n")
    edit(repo, "designs/nangate45/gcd/config.mk",
         lambda t: t + "\ninclude extra.mk\n")
    diff = _diff_keys(flow, repo, base)
    # the cts-stage variable lands at cts only (per-stage inputs)
    for s in STAGES:
        assert (diff[s][0] != diff[s][1]) == (s == "cts"), s


def test_materialize_drops_unreachable(tmp_path):
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    d = repo / "designs/nangate45/gcd"
    (d / "sweep.sh").write_text("echo 1\n")
    sha = commit_all(repo)
    workdir = repo.parent / "mat2"
    flow.materialize(str(repo), sha, base, PROTECTED, workdir)
    assert not (workdir / "designs/nangate45/gcd/sweep.sh").exists()
    assert (workdir / "designs/nangate45/gcd/config.mk").exists()


def test_impl_sdc_edit_all_differ(tmp_path):
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    edit(repo, "designs/nangate45/gcd/impl.sdc",
         lambda t: t + "\n# tighter\n")
    diff = _diff_keys(flow, repo, base)
    assert all(a != b for a, b in diff.values())


def test_num_cores_all_equal(tmp_path):
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    edit(repo, "designs/nangate45/gcd/config.mk",
         lambda t: t + "\nexport NUM_CORES = 32\n")
    diff = _diff_keys(flow, repo, base)
    assert all(a == b for a, b in diff.values())


def test_ifeq_config_any_edit_changes_all(tmp_path):
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    edit(repo, "designs/nangate45/gcd/config.mk",
         lambda t: t + "\nifeq ($(X),1)\nexport A = 1\nendif\n")
    diff = _diff_keys(flow, repo, base)
    assert all(a != b for a, b in diff.values())
    # a second, comment-only edit still changes every key: the whole
    # text is hashed once the config is unattributable
    base2 = head(repo)
    edit(repo, "designs/nangate45/gcd/config.mk",
         lambda t: t + "# another comment\n")
    k0 = _keys(flow, repo, base2, base)
    sha = commit_all(repo)
    k1 = _keys(flow, repo, sha, base)
    assert all(k0[s] != k1[s] for s in STAGES)


def test_var_referenced_file_stage(tmp_path):
    """pdn.tcl is referenced by floorplan-stage PDN_TCL."""
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    edit(repo, "designs/nangate45/gcd/pdn.tcl",
         lambda t: t + "\nputs 3\n")
    diff = _diff_keys(flow, repo, base)
    for s in STAGES:
        assert (diff[s][0] != diff[s][1]) == (s == "floorplan"), s


def test_image_id_changes_all(tmp_path):
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    sha = head(repo)
    k1 = _keys(flow, repo, sha, base)
    k2 = _keys(flow, repo, sha, base, image="sha256:other")
    assert all(k1[s] != k2[s] for s in STAGES)


def test_protected_content_changes_all(tmp_path):
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    edit(repo, "platforms/nangate45/lib/"
               "NangateOpenCellLibrary_typical.lib",
         lambda t: t + "x")
    sha = commit_all(repo)
    k1 = _keys(flow, repo, sha, base)
    k2 = _keys(flow, repo, sha, sha)   # baseline = edited protected set
    assert all(k1[s] != k2[s] for s in STAGES)


def test_make_cmd_carries_tool_env():
    """Regression: _make_cmd must include the container tool env on the
    command line (the constants live in backends/toolenv; a missing
    re-export once broke the real run while unit tests stayed green)."""
    flow = OrfsFlow(platform="nangate45", design="gcd")
    cmd = flow._make_cmd(None, None, ["do-synth"])
    assert "DESIGN_CONFIG=./designs/nangate45/gcd/config.mk" in cmd
    for var in ("YOSYS_EXE", "OPENROAD_EXE", "OPENSTA_EXE",
                "KEPLER_FORMAL_EXE"):
        assert any(t.startswith(f"{var}=") for t in cmd), var
    assert cmd[-1] == "do-synth"


def test_component_boundary_negatives(tmp_path):
    """`foo.tcl.bak` and `xfoo.tcl` in a namer do NOT name `foo.tcl`:
    the base name must occur as a whole component."""
    repo, _ = _base_and_head(tmp_path)
    flow = _flow()
    edit(repo, "scripts/cts.tcl",
         lambda t: t + "\nsource foo.tcl.bak\nsource xfoo.tcl\n")
    base2 = commit_all(repo)
    (repo / "foo.tcl").write_text("puts f\n")
    diff = _diff_keys(flow, repo, base2)
    assert all(a == b for a, b in diff.values())


def test_unparseable_line_makes_config_unattributable(tmp_path):
    """A config line the parser cannot account for (here a shell
    assignment) makes the whole config unattributable: its text is in
    every key, so any edit changes all keys."""
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    edit(repo, "designs/nangate45/gcd/config.mk",
         lambda t: t + "\nFOO != echo hi\n")
    diff = _diff_keys(flow, repo, base)
    assert all(a != b for a, b in diff.values())
    base2 = head(repo)
    edit(repo, "designs/nangate45/gcd/config.mk",
         lambda t: t + "\nBAR != echo there\n")
    diff2 = _diff_keys(flow, repo, base2)
    assert all(a != b for a, b in diff2.values())


def test_unsupported_config_still_names(tmp_path):
    """An ifeq config cannot be parsed for variables, but its text
    still acts as a namer: a file it mentions is reachable
    (unattributable → every key), materialized, and an edit to it
    changes all keys."""
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    d = repo / "designs/nangate45/gcd"
    (d / "alt.sdc").write_text("create_clock -period 0.4 clk\n")
    edit(repo, "designs/nangate45/gcd/config.mk",
         lambda t: t + "\nifeq ($(SWEEP),1)\n"
                       "export SDC_FILE = $(DESIGN_DIR)/alt.sdc\n"
                       "endif\n")
    diff = _diff_keys(flow, repo, base)
    assert all(a != b for a, b in diff.values())
    new_files = set(flow.build_tree_new_files(str(repo), head(repo),
                                              base))
    assert "designs/nangate45/gcd/alt.sdc" in new_files
    base2 = head(repo)
    edit(repo, "designs/nangate45/gcd/alt.sdc",
         lambda t: t + "# edit\n")
    diff2 = _diff_keys(flow, repo, base2)
    assert all(a != b for a, b in diff2.values())


def test_assignment_form_enters_key(tmp_path):
    """Same resolved value, different assignment form → different key
    at the variable's stages only (§11.4.2; the DONT_USE_CELLS
    `export :=` vs `export override =` soundness finding)."""
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    cfg_path = "designs/nangate45/gcd/config.mk"
    orig = "export CORE_UTILIZATION = 55"
    variants = [
        "export CORE_UTILIZATION := 55",
        "export override CORE_UTILIZATION = 55",
        "CORE_UTILIZATION = 55",
        "export CORE_UTILIZATION ?= 55",
    ]
    k0 = _keys(flow, repo, base, base)
    keys = []
    for v in variants:
        edit(repo, cfg_path, lambda t, v=v: t.replace(orig, v))
        sha = commit_all(repo)
        k = _keys(flow, repo, sha, base)
        keys.append(k)
        for s in STAGES:
            assert (k[s] != k0[s]) == (s == "floorplan"), (v, s)
        edit(repo, cfg_path, lambda t, v=v: t.replace(v, orig))
        commit_all(repo)
    for i in range(len(variants)):
        for j in range(i + 1, len(variants)):
            assert keys[i]["floorplan"] != keys[j]["floorplan"], \
                (variants[i], variants[j])


def test_var_line_reorder_no_key_change(tmp_path):
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    edit(repo, "designs/nangate45/gcd/config.mk", lambda t: t.replace(
        "export CORE_MARGIN = 2\nexport PLACE_DENSITY_LB_ADDON = 0.20",
        "export PLACE_DENSITY_LB_ADDON = 0.20\nexport CORE_MARGIN = 2"))
    diff = _diff_keys(flow, repo, base)
    assert all(a == b for a, b in diff.values())


def test_append_order_enters_key(tmp_path):
    """`+=` sequences keep their order in the variable's key entry."""
    repo, base = _base_and_head(tmp_path)
    flow = _flow()
    p = "designs/nangate45/gcd/config.mk"
    edit(repo, p, lambda t: t +
         "\nexport EXTRA_FOO = x\nEXTRA_FOO += a\nEXTRA_FOO += b\n")
    sha1 = commit_all(repo)
    k1 = _keys(flow, repo, sha1, base)
    edit(repo, p, lambda t: t.replace(
        "EXTRA_FOO += a\nEXTRA_FOO += b",
        "EXTRA_FOO += b\nEXTRA_FOO += a"))
    sha2 = commit_all(repo)
    k2 = _keys(flow, repo, sha2, base)
    assert all(k1[s] != k2[s] for s in STAGES)
