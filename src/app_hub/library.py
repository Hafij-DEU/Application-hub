"""The Library: apps from any GitHub repository that publishes .deb releases.

Entries look like catalog.json entries (id, name, summary, repo, package,
desktop) plus `icon` (a file) and `owner` (the group they are shown in).
A default set ships in library-default.json; the user's additions and the
defaults they removed are kept in ~/.config/app-hub/library.json.
"""

import gzip
import io
import json
import lzma
import os
import re
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.parse
import urllib.request

from . import core

DEFAULT = os.path.join(core.LIB_DIR, "library-default.json")
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                      "app-hub", "library.json")
ICON_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                        "app-hub", "library-icons")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


# --- storage --------------------------------------------------------------

def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _user():
    data = _read(CONFIG)
    data.setdefault("apps", [])
    data.setdefault("removed", [])
    return data


def _save_user(data):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, CONFIG)


def entries():
    """Library entries: shipped defaults (minus removed ones) then the user's own."""
    user = _user()
    removed = set(user["removed"])
    mine = {e["id"]: e for e in user["apps"]}   # the user's copy wins (e.g. moved to My Apps)
    out, seen = [], set()
    for e in [mine.get(d["id"], d) for d in _read(DEFAULT).get("apps", [])] + user["apps"]:
        if e["id"] in removed or e["id"] in seen:
            continue
        seen.add(e["id"])
        if e.get("icon") and not os.path.isabs(e["icon"]):
            e = dict(e, icon=os.path.join(core.LIB_DIR, "icons", e["icon"]))
        out.append(e)
    return out


def load_apps():
    fields = core.App.__dataclass_fields__
    apps = []
    for e in entries():
        app = core.App(**{k: v for k, v in e.items() if k in fields})
        app.icon = e.get("icon", "")
        app.owner = e.get("owner") or e["repo"].split("/")[0]
        apps.append(app)
    return apps


def add(entry):
    user = _user()
    user["apps"] = [e for e in user["apps"] if e["id"] != entry["id"]] + [entry]
    user["removed"] = [i for i in user["removed"] if i != entry["id"]]
    _save_user(user)


def remove(app_id):
    """Remove an app from My Apps or the Library. A default Library app taken out of
    My Apps goes back to the Library."""
    user = _user()
    moved = any(e["id"] == app_id and e.get("section") == "mine" for e in user["apps"])
    user["apps"] = [e for e in user["apps"] if e["id"] != app_id]
    if not moved and any(e["id"] == app_id for e in _read(DEFAULT).get("apps", [])):
        user["removed"] = sorted(set(user["removed"]) | {app_id})
    _save_user(user)


def known_repos():
    return {e["repo"].lower() for e in entries()} | {a.repo.lower() for a in core.load_catalog(False)}


# --- reading a .deb without downloading it --------------------------------

def _decompress(name, data):
    if name.endswith(".gz"):
        return gzip.decompress(data)
    if name.endswith(".xz"):
        return lzma.decompress(data)
    if name.endswith(".zst"):
        try:
            from compression import zstd  # Python 3.14+
            return zstd.decompress(data)
        except ImportError:
            if shutil.which("zstd"):
                return subprocess.run(["zstd", "-dc"], input=data, capture_output=True, check=True).stdout
            raise core.HubError("zstd is needed to read this package")
    if name.endswith(".tar"):
        return data
    raise core.HubError(f"unknown control archive {name}")


