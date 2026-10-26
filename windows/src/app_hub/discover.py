"""Finding apps to install, and app lists for a new PC.

- search(): every app in winget and the Microsoft Store, by name (like UniGetUI's Discover page).
- popular(): a hand-picked list of well-known apps by category, installed in one go.
- App lists (".ubundle"): the format of UniGetUI's package bundles (export_version 3), so a
  list saved here opens in UniGetUI and the other way round. App Hub's own GitHub apps go in
  the bundle's "incompatible_packages" (UniGetUI lists them; App Hub installs them).
"""

import json
import os

from . import core, favorites, winget

POPULAR = os.path.join(core.LIB_DIR, "popular.json")
LAST_LIST = os.path.join(core.CONFIG_DIR, "my-apps.ubundle")
APPHUB_SOURCE = "App Hub (GitHub)"


def popular():
    """[(category title, icon name, [Recipe])]."""
    try:
        with open(POPULAR, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for cat in data.get("categories", []):
        recipes = [favorites.Recipe(a.get("source", "winget"), a["id"], a["name"], summary=a.get("summary", ""),
                                    url=a.get("url", ""))
                   for a in cat.get("apps", []) if winget.ID_RE.match(a.get("id", ""))]
        out.append((cat.get("title", ""), cat.get("icon", ""), recipes))
    return out


def search(query, limit=40):
    """Apps in winget and the Microsoft Store matching `query`: [(Recipe, latest version)]."""
    rows = winget.search(query, timeout=90)
    out, seen = [], set()
    for r in rows:
        source = (r["source"] or "winget").lower()
        if source not in ("winget", "msstore") or not winget.ID_RE.match(r["id"]) or r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append((favorites.Recipe(source, r["id"], r["name"].rstrip("…")), r["version"]))
        if len(out) >= limit:
            break
    return out


# --- app lists (UniGetUI bundle format) ------------------------------------

def to_bundle(recipes, versions=None):
    """A UniGetUI-compatible bundle (dict) for winget / Store / App Hub recipes."""
    versions = versions or {}
    packages, others = [], []
    for r in recipes:
        if r.channel in ("winget", "msstore"):
            packages.append({"Id": r.id, "Name": r.name, "Version": versions.get(r.key, ""),
                             "Source": r.channel, "ManagerName": "Winget"})
        elif r.channel in ("hub", "github"):
            others.append({"Id": r.id, "Name": r.name, "Version": versions.get(r.key, ""),
                           "Source": APPHUB_SOURCE if r.channel == "hub" else f"GitHub:{r.id}"})
        elif r.channel == "web":
            others.append({"Id": r.id, "Name": r.name, "Version": versions.get(r.key, ""),
                           "Source": "Install yourself", "Url": r.url})
    return {"export_version": 3, "packages": packages,
            "incompatible_packages_info": "Apps App Hub installs from their GitHub releases "
                                          "(open this list in App Hub to install them).",
            "incompatible_packages": others}


def save(recipes, path, versions=None):
    data = to_bundle(recipes, versions)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return len(data["packages"]) + len(data["incompatible_packages"])


def load(path):
    """Recipes from a UniGetUI bundle or an App Hub list. Returns (recipes, skipped names):
    packages of managers App Hub does not use (Scoop, Chocolatey, pip, …) are skipped."""
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise core.HubError("not an app list")
    catalog = {a.id for a in core.load_catalog()}
    recipes, skipped = [], []
    for p in data.get("packages") or []:
        pid, name = str(p.get("Id", "")), str(p.get("Name") or p.get("Id", ""))
        manager, source = str(p.get("ManagerName", "")).lower(), str(p.get("Source", "")).lower()
        if manager in ("winget", "") and winget.ID_RE.match(pid):
            recipes.append(favorites.Recipe("msstore" if source == "msstore" else "winget", pid, name))
        else:
            skipped.append(f"{name} ({p.get('ManagerName') or 'unknown manager'})")
    for p in data.get("incompatible_packages") or []:
        pid, name, source = str(p.get("Id", "")), str(p.get("Name") or p.get("Id", "")), str(p.get("Source", ""))
        if source == APPHUB_SOURCE and pid in catalog:
            recipes.append(favorites.Recipe("hub", pid, name))
        elif source.startswith("GitHub:") and "/" in pid:
            recipes.append(favorites.Recipe("github", pid, name))
        elif source == "Install yourself" and str(p.get("Url", "")).startswith(("https://", "ms-windows-store://")):
            recipes.append(favorites.Recipe("web", pid, name, url=str(p["Url"])))
        else:
            skipped.append(f"{name} ({source or 'local'})")
    return recipes, skipped
