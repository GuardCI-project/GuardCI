import pytest
import yaml

from guardci.contract import loader, schema


def test_orfs_default_validates():
    c = loader.load("orfs-default")
    assert c.name == "orfs-default"
    assert "designs/src/**" in c.protected
    names = [cl.check for cl in c.clauses]
    assert "recipe_scope" in names and "sta_setup" in names
    assert c.routing["unknown"] == ["heavy"]


def test_extends_merge(tmp_path):
    child = tmp_path / "child.yaml"
    child.write_text(yaml.safe_dump({
        "name": "child",
        "extends": "orfs-default",
        "protected": ["extra/protected/**"],
        "recipe": ["extra.mk"],
        "clauses": [
            {"check": "recipe_scope", "severity": "warn"},
            {"check": "my_check", "severity": "hard", "foo": 1},
        ],
        "routing": {"unknown": ["my_check"], "newtag": ["lec"]},
        "edges": [{"metric": "drc_count", "severity": "hard"}],
    }))
    c = loader.load(str(child))
    # append
    assert "designs/src/**" in c.protected
    assert "extra/protected/**" in c.protected
    # clause override by check name
    by_name = {cl.check: cl for cl in c.clauses}
    assert by_name["recipe_scope"].severity.value == "warn"
    assert by_name["my_check"].params["foo"] == 1
    assert "sta_setup" in by_name  # inherited
    # routing override by tag
    assert c.routing["unknown"] == ["my_check"]
    assert c.routing["newtag"] == ["lec"]
    assert c.routing["constraints"]  # inherited
    # edges override by metric
    by_metric = {e.metric: e for e in c.edges}
    assert by_metric["drc_count"].severity.value == "hard"
    assert "hold_wns" in by_metric


def test_missing_field_reports_path(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({
        "name": "bad",
        "clauses": [{"severity": "hard"}],
    }))
    with pytest.raises(schema.ContractError, match=r"clauses\[0\]\.check"):
        loader.load(str(bad))

    bad2 = tmp_path / "bad2.yaml"
    bad2.write_text(yaml.safe_dump({
        "name": "bad2",
        "clauses": [{"check": "x", "severity": "bogus"}],
    }))
    with pytest.raises(schema.ContractError,
                       match=r"clauses\[0\]\.severity"):
        loader.load(str(bad2))

    # name is optional — an unnamed contract takes the file stem
    bad3 = tmp_path / "bad3.yaml"
    bad3.write_text(yaml.safe_dump({"clauses": []}))
    assert loader.load(str(bad3)).name == "bad3"

    bad4 = tmp_path / "bad4.yaml"
    bad4.write_text(yaml.safe_dump({"name": "x", "clauses": [],
                                    "protection": {"mode": "bogus"}}))
    with pytest.raises(schema.ContractError, match="protection.mode"):
        loader.load(str(bad4))


def test_unknown_checker_allowed(tmp_path):
    # loader validates structure, not whether checkers are registered
    doc = tmp_path / "ok.yaml"
    doc.write_text(yaml.safe_dump({
        "name": "ok",
        "clauses": [{"check": "nonexistent_checker_xyz"}],
    }))
    c = loader.load(str(doc))
    assert c.clauses[0].check == "nonexistent_checker_xyz"
