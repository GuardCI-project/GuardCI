"""Contract schema validation — hand-written, no third-party schema lib.

Errors raise ContractError with a message that names the field path,
e.g. ``clauses[2].severity``.
"""

from __future__ import annotations

SEVERITIES = {"hard", "warn", "info", "route"}

_TOP_KEYS = {"name", "extends", "protected", "recipe", "clauses",
             "edges", "routing", "verification", "protection",
             "tradable", "predict"}

_KINDS = {"invariant", "exit"}
_VERIFICATION_MODES = {"full", "routed"}
_PROTECTION_MODES = {"default", "explicit"}
_VOID_MODES = {"descendants", "until_restored"}


class ContractError(ValueError):
    """Raised when a contract document fails validation."""


def _err(path: str, msg: str) -> ContractError:
    return ContractError(f"{path}: {msg}")


def _check_str_list(value, path: str):
    if not isinstance(value, list) or \
            any(not isinstance(v, str) for v in value):
        raise _err(path, "expected a list of strings")


def validate(doc) -> None:
    """Validate a parsed contract YAML document (a single level, pre-merge)."""
    if not isinstance(doc, dict):
        raise _err("$", "contract must be a mapping")

    for key in doc:
        if key not in _TOP_KEYS:
            raise _err(key, f"unknown top-level key (allowed: "
                            f"{sorted(_TOP_KEYS)})")

    if "name" in doc and not isinstance(doc["name"], str):
        raise _err("name", "expected a string")

    if "extends" in doc and doc["extends"] is not None and \
            not isinstance(doc["extends"], str):
        raise _err("extends", "expected string or null")

    for key in ("protected", "recipe"):
        if key in doc:
            _check_str_list(doc[key], key)

    clauses = doc.get("clauses", [])
    if not isinstance(clauses, list):
        raise _err("clauses", "expected a list")
    for i, c in enumerate(clauses):
        cp = f"clauses[{i}]"
        if not isinstance(c, dict):
            raise _err(cp, "expected a mapping")
        if "check" not in c or not isinstance(c["check"], str):
            raise _err(f"{cp}.check", "missing or not a string")
        if "id" in c and c["id"] is not None and \
                not isinstance(c["id"], str):
            raise _err(f"{cp}.id", "expected string or null")
        sev = c.get("severity", "hard")
        if sev not in SEVERITIES:
            raise _err(f"{cp}.severity",
                       f"expected one of {sorted(SEVERITIES)}, got {sev!r}")
        if "kind" in c and c["kind"] not in _KINDS:
            raise _err(f"{cp}.kind",
                       f"expected one of {sorted(_KINDS)}, "
                       f"got {c['kind']!r}")

    ver = doc.get("verification")
    if ver is not None:
        if not isinstance(ver, dict):
            raise _err("verification", "expected a mapping")
        mode = ver.get("mode")
        if mode is not None and mode not in _VERIFICATION_MODES:
            raise _err("verification.mode",
                       f"expected one of {sorted(_VERIFICATION_MODES)}, "
                       f"got {mode!r}")
        sup = ver.get("superseded")
        if sup is not None and sup not in ("verify", "defer", "skip"):
            raise _err("verification.superseded",
                       "expected one of ['verify', 'defer', 'skip'], "
                       f"got {sup!r}")
        src = ver.get("source")
        if src is not None and src not in ("artifact", "rebuild"):
            raise _err("verification.source",
                       "expected one of ['artifact', 'rebuild'], "
                       f"got {src!r}")
        fb = ver.get("fallback")
        if fb is not None and fb not in ("rebuild", "none"):
            raise _err("verification.fallback",
                       "expected one of ['rebuild', 'none'], "
                       f"got {fb!r}")
        vd = ver.get("void")
        if vd is not None and vd not in _VOID_MODES:
            raise _err("verification.void",
                 f"expected one of {sorted(_VOID_MODES)}, "
                 f"got {vd!r}")

    prot = doc.get("protection")
    if prot is not None:
        if not isinstance(prot, dict):
            raise _err("protection", "expected a mapping")
        pmode = prot.get("mode")
        if pmode is not None and pmode not in _PROTECTION_MODES:
            raise _err("protection.mode",
                       f"expected one of {sorted(_PROTECTION_MODES)}, "
                       f"got {pmode!r}")
        rt = prot.get("rel_tol")
        if rt is not None and not isinstance(rt, (int, float)):
            raise _err("protection.rel_tol", "expected a number")
        for key in prot:
            if key not in ("mode", "rel_tol"):
                raise _err(f"protection.{key}", "unknown key")

    if "tradable" in doc:
        _check_str_list(doc["tradable"], "tradable")

    pred = doc.get("predict")
    if pred is not None and not isinstance(pred, dict):
        raise _err("predict", "expected a mapping")

    edges = doc.get("edges", [])
    if not isinstance(edges, list):
        raise _err("edges", "expected a list")
    for i, e in enumerate(edges):
        ep = f"edges[{i}]"
        if not isinstance(e, dict):
            raise _err(ep, "expected a mapping")
        if "metric" not in e or not isinstance(e["metric"], str):
            raise _err(f"{ep}.metric", "missing or not a string")
        sev = e.get("severity", "warn")
        if sev not in SEVERITIES:
            raise _err(f"{ep}.severity",
                       f"expected one of {sorted(SEVERITIES)}, got {sev!r}")

    routing = doc.get("routing", {})
    if not isinstance(routing, dict):
        raise _err("routing", "expected a mapping")
    for tag, entries in routing.items():
        if not isinstance(tag, str):
            raise _err("routing", "tag keys must be strings")
        if not isinstance(entries, list) or \
                any(not isinstance(x, str) for x in entries):
            raise _err(f"routing.{tag}", "expected a list of check names")
