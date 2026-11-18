"""config.mk parsing, variable resolution and path extraction.

Ported from ppa-hack-bench ``changes.py`` (``_mk_parse`` / ``_mk_semantic``)
and extended with make-variable resolution and wildcard expansion.
"""

from __future__ import annotations

import posixpath
import re

_MK_RE = re.compile(
    r"^((?:(?:export|override)\s+)*)(\w+)\s*"
    r"(:::=|::=|:=|\+=|\?=|=)\s*(.*)$")
_EXPORT_RE = re.compile(r"^(?:export|unexport)(?:\s+\w+)+\s*$")
_INC_RE = re.compile(r"^(?:-?include|sinclude)\s+(.+)$")
_VAR_RE = re.compile(r"\$\((\w+)\)|\$\{(\w+)\}")
_FUNC_RE = re.compile(
    r"\$\((dir|notdir|abspath|realpath|sort|firstword|lastword)\s+"
    r"([^)]*)\)")
_WILDCARD_RE = re.compile(r"\$\(wildcard\s+([^)]*)\)")
# a call the resolver does not implement: $(name args…) or the
# substitution-reference form $(VAR:pat=rep)
_FUNCALL_RE = re.compile(r"\$\(([A-Za-z_]\w*)\s+")
_SUBSTREF_RE = re.compile(r"\$[(]\w+:")
_SUPPORTED_FUNCS = {"dir", "notdir", "abspath", "realpath", "wildcard",
                    "sort", "firstword", "lastword"}

_PATH_EXTS = (".v", ".sv", ".lib", ".lef", ".sdc", ".tcl", ".def",
              ".gds", ".rules")


def parse_config_mk(text: str, resolver=None, _depth: int = 0,
                    _seen: set | None = None,
                    _collected: list | None = None,
                    _unparsed: list | None = None,
                    _forms: dict | None = None) -> dict:
    """Parse a config.mk into {var: raw_value}.

    Assignment forms: ``= := ::= :::= ?= +=`` with any mix of
    ``export``/``override`` prefixes; ``\\`` continuations are joined
    first. ``include``/``-include``/``sinclude`` lines are expanded
    through ``resolver``: called with the (possibly
    ``$(VAR)``-containing) include operand, it must return
    ``(key, text)`` or None. Included assignments behave as if inlined
    at that position. Recursion is bounded at depth 5 with cycle
    protection on ``key``; every resolved include path is appended to
    ``_collected`` when given.

    When ``_unparsed`` (a list) is given, every logical line the parser
    cannot account for is appended to it — rules/targets, conditionals,
    ``define`` blocks, ``!=``/function calls the resolver does not
    implement, includes that fail to resolve — so a caller can tell a
    supported config (empty list) from one whose variables are
    incomplete. Without it the parse stays lenient and simply skips
    what it does not understand.

    When ``_forms`` (a dict) is given, it is filled with
    ``{var: [[prefixes, operator], ...]}`` — every assignment of the
    variable in order, included files inlined at their position, where
    ``prefixes`` is the sorted list of ``export``/``override`` words
    and ``operator`` the assignment operator; a bare
    ``export NAME``/``unexport NAME`` line records ``[["export"], ""]``
    / ``[["unexport"], ""]`` for each name.
    """
    if _seen is None:
        _seen = set()
    joined = []
    buf = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        joined.append(buf)
        buf = ""
    if buf.strip():
        joined.append(buf)
    out = {}
    for line in joined:
        line = line.strip()
        if not line:
            continue
        im = _INC_RE.match(line)
        if im:
            hit = resolver(im.group(1).strip()) \
                if resolver is not None and _depth < 5 else None
            if hit is not None:
                key, sub_text = hit
                if key not in _seen:
                    _seen.add(key)
                    if _collected is not None:
                        _collected.append(key)
                    out.update(parse_config_mk(
                        sub_text, resolver, _depth + 1, _seen,
                        _collected, _unparsed, _forms))
            else:
                _collect_unparsed(_unparsed, line)
            continue
        m = _MK_RE.match(line)
        if m:
            prefixes = sorted(m.group(1).split())
            var, op, val = m.group(2), m.group(3), m.group(4).strip()
            if _forms is not None:
                _forms.setdefault(var, []).append([prefixes, op])
            if _has_unsupported_call(val):
                _collect_unparsed(_unparsed, line)
            if op == "+=":
                out[var] = (out.get(var, "") + " " + val).strip()
            else:
                out[var] = val
            continue
        em = _EXPORT_RE.match(line)
        if em:
            if _forms is not None:
                words = line.split()
                for name in words[1:]:
                    _forms.setdefault(name, []).append([[words[0]], ""])
            continue
        _collect_unparsed(_unparsed, line)
    return out