def remote_deb_control(url, limit=512 * 1024):
    """Fields of a .deb's control file, read from its first bytes with an HTTP range request."""
    req = urllib.request.Request(url, headers={"User-Agent": core.USER_AGENT, "Range": f"bytes=0-{limit - 1}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            head = resp.read(limit)
    except (urllib.error.URLError, OSError) as e:
        raise core.HubError(f"could not read the package ({getattr(e, 'reason', e)})") from None
    if not head.startswith(b"!<arch>\n"):
        raise core.HubError("not a .deb package")
    pos = 8
    while pos + 60 <= len(head):
        name = head[pos:pos + 16].decode().strip().rstrip("/")
        size = int(head[pos + 48:pos + 58].decode().strip())
        body = head[pos + 60:pos + 60 + size]
        if name.startswith("control.tar"):
            if len(body) < size:
                raise core.HubError("package header is unusually large")
            with tarfile.open(fileobj=io.BytesIO(_decompress(name, body))) as tar:
                member = next(m for m in tar.getmembers() if m.name.lstrip("./") == "control")
                text = tar.extractfile(member).read().decode("utf-8", "replace")
            fields, key = {}, None
            for line in text.splitlines():
                if line[:1] in " \t" and key:
                    fields[key] += "\n" + line.strip()
                elif ":" in line:
                    key, _, value = line.partition(":")
                    fields[key] = value.strip()
            return fields
        pos += 60 + size + (size % 2)
    raise core.HubError("package has no control data")


# --- GitHub ---------------------------------------------------------------

def parse_github(text):
    """'https://github.com/a/b', 'a/b' -> ('a', 'b'); org/user URL -> ('a', None)."""
    text = text.strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([^/\s?#]+)(?:/([^/\s?#]+))?", text)
    if m:
        owner = m.group(1)
        if owner in ("orgs", "users") and m.group(2):
            return m.group(2), None
        return owner, (m.group(2) or "").removesuffix(".git") or None
    if REPO_RE.match(text):
        owner, repo = text.split("/")
        return owner, repo
    return None, None


def _pretty_name(repo, package, description):
    """'PhotoCraft: ...' description -> PhotoCraft; otherwise the package name, capitalized."""
    head = (description or "").split(":", 1)[0].strip()
    if 2 < len(head) <= 30 and " " not in head and head.lower().replace("-", "") in (
            repo.lower().replace("-", ""), package.lower().replace("-", "")):
        return head
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", package or repo) if w)


def _summary(description, name):
    text = (description or "").strip()
    if text.lower().startswith(name.lower() + ":"):
        text = text[len(name) + 1:].strip()
    text = text[:1].upper() + text[1:]
    return text.split(". ")[0].rstrip(".")[:90]


def find_icon(full_repo, branch="HEAD"):
    """Raw URL of the app icon in a repo (hicolor 128/256 png, else an icon-ish png/svg)."""
    try:
        tree = core._get_json(f"{core.API}/repos/{full_repo}/git/trees/{branch}?recursive=1").get("tree", [])
    except core.HubError:
        return ""
    paths = [t["path"] for t in tree if t.get("type") == "blob"]

    def score(p):
        low = p.lower()
        if re.search(r"hicolor/(128x128|256x256)/apps/[^/]+\.png$", low):
            return 0
        if re.search(r"hicolor/(512x512|96x96|64x64)/apps/[^/]+\.png$", low):
            return 1
        if re.search(r"(app[-_]?icon|logo|icon)[^/]*\.(png|svg)$", low) and not re.search(r"/icons?/[^/]+/", low):
            return 2 + low.count("/")
        return 99
    best = min(paths, key=score, default=None)
    if not best or score(best) >= 99:
        return ""
    return f"https://raw.githubusercontent.com/{full_repo}/{branch}/{urllib.parse.quote(best)}"


def save_icon(app_id, url):
    if not url:
        return ""
    os.makedirs(ICON_DIR, exist_ok=True)
    ext = ".svg" if url.lower().split("?")[0].endswith(".svg") else ".png"
    path = os.path.join(ICON_DIR, re.sub(r"[^A-Za-z0-9._-]", "_", app_id) + ext)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": core.USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp, open(path, "wb") as out:
            out.write(resp.read(4 * 1024 * 1024))
    except (urllib.error.URLError, OSError):
        return ""
    return path


def resolve_repo(full_repo, name="", summary=""):
    """Check a GitHub repo and build a Library entry for it, or raise HubError."""
    if not REPO_RE.match(full_repo):
        raise core.HubError(f"not a GitHub owner/repo: {full_repo}")
    meta = core._get_json(f"{core.API}/repos/{full_repo}")
    owner, repo = meta["full_name"].split("/")
    probe = core.App(id=repo.lower(), name=name or repo, summary="", repo=meta["full_name"], package="")
    rel = core.fetch_release(probe)
    if not rel.asset_url:
        raise core.HubError(f"{meta['full_name']}: latest release ({rel.tag}) has no .deb for this computer")
    control = remote_deb_control(rel.asset_url)
    package = control.get("Package", "")
    if not package:
        raise core.HubError(f"{rel.asset_name} has no package name")
    name = name or _pretty_name(repo, package, meta.get("description"))
    desc_line = (control.get("Description", "").split("\n")[0]).strip()
    summary = summary or _summary(meta.get("description"), name) or desc_line[:1].upper() + desc_line[1:]
    app_id = re.sub(r"[^a-z0-9-]", "-", package.lower())
    return {
        "id": app_id, "name": name, "summary": summary, "repo": meta["full_name"],
        "package": package, "desktop": "", "owner": owner,
        "icon_url": (find_icon(meta["full_name"], meta.get("default_branch") or "HEAD")
                     or f"https://github.com/{owner}.png?size=128"),
        "version": rel.version, "asset": rel.asset_name,
    }


def owner_repos(owner, limit=60):
    """Non-archived, non-fork repos of a user/org that have releases, newest push first."""
    repos = []
    for kind in ("orgs", "users"):
        try:
            repos = core._get_json(f"{core.API}/{kind}/{owner}/repos?per_page=100&sort=pushed")
            break
        except core.HubError:
            continue
    out = []
    for r in repos if isinstance(repos, list) else []:
        if r.get("archived") or r.get("fork"):
            continue
        out.append({"repo": r["full_name"], "description": r.get("description") or ""})
    return out[:limit]


def finish_entry(entry, section=""):
    """Download the icon and keep only the fields the Library stores.
    section "mine" shows the app in My Apps instead of the Library."""
    icon = save_icon(entry["id"], entry.pop("icon_url", ""))
    keep = {k: entry[k] for k in ("id", "name", "summary", "repo", "package", "desktop", "owner")}
    keep["icon"] = icon
    if section:
        keep["section"] = section
    return keep
