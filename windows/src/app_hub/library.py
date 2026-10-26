"""The Library: apps from any GitHub repository that publishes Windows releases.

Entries look like catalog.json entries (id, name, summary, repo) plus `icon` (a file)
and `owner` (the group they are shown in). A default set ships in
library-default.json; the user's additions and the defaults they removed are kept in
%APPDATA%\\App Hub\\library.json.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from . import core

DEFAULT = os.path.join(core.LIB_DIR, "library-default.json")
CONFIG = os.path.join(core.CONFIG_DIR, "library.json")
ICON_DIR = os.path.join(core.DATA_DIR, "library-icons")
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
    apps = []
    for e in entries():
        app = core._make_app(e)
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


def _pretty_name(repo, description):
    """'PhotoCraft: ...' description -> PhotoCraft; otherwise the repository name, capitalized."""
    head = (description or "").split(":", 1)[0].strip()
    if 2 < len(head) <= 30 and " " not in head and head.lower().replace("-", "") == repo.lower().replace("-", ""):
        return head
    return " ".join(w[:1].upper() + w[1:] for w in re.split(r"[-_]+", repo) if w)


def _summary(description, name):
    text = (description or "").strip()
    if text.lower().startswith(name.lower() + ":"):
        text = text[len(name) + 1:].strip()
    text = text[:1].upper() + text[1:]
    return text.split(". ")[0].rstrip(".")[:90]


def find_icon(full_repo, branch="HEAD"):
    """Raw URL of the app icon in a repo (a 128/256 px png, else an icon-ish png/svg/ico)."""
    try:
        tree = core._get_json(f"{core.API}/repos/{full_repo}/git/trees/{branch}?recursive=1").get("tree", [])
    except core.HubError:
        return ""
    paths = [t["path"] for t in tree if t.get("type") == "blob"]

    def score(p):
        low = p.lower()
        if re.search(r"hicolor/(128x128|256x256)/apps/[^/]+\.png$", low) or re.search(r"(^|/)(256|128)\.png$", low):
            return 0
        if re.search(r"hicolor/(512x512|96x96|64x64)/apps/[^/]+\.png$", low):
            return 1
        if re.search(r"(app[-_]?icon|logo|icon)[^/]*\.(png|svg)$", low) and not re.search(r"/icons?/[^/]+/", low):
            return 2 + low.count("/")
        if re.search(r"(app[-_]?icon|logo|icon)[^/]*\.ico$", low):
            return 8 + low.count("/")
        return 99
    best = min(paths, key=score, default=None)
    if not best or score(best) >= 99:
        return ""
    return f"https://raw.githubusercontent.com/{full_repo}/{branch}/{urllib.parse.quote(best)}"


def save_icon(app_id, url):
    if not url:
        return ""
    os.makedirs(ICON_DIR, exist_ok=True)
    low = url.lower().split("?")[0]
    ext = ".svg" if low.endswith(".svg") else ".ico" if low.endswith(".ico") else ".png"
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
    probe = core.App(id=repo.lower(), name=name or repo, summary="", repo=meta["full_name"])
    rel = core.fetch_release(probe)
    if not rel.asset_url:
        raise core.HubError(f"{meta['full_name']}: latest release ({rel.tag}) has no Windows download")
    name = name or _pretty_name(repo, meta.get("description"))
    summary = summary or _summary(meta.get("description"), name) or "From " + meta["full_name"]
    app_id = re.sub(r"[^a-z0-9-]", "-", repo.lower())
    return {
        "id": app_id, "name": name, "summary": summary, "repo": meta["full_name"], "owner": owner,
        "icon_url": (find_icon(meta["full_name"], meta.get("default_branch") or "HEAD")
                     or f"https://github.com/{owner}.png?size=128"),
        "version": rel.version, "asset": rel.asset_name, "kind": rel.kind,
    }


def owner_repos(owner, limit=60):
    """Non-archived, non-fork repos of a user/org, newest push first."""
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
    keep = {k: entry[k] for k in ("id", "name", "summary", "repo", "owner")}
    keep["icon"] = icon
    if section:
        keep["section"] = section
    return keep