def _collect_unparsed(unparsed, line):
    if unparsed is not None:
        unparsed.append(line)


def _has_unsupported_call(value: str) -> bool:
    if _SUBSTREF_RE.search(value):
        return True
    for m in _FUNCALL_RE.finditer(value):
        if m.group(1) not in _SUPPORTED_FUNCS:
            return True
    return False


def unparsed_lines(text: str, resolver=None) -> list:
    """Logical lines of a config the parser cannot account for;
    an empty result means the config is fully supported."""
    unparsed: list = []
    parse_config_mk(text, resolver, _unparsed=unparsed)
    return unparsed


def lhs_name(line: str) -> str | None:
    """Variable name on a config.mk assignment line, else None."""
    m = _MK_RE.match(line.split("#", 1)[0].strip())
    return m.group(1) if m else None


def semantic_diff(before: dict, after: dict) -> dict:
    """Variable-level diff of two parsed config dicts."""
    return {
        "added": {k: v for k, v in after.items() if k not in before},
        "removed": {k: v for k, v in before.items() if k not in after},
        "changed": {k: [before[k], after[k]] for k in after
                    if k in before and before[k] != after[k]},
    }


def _expand_vars(value: str, env: dict, depth: int = 0) -> str:
    """Expand $(VAR)/${VAR} against env; unknown vars left as-is."""
    if depth > 10:
        return value

    def sub(m):
        name = m.group(1) or m.group(2)
        if name in env:
            return env[name]
        return m.group(0)

    out = _VAR_RE.sub(sub, value)
    return _expand_vars(out, env, depth + 1) if out != value else out


def _expand_funcs(value: str, env: dict) -> str:
    """Handle $(dir/notdir/abspath/realpath X), $(sort/firstword/
    lastword X)."""
    def sub(m):
        func, arg = m.group(1), _expand_vars(m.group(2), env)
        if func == "dir":
            # $(dir X) → the containing directory, matching DESIGN_DIR
            # semantics (no trailing slash): $(dir $(DESIGN_CONFIG))
            # resolves to DESIGN_DIR.
            d = arg.rstrip("/")
            return d.rsplit("/", 1)[0] if "/" in d else "."
        if func == "notdir":
            return arg.rstrip("/").rsplit("/", 1)[-1]
        words = arg.split()
        if func == "sort":
            return " ".join(sorted(set(words)))
        if func == "firstword":
            return words[0] if words else ""
        if func == "lastword":
            return words[-1] if words else ""
        return arg  # abspath/realpath: repo-relative already
    prev = None
    while prev != value:
        prev = value
        value = _FUNC_RE.sub(sub, value)
    return value


def build_env(cfg: dict, config_dir: str) -> dict:
    """Variable environment for resolving a config.mk's values.

    Pre-set: DESIGN_HOME=designs, PLATFORM_DIR=platforms/<PLATFORM>,
    FLOW_HOME=., DESIGN_DIR=<config.mk dir>, DESIGN_CONFIG=<path>;
    then every variable the file defines.
    """
    env = {
        "DESIGN_HOME": "designs",
        "PLATFORM_DIR": f"platforms/{cfg.get('PLATFORM', '')}",
        "FLOW_HOME": ".",
        "DESIGN_DIR": config_dir,
        "DESIGN_CONFIG": f"{config_dir}/config.mk" if config_dir
        else "config.mk",
    }
    env.update(cfg)
    return env


def resolve_value(value: str, env: dict) -> str:
    """Fully resolve a raw config value: variables first (so that
    ``$(dir $(VAR))`` sees the expanded path), then functions, then a
    final variable pass."""
    value = _expand_vars(value, env)
    value = _expand_funcs(value, env)
    return _expand_vars(value, env)


def expand_wildcards(resolved: str, files) -> str:
    """Replace $(wildcard pat) with matching repo paths (space-joined)."""
    import fnmatch as _fn

    def sub(m):
        pat = m.group(1).strip()
        hits = sorted(f for f in files if _fn.fnmatchcase(f, pat))
        return " ".join(hits) if hits else ""

    return _WILDCARD_RE.sub(sub, resolved)


def path_values(cfg: dict, env: dict, files=()) -> list:
    """[(var, path)] — resolved values that look like repo paths.

    A token counts as a path if it contains '/' or ends with a known HDL/
    library extension. $(wildcard ...) is expanded against ``files``.
    """
    out = []
    for var, raw in cfg.items():
        resolved = resolve_value(raw, env)
        resolved = expand_wildcards(resolved, files)
        for tok in resolved.split():
            if tok.startswith("-"):
                continue
            if "/" in tok or tok.endswith(_PATH_EXTS):
                out.append((var, posixpath.normpath(tok)))
    return out
