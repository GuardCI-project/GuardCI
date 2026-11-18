"""ppa-hack-bench snapshot loader.

Knows the phb on-disk layout: ``snapshots/NNNN-<event>/manifest.json`` +
``text.tar.zst``. Produces a per-snapshot delta (writes + deletes +
uncaptured-change markers) for the driver, applying the flow-root
filter, the SDC path translation and the uncaptured-content placeholder
rule.

Change detection is metadata-based (``(class, size, mtime)``): only
entries that differ from the previous manifest have their text content
read — unchanged files carry forward untouched. That also means the
loader never holds the whole flow's text in memory.
"""

from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

IGNORED_PREFIXES = ("results/", "logs/", "reports/", "objects/")
_CORE_DUMP_RE = re.compile(r"^core\.\d+$")   # crash dumps at flow root
PLACEHOLDER_PREFIX = "GUARDCI-REPLAY"
PLACEHOLDER = (PLACEHOLDER_PREFIX + ": content not captured "
               "(size={size}, mtime={mtime})")

_SDC_RE = re.compile(r"^designs/[^/]+/[^/]+/constraint\.sdc$")
_SDC_LINE_RE = re.compile(
    r"^(\s*(?:export\s+)?SDC_FILE\s*[:+?]?=.*)constraint\.sdc(.*)$")

_SNAP_RE = re.compile(r"^(\d{4,})-(.+)$")


def list_snapshots(source_dir: Path) -> list[dict]:
    """Ordered snapshot descriptors: seq, name, dir."""
    out = []
    for d in sorted((Path(source_dir) / "snapshots").iterdir()):
        m = _SNAP_RE.match(d.name)
        if m and (d / "manifest.json").exists():
            out.append({"seq": int(m.group(1)), "name": m.group(2),
                        "dir": d})
    return out


def load_manifest(snap_dir: Path) -> dict:
    return json.loads((snap_dir / "manifest.json").read_text())


def snapshot_time(manifest: dict, first_started: float) -> dict:
    """{timestamp, elapsed_seconds} for a snapshot."""
    ts = manifest.get("capture_started") or first_started
    ev = manifest.get("event") or {}
    elapsed = ev.get("elapsed_seconds")
    if elapsed is None:
        elapsed = ts - first_started
    return {"timestamp": ts, "elapsed_seconds": elapsed}


def translate_path(path: str) -> str:
    """Agent workspace path (below ./flow/, prefix stripped) → repo path."""
    if _SDC_RE.match(path):
        return path.rsplit("/", 1)[0] + "/impl.sdc"
    return path


def translate_content(repo_path: str, text: str) -> str:
    """SDC_FILE assignment lines: constraint.sdc literal → impl.sdc."""
    if not repo_path.endswith("config.mk"):
        return text
    out = []
    for line in text.splitlines():
        m = _SDC_LINE_RE.match(line)
        out.append(m.group(1) + "impl.sdc" + m.group(2) if m else line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _member_path(cache_dir: Path, seq: int, flow_path: str) -> Path:
    """text.tar.zst members are named ``./flow/...`` — same as manifest."""
    return cache_dir / f"{seq:04d}" / flow_path


def flow_files(manifest: dict) -> dict:
    """{repo_path: entry} for file/symlink entries under ./flow/ that are
    not in an ignored directory, with SDC path translation applied."""
    out = {}
    for e in manifest["entries"]:
        p = e["path"]
        if not p.startswith("./flow/"):
            continue
        if e["type"] == "dir":
            continue
        rel = p[len("./flow/"):]
        if rel.startswith(IGNORED_PREFIXES) or _CORE_DUMP_RE.match(rel):
            continue
        out[translate_path(rel)] = e
    return out


def _changed(e: dict, prev: dict | None) -> bool:
    return (prev is None
            or (prev["class"], prev["size"], prev["mtime"])
            != (e["class"], e["size"], e["mtime"]))


def needed_members(manifests: list) -> dict:
    """{seq: set(member paths)} — text-stored members whose content is
    actually needed by the replay: entries that changed vs the previous
    manifest. Content lives in the archive of ``stored_in`` (usually the
    same snapshot)."""
    need: dict[int, set] = {}
    prev = {}
    for m in manifests:
        for e in m["entries"]:
            p = e["path"]
            if not p.startswith("./flow/") or e["type"] == "dir":
                continue
            rel = p[len("./flow/"):]
            if (rel.startswith(IGNORED_PREFIXES)
                    or _CORE_DUMP_RE.match(rel)):
                continue
            if e["class"] != "text-stored":
                continue
            if _changed(e, prev.get(p)):
                src = e.get("stored_in") or m["seq"]
                need.setdefault(src, set()).add(p)
        prev = {e["path"]: e for e in m["entries"]}
    return need


def extract_needed(snapshots: list, need: dict,
                   cache_dir: Path) -> Path:
    """Extract only the needed members of each snapshot's text.tar.zst
    into ``cache_dir/<seq>/`` (parallel across snapshots)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    by_seq = {s["seq"]: s for s in snapshots}

    def _one(item):
        seq, members = item
        archive = by_seq[seq]["dir"] / "text.tar.zst"
        dest = cache_dir / f"{seq:04d}"
        marker = dest / ".done"
        if marker.exists():
            return
        dest.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            marker.touch()
            return
        members_file = dest / ".members"
        members_file.write_bytes(
            "".join(m + "\n" for m in sorted(members))
            .encode("utf-8", "surrogateescape"))
        subprocess.run(
            f"zstd -dc {archive} | tar -x -C {dest} -T {members_file}",
            shell=True, check=True, capture_output=True)
        marker.touch()

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(_one, need.items()))
    return cache_dir


def compute_delta(manifest: dict, prev_entries: dict,
                  texts_root: Path) -> dict:
    """Delta between the previous snapshot and this one:
    ``{writes: {repo_path: text}, deletes: [repo_path],
    uncaptured: [repo_path]}``.

    ``prev_entries`` maps repo_path → previous manifest entry.
    """
    writes: dict[str, str] = {}
    uncaptured = []
    cur = flow_files(manifest)
    for repo_path, e in cur.items():
        cls = e["class"]
        prev = prev_entries.get(repo_path)
        changed = _changed(e, prev)
        if not changed:
            continue
        if cls == "text-stored":
            seq = e.get("stored_in") or manifest["seq"]
            member = _member_path(texts_root, seq,
                                  "./flow/" + _untranslate(repo_path))
            try:
                text = member.read_text()
            except (OSError, UnicodeDecodeError):
                writes[repo_path] = PLACEHOLDER.format(
                    size=e["size"], mtime=e["mtime"])
                uncaptured.append(repo_path)
            else:
                writes[repo_path] = translate_content(repo_path, text)
        else:
            uncaptured.append(repo_path)
            writes[repo_path] = PLACEHOLDER.format(
                size=e["size"], mtime=e["mtime"])
    deletes = [p for p in prev_entries if p not in cur]
    return {"writes": writes, "deletes": deletes,
            "uncaptured": uncaptured}


def _untranslate(repo_path: str) -> str:
    """Repo path → workspace path (impl.sdc ↔ constraint.sdc)."""
    m = re.match(r"^(designs/[^/]+/[^/]+/)impl\.sdc$", repo_path)
    return m.group(1) + "constraint.sdc" if m else repo_path
