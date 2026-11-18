"""Unit tests for the ORFS config.mk parser/resolver."""

import pytest

from guardci.flows.orfs import config as cfg

SAMPLE = """\
# comment
export DESIGN_NICKNAME = gcd
export DESIGN_NAME = gcd
export PLATFORM = nangate45
export VERILOG_FILES = $(DESIGN_HOME)/src/$(DESIGN_NICKNAME)/gcd.v \\
    $(DESIGN_HOME)/src/$(DESIGN_NICKNAME)/extra.v  # more
export LIB_FILES += $(PLATFORM_DIR)/lib/a.lib
export LIB_FILES += $(PLATFORM_DIR)/lib/b.lib
export UNSET_VAR ?= fallback
export IMMED := $(PLATFORM)-now
export CORE_UTILIZATION = 55
"""


def _env(c, config_dir="designs/nangate45/gcd"):
    return cfg.build_env(c, config_dir)


def test_parse_basic_ops():
    c = cfg.parse_config_mk(SAMPLE)
    assert c["DESIGN_NAME"] == "gcd"
    assert c["PLATFORM"] == "nangate45"
    # line continuation joined
    assert "gcd.v" in c["VERILOG_FILES"] and "extra.v" in c["VERILOG_FILES"]
    # += accumulates
    assert "a.lib" in c["LIB_FILES"] and "b.lib" in c["LIB_FILES"]
    # comments stripped
    assert "#" not in c["VERILOG_FILES"]


def test_override_and_export_prefixes():
    c = cfg.parse_config_mk(
        "override export ABC_LOAD_IN_FF = 1.5\n"
        "override FOO := x\n"
        "export override BAR ?= y\n")
    assert c["ABC_LOAD_IN_FF"] == "1.5"
    assert c["FOO"] == "x"
    assert c["BAR"] == "y"


@pytest.mark.parametrize("line", [
    "override export X = 1",
    "export override X = 1",
    "X ::= 1",
    "X :::= 1",
    "X += 1",
    "export X",
    "unexport X",
])
def test_supported_lines(line):
    assert cfg.unparsed_lines(line + "\n") == []


@pytest.mark.parametrize("text", [
    "X != echo 1",
    "vpath %.v src",
    "foo: bar",
    "define A\nx\nendef",
    "X = $(shell echo 1)",
    "X = $(subst a,b,c)",
    "X = $(FOO:.v=.o)",
    "ifeq ($(X),1)\nendif",
    "include missing.mk",
])
def test_unsupported_lines(text):
    assert cfg.unparsed_lines(text + "\n") != []


def test_unsupported_value_still_parsed_lenient():
    # lenient callers (static checkers) still get the assignment
    c = cfg.parse_config_mk("X = $(shell echo 1)\nY = 2\n")
    assert c["Y"] == "2"


def test_resolve_vars():
    c = cfg.parse_config_mk(SAMPLE)
    env = _env(c)
    assert cfg.resolve_value("$(DESIGN_HOME)/src/$(DESIGN_NICKNAME)/gcd.v",
                             env) == "designs/src/gcd/gcd.v"
    assert cfg.resolve_value("${PLATFORM_DIR}/lib/a.lib", env) == \
        "platforms/nangate45/lib/a.lib"
    # unknown variables preserved
    assert cfg.resolve_value("$(NOPE)/x", env) == "$(NOPE)/x"


def test_dir_func_resolves_to_design_dir():
    env = _env({})
    assert cfg.resolve_value("$(dir $(DESIGN_CONFIG))", env) == \
        "designs/nangate45/gcd"


def test_semantic_diff():
    before = cfg.parse_config_mk("A = 1\nB = 2\n")
    after = cfg.parse_config_mk("A = 1\nB = 3\nC = 4\n")
    sd = cfg.semantic_diff(before, after)
    assert set(sd["added"]) == {"C"}
    assert set(sd["changed"]) == {"B"}
    assert not sd["removed"]


def test_path_values():
    c = cfg.parse_config_mk(SAMPLE)
    env = _env(c)
    vf = [p for v, p in cfg.path_values(c, env) if v == "VERILOG_FILES"]
    assert "designs/src/gcd/gcd.v" in vf
    assert "designs/src/gcd/extra.v" in vf
    # multi-token value flattened into (var, path) pairs
    lib_paths = [p for v, p in cfg.path_values(c, env) if v == "LIB_FILES"]
    assert "platforms/nangate45/lib/a.lib" in lib_paths
    assert "platforms/nangate45/lib/b.lib" in lib_paths
    # plain numbers are not paths
    assert "55" not in [p for _v, p in cfg.path_values(c, env)]


def test_path_values_wildcard():
    c = cfg.parse_config_mk(
        "export VERILOG_FILES = $(wildcard designs/src/gcd/*.v)\n")
    env = _env(c)
    files = ["designs/src/gcd/gcd.v", "designs/src/gcd/extra.v",
             "designs/src/other/x.v", "README.md"]
    out = cfg.path_values(c, env, files)
    assert out == [("VERILOG_FILES", "designs/src/gcd/extra.v"),
                   ("VERILOG_FILES", "designs/src/gcd/gcd.v")]


def test_assignment_forms():
    """_forms: ordered [prefixes, operator] per variable, bare
    export/unexport lines recorded too."""
    forms = {}
    cfg.parse_config_mk(
        "export X := 1\n"
        "export override X = 2\n"
        "X += 4\n"
        "Y = 3\n"
        "export Z\n"
        "unexport W\n",
        _forms=forms)
    assert forms["X"] == [[["export"], ":="],
                          [["export", "override"], "="],
                          [[], "+="]]
    assert forms["Y"] == [[[], "="]]
    assert forms["Z"] == [[["export"], ""]]
    assert forms["W"] == [[["unexport"], ""]]


def test_assignment_forms_include_order():
    """Included assignments are inlined at the include position."""
    inc = "export X = included\n"
    forms = {}
    cfg.parse_config_mk(
        "X = top\ninclude inc.mk\nX += tail\n",
        resolver=lambda operand: (operand, inc),
        _forms=forms)
    assert forms["X"] == [[[], "="], [["export"], "="], [[], "+="]]
