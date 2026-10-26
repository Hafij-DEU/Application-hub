"""Install recipes and the Favorites list.

A recipe says how to install one app from one channel:
  winget    a winget package id (community repository)
  msstore   a Microsoft Store product id, through winget
  hub       an App Hub catalog / Library app
  github    the latest Windows download of a GitHub repository's releases
  url       an installer (.exe, .msi) or portable .zip at an https URL
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

from . import core, installer, winsys

FAVORITES = os.path.join(core.DATA_DIR, "favorites.json")
CHANNEL_LABELS = {"winget": "winget", "msstore": "Store", "hub": "App Hub", "github": "GitHub",
                  "url": "Download"}


@dataclass
class Recipe:
    channel: str
    id: str                   # winget id / Store id / hub id / owner/repo / slug
    name: str
    summary: str = ""
    url: str = ""             # url channel
    asset_pattern: str = ""   # github: regex for the download
    icon: str = ""
    added: float = 0

    @property
    def key(self):
        return f"{self.channel}:{self.id}"

    @property
    def channel_label(self):
        return CHANNEL_LABELS.get(self.channel, self.channel)

    def describe(self):
        """The exact thing that will be installed, for confirmation dialogs."""
        if self.channel == "winget":
            return f"winget install --id {self.id}"
        if self.channel == "msstore":
            return f"Microsoft Store: {self.id}"
        if self.channel == "hub":
            return f"App Hub: latest release of {self.name}"
        if self.channel == "github":
            return f"latest Windows download from github.com/{self.id}/releases"
        return self.url


def from_sysapp(app):
    """Recipe that reinstalls an installed app, or None if it has no download source."""
    if app.hub_id:
        return Recipe("hub", app.hub_id, app.name, icon=app.icon)
    if app.winget_id:
        return Recipe("msstore" if app.winget_source == "msstore" else "winget", app.winget_id, app.name,
                      icon=app.icon)
    return None


def from_removed(entry):
    if entry.hub_id:
        return Recipe("hub", entry.hub_id, entry.name, icon=entry.icon)
    if entry.winget_id:
        return Recipe("msstore" if entry.winget_source == "msstore" else "winget", entry.winget_id,
                      entry.name, icon=entry.icon)
    return None


# --- storage --------------------------------------------------------------

def load():
    try:
        with open(FAVORITES, encoding="utf-8") as f:
            return [Recipe(**{k: v for k, v in r.items() if k in Recipe.__dataclass_fields__})
                    for r in json.load(f)]
    except (OSError, ValueError, TypeError):
        return []


def save(recipes):
    os.makedirs(os.path.dirname(FAVORITES), exist_ok=True)
    tmp = FAVORITES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in recipes], f, indent=1)
    os.replace(tmp, FAVORITES)


def add(recipe):
    favs = [r for r in load() if r.key != recipe.key]
    recipe.added = recipe.added or time.time()
    save(favs + [recipe])


def remove(recipe):
    save([r for r in load() if r.key != recipe.key])


def is_favorite(key):
    return any(r.key == key for r in load())


# --- installing -----------------------------------------------------------

def check_https(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise core.HubError(f"only https downloads are allowed: {url}")
    return parts


def fetch(url, dest, log, cancel=None):
    check_https(url)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    log(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": core.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done, last = 0, 0
            while chunk := resp.read(256 * 1024):
                if cancel and cancel.is_set():
                    raise core.Cancelled("download cancelled")
                out.write(chunk)
                done += len(chunk)
                if total and done - last > total // 10:
                    last = done
                    log(f"  {done * 100 // total}% of {core.human_size(total)}")
    except (OSError, ValueError) as e:
        core._unlink(part)
        raise core.HubError(f"download failed: {getattr(e, 'reason', e)}") from None
    os.replace(part, dest)


def _place(path, label, app, admin, user, log):
    kind = installer.detect(path)
    if not kind:
        raise core.HubError(f"{os.path.basename(path)} is not an installer, .msi or .zip")
    log(f"{label}: {installer.KINDS.get(kind, kind)}")
    step = installer.install_step(path, kind, label, app)
    (admin if installer.needs_admin(kind, getattr(app, "entry", None)) else user).append(step)


def install_actions(recipe, catalog=None):
    """(admin_plan, user_plan, local) that install the recipe. local(log, admin, user)
    downloads first and adds its install step to the plans."""
    ch = recipe.channel
    if ch in ("winget", "msstore"):
        step = ["winget", "install", recipe.id, ch, recipe.name]
        if ch == "winget" and winsys.elevates_as_self() and not winsys.is_elevated():
            return [step], [], None
        return [], [step], None

    if ch in ("hub", "github"):
        if ch == "hub":
            apps = catalog if catalog is not None else core.load_catalog()
            app = next((a for a in apps if a.id == recipe.id), None)
            if not app:
                raise core.HubError(f"{recipe.name} is not in the App Hub catalog")
            core.refresh_installed([app])
        else:
            app = core.App(id=re.sub(r"[^A-Za-z0-9._-]", "_", recipe.id), name=recipe.name, summary="",
                           repo=recipe.id, asset_pattern=recipe.asset_pattern)

        def fetch_hub(log, admin, user):
            app.release = core.fetch_release(app)
            if not app.release.asset_url:
                raise core.HubError(f"{recipe.name}: latest release has no Windows download")
            log(f"downloading {app.release.asset_name}")
            _place(core.download(app), app.name, app, admin, user, log)
            return 0
        return [], [], fetch_hub

    if ch == "url":
        name = os.path.basename(urllib.parse.urlsplit(recipe.url).path) or "download.exe"
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", recipe.id or recipe.name) or "download"
        target = os.path.join(core.CACHE_DIR, "favorites", slug, name)
        app = core.App(id=slug, name=recipe.name, summary=recipe.summary, repo="")

        def fetch_url(log, admin, user):
            fetch(recipe.url, target, log)
            _place(target, recipe.name, app, admin, user, log)
            return 0
        return [], [], fetch_url

    raise core.HubError(f"unknown install channel: {ch}")
