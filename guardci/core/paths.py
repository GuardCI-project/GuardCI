"""Repo-path glob matching with gitignore-like semantics.

``**`` matches zero or more path segments (directories); ``*`` and ``?``
never cross ``/``; a pattern without wildcards is an exact match.
Unlike ``fnmatch``, a bare ``*`` does not match nested paths.
"""

from __future__ import annotations

import re

_CACHE: dict[str, re.Pattern] = {}

_STAR = "\x00"


def _translate(pattern: str) -> re.Pattern:
    rx = _CACHE.get(pattern)
    if rx is not None:
        return rx
    parts = []
    for seg in pattern.split("/"):
        if seg == "**":
            parts.append(_STAR)
            continue
        out = []
        for ch in seg:
            if ch == "*":
                out.append("[^/]*")
            elif ch == "?":
                out.append("[^/]")
            else:
                out.append(re.escape(ch))
        parts.append("".join(out))
    body = "/".join(parts)
    body = body.replace(f"/{_STAR}/", "/(?:.+/)?")   # mid '**': 0+ dirs
    body = re.sub(rf"/{_STAR}$", "/.+", body)         # trailing '**'
    body = re.sub(rf"^{_STAR}/", "(?:.+/)?", body)    # leading '**'
    if body == _STAR:
        body = ".+"
    rx = re.compile("^" + body + "$")
    _CACHE[pattern] = rx
    return rx


def match(path: str, pattern: str) -> bool:
    """True if ``path`` matches ``pattern`` (repo-relative, '/'-separated)."""
    if "**" not in pattern and "*" not in pattern and "?" not in pattern:
        return path == pattern
    return bool(_translate(pattern).match(path))


def match_any(path: str, patterns) -> bool:
    return any(match(path, p) for p in patterns)
