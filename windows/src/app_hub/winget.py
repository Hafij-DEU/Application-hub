"""winget (Windows Package Manager): list, upgrades, show, and the commands App Hub runs.

winget has no machine-readable output, so its tables are read by the header's column
positions (counted in screen cells, so wide characters line up).
"""

import re
import shutil
import unicodedata

from . import winsys

COMMON = ["--accept-source-agreements", "--disable-interactivity"]
AGREE = ["--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


def available():
    return bool(exe())


def exe():
    return shutil.which("winget") or shutil.which("winget.exe") or ""


def _cells(ch):
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _cut(line, start, end):
    """Characters of `line` between screen columns start and end (end None = rest)."""
    out, col = [], 0
    for ch in line:
        w = _cells(ch)
        if col >= start and (end is None or col < end):
            out.append(ch)
        col += w
    return "".join(out).strip()


def _clean_lines(text):
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rsplit("\r", 1)[-1]               # progress spinners redraw with \r
        line = re.sub(r"[\b]", "", line)
        lines.append(line.rstrip())
    return lines


def parse_tables(text):
    """Every table in winget output as a list of dicts keyed by the header names.
    Rows of a second table (packages that need explicit targeting) get '_explicit': True."""
    lines = _clean_lines(text)
    rows, explicit = [], False
    i = 0
    while i < len(lines):
        line = lines[i]
        if "explicit targeting" in line.lower():
            explicit = True
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt and set(nxt.strip()) == {"-"} and len(nxt.strip()) > 10 and line.strip():
            header = line.lstrip()
            names = re.findall(r"\S+(?: \S+)*", header)
            starts, col = [], 0
            pos = 0
            for name in names:
                idx = header.index(name, pos)
                starts.append(sum(_cells(c) for c in header[:idx]))
                pos = idx + len(name)
            i += 2
            while i < len(lines) and lines[i].strip() and not set(lines[i].strip()) == {"-"}:
                row = lines[i].lstrip()
                if re.match(r"^\d+ (upgrades?|packages?) ", row) or row.startswith("The following"):
                    break
                cells = {}
                for n, name in enumerate(names):
                    end = starts[n + 1] if n + 1 < len(names) else None
                    cells[name] = _cut(row, starts[n], end)
                cells["_explicit"] = explicit
                rows.append(cells)
                i += 1
            continue
        i += 1
    return rows


def _normal(row):
    return {"name": row.get("Name", ""), "id": row.get("Id", ""), "version": row.get("Version", ""),
            "available": row.get("Available", ""), "source": row.get("Source", ""),
            "explicit": row.get("_explicit", False)}


def list_installed(timeout=120):
    """Every installed package winget can see: [{name, id, version, available, source}].
    `id` is a winget id when the app is known to a source, else ARP\\... or MSIX\\..."""
    if not available():
        return []
    r = winsys.run([exe(), "list", *COMMON], timeout=timeout)
    return [_normal(x) for x in parse_tables(r.stdout)]


def list_upgrades(timeout=180):
    if not available():
        return []
    r = winsys.run([exe(), "upgrade", "--include-unknown", *COMMON], timeout=timeout)
    return [_normal(x) for x in parse_tables(r.stdout)]


def show(package_id, source="winget", timeout=60):
    """Details of a package (Version, Publisher, Homepage, ...) or None when not found."""
    if not available() or not ID_RE.match(package_id or ""):
        return None
    cmd = [exe(), "show", "--id", package_id, "--exact", *COMMON]
    if source:
        cmd += ["--source", source]
    r = winsys.run(cmd, timeout=timeout)
    if r.returncode != 0:
        return None
    info = {}
    m = re.search(r"^Found (.+?) \[(.+?)\]", r.stdout, re.MULTILINE)
    if m:
        info["Name"], info["Id"] = m.group(1).strip(), m.group(2).strip()
    for line in _clean_lines(r.stdout):
        key, sep, value = line.partition(":")
        if sep and key and not key.startswith(" ") and value.strip():
            info.setdefault(key.strip(), value.strip())
    return info if info.get("Id") or info.get("Version") else None


def search(query, timeout=60, exact_name=False):
    if not available():
        return []
    cmd = [exe(), "search", "--name", query, "--exact", "--source", "winget"] if exact_name else [exe(), "search", query]
    r = winsys.run([*cmd, *COMMON], timeout=timeout)
    return [_normal(x) for x in parse_tables(r.stdout)]


# --- commands (run by the helper or directly) ------------------------------

def _source_args(source):
    return ["--source", source] if source in ("winget", "msstore") else []


def install_cmd(package_id, source="winget"):
    return ["winget", "install", "--id", package_id, "--exact", "--silent", *_source_args(source), *AGREE]


def upgrade_cmd(package_id, source=""):
    return ["winget", "upgrade", "--id", package_id, "--exact", "--silent", "--include-unknown",
            *_source_args(source), *AGREE]


def uninstall_cmd(package_id, source=""):
    return ["winget", "uninstall", "--id", package_id, "--exact", "--silent", *_source_args(source),
            "--accept-source-agreements", "--disable-interactivity"]


def upgrade_all_cmd():
    return ["winget", "upgrade", "--all", "--silent", *AGREE]


# winget exit codes worth explaining (as unsigned 32-bit values)
MESSAGES = {
    0x8A15002B: "no newer version found",
    0x8A150011: "the installer hash did not match",
    0x8A15010C: "no applicable installer for this system",
    0x8A150014: "no package found with that id",
    0x8A150101: "the app is running; close it and try again",
    0x8A150102: "another installation is in progress",
    0x8A150109: "a restart is needed to finish",
    0x8A150077: "the package is pinned",
}


def explain(rc):
    return MESSAGES.get(rc & 0xFFFFFFFF, "")
