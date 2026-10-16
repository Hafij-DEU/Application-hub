"""Install recipes and the Favorites list.

A recipe says how to install one app from one channel:
  apt           package from your software sources
  snap          snap name (+ channel, classic)
  flatpak       app id from a remote (system installation)
  hub           an App Hub catalog app
  github        latest .deb of a GitHub repo's releases
  deb-url       a .deb at an https URL
  appimage-url  an AppImage at an https URL (saved to ~/Applications)
"""

import json
import os
import re
import stat
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

from . import core, inventory

FAVORITES = os.path.join(inventory.DATA_DIR, "favorites.json")
APPIMAGE_HOME = os.path.join(inventory.HOME, "Applications")
CHANNEL_LABELS = {
    "apt": "Deb", "snap": "Snap", "flatpak": "Flatpak", "hub": "App Hub",
    "github": "GitHub .deb", "deb-url": ".deb download", "appimage-url": "AppImage",
}


@dataclass
class Recipe:
    channel: str
    id: str                   # package / snap / flatpak id / hub id / owner/repo / slug
    name: str
    summary: str = ""
    remote: str = ""          # snap channel or flatpak remote
    branch: str = ""
    classic: bool = False
    url: str = ""             # deb-url / appimage-url
    package: str = ""         # Debian package name when known (to detect installs)
    asset_pattern: str = ""   # github: regex for the .deb asset
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
        if self.channel == "apt":
            return f"apt install {self.id}"
        if self.channel == "snap":
            return f"snap install {self.id}" + (f" --channel={self.remote}" if self.remote else "") \
                + (" --classic" if self.classic else "")
        if self.channel == "flatpak":
            return f"flatpak install {self.remote or 'flathub'} {self.id}"
        if self.channel == "hub":
            return f"App Hub: latest release of {self.name}"
        if self.channel == "github":
            return f"latest .deb from github.com/{self.id}/releases"
        return self.url

    def installed_key(self):
        """inventory key that means this recipe is installed."""
        if self.channel in ("apt",):
            return f"apt:{self.id}"
        if self.channel in ("hub", "github", "deb-url") and self.package:
            return f"apt:{self.package}"
        if self.channel == "snap":
            return f"snap:{self.id}"
        if self.channel == "flatpak":
            return f"flatpak:{self.id}"
        if self.channel == "appimage-url":
            return f"appimage:{self.appimage_path()}"
        return ""

    def appimage_path(self):
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", self.name).strip("-") or "app"
        return os.path.join(APPIMAGE_HOME, f"{slug}.AppImage")


def from_sysapp(app):
    """Recipe that reinstalls an installed app, or None if it has no download source."""
    if app.hub_id:
        return Recipe("hub", app.hub_id, app.name, package=app.id, icon=app.icon)
    if app.channel == "apt":
        if app.source in ("Local .deb", "Deb package"):
            return None
        return Recipe("apt", app.id, app.name, package=app.id, icon=app.icon)
    if app.channel == "snap":
        return Recipe("snap", app.id, app.name, remote=app.remote, classic=app.classic, icon=app.icon)
    if app.channel == "flatpak":
        return Recipe("flatpak", app.id, app.name, remote=app.remote, branch=app.branch, icon=app.icon)
    return None


def from_removed(entry):
    if entry.hub_id:
        return Recipe("hub", entry.hub_id, entry.name, package=entry.id, icon=entry.icon)
    if entry.channel in ("apt", "snap", "flatpak") and not entry.reinstall_note:
        return Recipe(entry.channel, entry.id, entry.name, remote=entry.remote, branch=entry.branch,
                      classic=entry.classic, package=entry.id if entry.channel == "apt" else "",
                      icon=entry.icon)
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

def _check_https(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise core.HubError(f"only https downloads are allowed: {url}")
    return parts


def _fetch(url, dest, log, cancel=None):
    _check_https(url)
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


def install_actions(recipe, catalog=None):
    """(root_plan, user_cmds, local_fn) that install the recipe."""
    ch = recipe.channel
    if ch == "apt":
        return [["apt-install", recipe.id]], [], None
    if ch == "snap":
        return [["snap-install", recipe.id, recipe.remote, "classic" if recipe.classic else ""]], [], None
    if ch == "flatpak":
        ref = f"{recipe.id}//{recipe.branch}" if recipe.branch else recipe.id
        return [["flatpak-install", recipe.remote or "flathub", ref]], [], None

    if ch in ("hub", "github"):
        if ch == "hub":
            apps = catalog if catalog is not None else core.load_catalog()
            app = next((a for a in apps if a.id == recipe.id), None)
            if not app:
                raise core.HubError(f"{recipe.name} is not in the App Hub catalog")
        else:
            app = core.App(id=re.sub(r"[^A-Za-z0-9._-]", "_", recipe.id), name=recipe.name, summary="",
                           repo=recipe.id, package=recipe.package, asset_pattern=recipe.asset_pattern)
        path_box = {}

        def fetch(log):
            app.release = core.fetch_release(app)
            if not app.release.asset_url:
                raise core.HubError(f"{recipe.name}: latest release has no .deb for this system")
            log(f"downloading {app.release.asset_name}")
            path_box["path"] = core.download(app)
            os.replace(path_box["path"], target)
            return 0
        target = os.path.join(core.CACHE_DIR, "favorites", f"{app.id}.deb")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        return [["install-deb", app.package if ch == "hub" else "", target]], [], fetch

    if ch == "deb-url":
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", recipe.name) or "download"
        target = os.path.join(core.CACHE_DIR, "favorites", f"{slug}.deb")

        def fetch_deb(log):
            _fetch(recipe.url, target, log)
            info = core._run(["dpkg-deb", "-f", target, "Package", "Version"]).stdout.strip()
            if not info:
                raise core.HubError("the download is not a .deb package")
            log(info.replace("\n", ", "))
            return 0
        return [["install-deb", "", target]], [], fetch_deb

    if ch == "appimage-url":
        dest = recipe.appimage_path()

        def fetch_appimage(log):
            _fetch(recipe.url, dest, log)
            with open(dest, "rb") as f:
                head = f.read(4)
            if head != b"\x7fELF":
                core._unlink(dest)
                raise core.HubError("the download is not an AppImage (not an ELF executable)")
            os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            desktop = os.path.join(inventory.HOME, ".local/share/applications",
                                   f"appimage-{os.path.basename(dest)[:-9].lower()}.desktop")
            os.makedirs(os.path.dirname(desktop), exist_ok=True)
            with open(desktop, "w", encoding="utf-8") as f:
                f.write("[Desktop Entry]\nType=Application\n"
                        f"Name={recipe.name}\nComment={recipe.summary}\n"
                        f'Exec="{dest}" %U\nIcon=application-x-executable\nTerminal=false\n'
                        "Categories=Utility;\n")
            log(f"installed {dest}")
            return 0
        return [], [], fetch_appimage

    raise core.HubError(f"unknown install channel: {ch}")


def run_install(recipe, log, catalog=None):
    root, user, local = install_actions(recipe, catalog)

    def guarded(emit):
        try:
            return local(emit)
        except core.HubError as e:
            emit(str(e))
            return 1
    return inventory.execute(root, user, guarded if local else None, log)
