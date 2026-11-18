"""Layering guardrail (DESIGN.md §1.1): checks/ and core/ must stay
tool-agnostic.

- ``guardci/checks/*.py`` may only import ``..core.*``, ``..flows.base``,
  ``..artifacts.*``, ``.base`` and stdlib/third-party deps.
- ``guardci/core/*.py`` may not import concrete flow/checker/backend/
  executor implementations — only the corresponding ``base`` modules.
- ``guardci/backends/**`` must not import ``flows.*`` other than
  ``flows.base`` (shared container constants live in
  ``backends/toolenv.py``).

If this test fails, a checker or the core learned something about a
specific EDA tool — move that knowledge behind FlowAdapter/Checker APIs.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "guardci"

CHECK_ALLOWED_PREFIXES = ("core", "flows.base", "artifacts",
                          "checks.base")
# plugin layers whose *implementations* core must not import — only the
# corresponding ``<pkg>.base`` interface module is allowed
CORE_PLUGIN_PKGS = ("flows", "checks", "backends", "executors")


def _core_forbidden(rel: str) -> bool:
    pkg, _, rest = rel.partition(".")
    if pkg not in CORE_PLUGIN_PKGS or not rest:
        return False
    return rest != "base" and not rest.startswith("base.")


def _imported_modules(path: Path):
    """Fully qualified module names imported by ``path`` (relative → abs)."""
    tree = ast.parse(path.read_text())
    pkg = path.parent.relative_to(ROOT).parts
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = list(pkg[: len(pkg) - (node.level - 1)]) \
                if node.level else []
            mod = ".".join([*base, node.module or ""])
            for a in node.names:
                out.append(f"{mod}.{a.name}" if mod else a.name)
            if mod:
                out.append(mod)
        elif isinstance(node, ast.Import):
            out += [a.name for a in node.names]
    return out


def _rel(mod: str) -> str | None:
    """Project-relative module path, or None for external imports."""
    if mod.startswith("guardci."):
        return mod[len("guardci."):]
    top = mod.split(".")[0]
    return mod if top in {"core", "flows", "checks", "backends",
                          "executors", "artifacts", "contract", "remote",
                          "cli"} else None


def test_checks_are_tool_agnostic():
    bad = []
    for f in sorted((ROOT / "checks").glob("*.py")):
        if f.name in ("__init__.py", "base.py"):
            continue
        for mod in _imported_modules(f):
            rel = _rel(mod)
            if rel is None:
                continue
            if not any(rel == p or rel.startswith(p + ".")
                       for p in CHECK_ALLOWED_PREFIXES):
                bad.append(f"{f.name}: imports {mod}")
    assert not bad, "\n".join(bad)


def test_core_imports_no_concrete_impls():
    bad = []
    for f in sorted((ROOT / "core").glob("*.py")):
        for mod in _imported_modules(f):
            rel = _rel(mod)
            if rel is None:
                continue
            if _core_forbidden(rel):
                bad.append(f"{f.name}: imports {mod}")
    assert not bad, "\n".join(dict.fromkeys(bad))


def test_recorders_are_tool_agnostic():
    """§12.2: recorders may import only core.*, flows.base and
    recorders.base — tool names stay under flows/."""
    bad = []
    for f in sorted((ROOT / "recorders").glob("*.py")):
        if f.name in ("__init__.py",):
            continue
        for mod in _imported_modules(f):
            rel = _rel(mod)
            if rel is None:
                continue
            if not any(rel == p or rel.startswith(p + ".")
                       for p in ("core", "flows.base",
                                 "recorders.base")):
                bad.append(f"{f.name}: imports {mod}")
    assert not bad, "\n".join(bad)


def test_backends_do_not_import_flows():
    bad = []
    for f in sorted((ROOT / "backends").rglob("*.py")):
        if f.name in ("base.py",):
            continue
        for mod in _imported_modules(f):
            rel = _rel(mod)
            if rel is None or not rel.startswith("flows"):
                continue
            if rel != "flows.base" and not rel.startswith("flows.base."):
                bad.append(f"{f.name}: imports {mod}")
    assert not bad, "\n".join(dict.fromkeys(bad))


def test_no_commit_guardci_dir_reads():
    """§11.3: no code path may read files under a commit's `.guardci/`
    directory — verdicts never depend on author-supplied CI files.
    Path-prefix classification (`.guardci/` → ci_namespace) is allowed;
    opening/reading the file's content is not."""
    import re
    bad = []
    readers = re.compile(r'read_text|read_bytes|open\(|json\.load')
    for f in ROOT.rglob("*.py"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if ".guardci/" not in line:
                continue
            code = line.split("#", 1)[0]
            if readers.search(code):
                bad.append(f"{f.name}:{i}: {line.strip()}")
    assert not bad, "\n".join(bad)
