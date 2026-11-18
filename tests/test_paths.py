from guardci.core.paths import match, match_any


def test_doublestar():
    assert match("designs/src/a/b.v", "designs/src/**")
    assert match("designs/src/top.v", "designs/src/**")
    assert match("scripts/a.tcl", "scripts/**")
    assert match("scripts/a/b/c.tcl", "scripts/**")
    assert not match("designs/other/top.v", "designs/src/**")


def test_single_star_no_slash_crossing():
    assert match("designs/n45/ibex/config.mk", "designs/*/*/config.mk")
    assert not match("designs/n45/ibex/x/config.mk",
                     "designs/*/*/config.mk")
    assert not match("designs/config.mk", "designs/*/*/config.mk")


def test_exact_match():
    assert match("constraints/signoff.sdc", "constraints/signoff.sdc")
    assert not match("constraints/signoff.sdc.bak",
                     "constraints/signoff.sdc")
    assert not match("x/constraints/signoff.sdc",
                     "constraints/signoff.sdc")


def test_question_mark():
    assert match("a/b1/c", "a/b?/c")
    assert not match("a/b12/c", "a/b?/c")


def test_match_any():
    assert match_any("scripts/x.tcl", ["config.mk", "scripts/**"])
    assert not match_any("docs/x.md", ["config.mk", "scripts/**"])
