"""SDC parsing — port of ppa-hack-bench ``_sdc_parse`` with per-command
parameter extraction for sdc_delta classification."""

from __future__ import annotations

import re

_KEY_PARAMS = ("-name", "-period", "-from", "-to", "-setup", "-hold",
               "-max", "-min", "-clock", "-rise", "-fall", "-through",
               "-value", "-objects", "-type")


def parse_sdc(text: str) -> dict:
    """Resolve simple set-substitutions, join continuations, strip comments.

    Returns {"commands": [{"verb", "raw", "params"}], "vars": {...}}.
    """
    lines = []
    buf = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line and not buf:
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        lines.append(buf.strip())
        buf = ""
    if buf.strip():
        lines.append(buf.strip())
    varmap = {}
    commands = []
    for line in lines:
        if not line:
            continue
        m = re.match(r"^set\s+(\w+)\s+(.+)$", line)
        if m and not line.startswith("set_"):
            varmap[m.group(1)] = m.group(2).strip()
        else:
            commands.append(line)
    resolved = []
    for cmd in commands:
        def sub(match):
            name = match.group(1) or match.group(2)
            return varmap.get(name, match.group(0))
        resolved.append(re.sub(r"\$(\w+)|\$\{(\w+)\}", sub, cmd))

    parsed = []
    for cmd in resolved:
        toks = cmd.split()
        verb = toks[0] if toks else ""
        params = {}
        positional = []
        i = 1
        while i < len(toks):
            t = toks[i]
            if t in _KEY_PARAMS and i + 1 < len(toks):
                params[t] = toks[i + 1]
                i += 2
            elif t.startswith("-"):
                params[t] = True
                i += 1
            else:
                positional.append(t)
                i += 1
        if positional:
            params["_pos"] = positional
        parsed.append({"verb": verb, "raw": cmd, "params": params})
    return {"commands": parsed, "vars": varmap}
