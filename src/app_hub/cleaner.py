"""Find and remove what uninstalled apps leave behind.

User files go to the Trash (recoverable). System leftovers go through one
helper plan: purge removed packages' config, autoremove, apt clean, old
snap revisions and unused Flatpak runtimes.
"""

import glob
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from . import core, history, inventory  # noqa: E402

HOME = inventory.HOME
DATA_ROOTS = [os.path.join(HOME, d) for d in (".config", ".local/share", ".cache", ".local/state")]
# Shared folders that are never one app's leftovers, even if a package had the same name.
NEVER = {
    "applications", "autostart", "app-hub", "dconf", "desktop-directories", "fonts", "flatpak",
    "gnome-shell", "gtk-2.0", "gtk-3.0", "gtk-4.0", "gvfs-metadata", "icons", "keyrings", "mime",
    "nautilus", "pulse", "recently-used.xbel", "sounds", "systemd", "themes", "trash", "user-dirs.dirs",
    "ibus", "evolution", "goa-1.0", "tracker3", "ssh", "gnupg", "local", "config", "cache", "snap",
    "var", "mozilla", "pki", "nv", "fontconfig", "menus", "xdg-desktop-portal", "dbus", "bash",
    "python", "pip", "npm", "cargo", "rustup", "java", "android", "gradle", "vim", "git",
}


@dataclass
class Item:
    label: str
    detail: str = ""
    size: int = 0
    selected: bool = True
    path: str = ""           # trash / delete target
    step: list = None        # root plan step for this item


@dataclass
class Group:
    id: str
    title: str
    description: str
    items: list = field(default_factory=list)
    granular: bool = True    # items can be picked one by one
    action: str = ""         # trash | delete | root
    root_plan: list = None   # whole-group root plan when not granular
    user_cmds: list = None   # whole-group user commands
    note: str = ""

    @property
    def size(self):
        return sum(i.size for i in self.items)


def dir_size(path):
    if os.path.isfile(path) or os.path.islink(path):
        try:
            return os.lstat(path).st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _norm(name):
    return name.lower().lstrip(".")


def _keys_for(app_id, name=""):
    keys = {_norm(app_id)}
    if "." in app_id:  # reverse-DNS ids: org.gnome.Foo -> foo
        keys.add(_norm(app_id.rsplit(".", 1)[-1]))
    if name and name != app_id:
        keys.add(_norm(name))
        keys.add(_norm(name.replace(" ", "")))
    return {k for k in keys if len(k) >= 3}


def _tokens(text):
    """Every word of an id or name: io.github.draco.DracoDM -> io, github, draco, dracodm."""
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 3}


def installed_keys(apps):
    """Names that may belong to something still installed; never offered for cleaning."""
    keys = set()
    for line in inventory._run(["dpkg-query", "-W", "-f=${db:Status-Abbrev} ${Package}\n"]).stdout.splitlines():
        if line.startswith("ii"):
            keys.add(line.split()[1])
    for a in apps:
        keys |= _keys_for(a.id if a.channel != "appimage" else a.name, a.name)
        keys |= _tokens(a.name) | _tokens(a.id if a.channel != "appimage" else "")
        if a.desktop:
            stem = os.path.basename(a.desktop)[:-8]
            keys |= {_norm(stem)} | _tokens(stem)
    return keys


def newest_mtime(path):
    newest = 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        for name in files + dirs:
            try:
                newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
            except OSError:
                pass
    try:
        return max(newest, os.lstat(path).st_mtime)
    except OSError:
        return newest


def residual_packages():
    out = inventory._run(["dpkg-query", "-W", "-f=${db:Status-Abbrev} ${Package}\n"]).stdout
    return [line.split()[1] for line in out.splitlines() if line.startswith("rc")]


def scan_leftovers(apps, removed):
    """User folders that belong to apps no longer installed."""
    keep = installed_keys(apps)
    removed_at = {}  # key -> when the app was removed (0 = unknown)
    for r in removed:
        for k in _keys_for(r.id, r.name):
            removed_at[k] = max(removed_at.get(k, 0), r.time)
    for pkg in residual_packages():
        removed_at.setdefault(pkg, 0)
    for app in core.load_catalog():
        for k in _keys_for(app.package, app.name):
            removed_at.setdefault(k, 0)
    wanted = set(removed_at) - keep - NEVER

    def still_used(path, key):
        # Written to after the app was removed (or within a week, when we do not
        # know when): something installed still uses it.
        since = removed_at[key] + 3600 if removed_at[key] else time.time() - 7 * 86400
        return newest_mtime(path) > since

    items = []
    candidates = [p for root in DATA_ROOTS for p in glob.glob(os.path.join(root, "*"))]
    candidates += [p for p in glob.glob(os.path.join(HOME, ".*")) if os.path.isdir(p)]
    for path in candidates:
        key = _norm(os.path.basename(path))
        if key in wanted and not still_used(path, key):
            items.append(path)

    flatpaks = {a.id for a in apps if a.channel == "flatpak"}
    for path in glob.glob(os.path.join(HOME, ".var/app/*")):
        if os.path.basename(path) not in flatpaks:
            items.append(path)
    snaps = {s["name"] for s in inventory.snapd("/v2/snaps") or []}
    for path in glob.glob(os.path.join(HOME, "snap/*")):
        if os.path.basename(path) not in snaps:
            items.append(path)

    return [Item(os.path.basename(p), p.replace(HOME, "~", 1), dir_size(p), path=p)
            for p in sorted(set(items))]


def scan_autoremove(cache):
    r = inventory._run(["apt-get", "-s", "autoremove"])
    names = [line.split()[1] for line in r.stdout.splitlines() if line.startswith("Remv ")]
    items = []
    for n in names:
        size = cache[n].installed.installed_size if cache is not None and n in cache and cache[n].installed else 0
        items.append(Item(n, "", size))
    return items


def scan_snap_revisions():
    items = []
    for s in inventory.snapd("/v2/snaps?select=all") or []:
        if s.get("status") == "active":
            continue
        name, rev = s["name"], str(s.get("revision", ""))
        path = f"/var/lib/snapd/snaps/{name}_{rev}.snap"
        size = os.path.getsize(path) if os.path.exists(path) else s.get("installed-size", 0)
        items.append(Item(f"{name} {s.get('version', '')}", f"revision {rev}", size,
                          step=["snap-remove", name, rev, ""]))
    return items


def scan_flatpak_unused():
    """Ask flatpak what --unused would remove, answering "n" so nothing happens."""
    if not shutil.which("flatpak"):
        return [], []
    found, scopes = [], []
    for scope in ("--system", "--user"):
        try:
            r = subprocess.run(["flatpak", "uninstall", scope, "--unused"], input="n\n",
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        refs = re.findall(r"^\s*\d+\.\s+(?:\[\S+\]\s+)?(\S+)\s+(\S+)", r.stdout, re.MULTILINE)
        if refs:
            scopes.append(scope)
            found += [Item(ref, f"{branch} · {scope[2:]}") for ref, branch in refs]
    return found, scopes


def scan_apt_archives():
    files = glob.glob("/var/cache/apt/archives/*.deb")
    return [Item(os.path.basename(f), "", os.path.getsize(f)) for f in files]


def scan(apps=None, cache=None, removed=None):
    cache = cache if cache is not None else inventory.open_apt_cache()
    apps = apps if apps is not None else inventory.list_all(cache, updates=False)
    removed = removed if removed is not None else history.recent({a.key for a in apps}, cache, days=3650)
    groups = []

    g = Group("leftovers", "Files left by removed apps",
              "Settings, data and caches of apps that are no longer installed. Moved to the Trash.",
              scan_leftovers(apps, removed), action="trash")
    groups.append(g)

    rc = residual_packages()
    groups.append(Group(
        "residual", "Leftover system settings",
        "Configuration files in /etc kept after removing these packages (dpkg “rc” state).",
        [Item(p, "", 0, step=["apt-purge", p]) for p in rc], action="root"))

    auto = scan_autoremove(cache)
    groups.append(Group(
        "autoremove", "Unused dependencies",
        "Packages installed only as dependencies of apps that are gone (apt autoremove).",
        auto, granular=False, action="root", root_plan=[["apt-autoremove"]]))

    groups.append(Group(
        "snaps", "Old Snap versions",
        "Disabled revisions kept after snap updates.",
        scan_snap_revisions(), action="root"))

    unused, scopes = scan_flatpak_unused()
    groups.append(Group(
        "flatpak", "Unused Flatpak runtimes",
        "Runtimes and extensions no installed Flatpak app needs.",
        unused, granular=False, action="root",
        root_plan=[["flatpak-unused"]] if "--system" in scopes else [],
        user_cmds=[["flatpak", "uninstall", "--user", "--unused", "-y", "--noninteractive"]]
        if "--user" in scopes else []))

    archives = scan_apt_archives()
    groups.append(Group(
        "archives", "Downloaded package files",
        "Old .deb files in /var/cache/apt/archives. apt downloads them again if needed.",
        archives, granular=False, action="root", root_plan=[["apt-clean"]]))

    hub_items = []
    if os.path.isdir(core.CACHE_DIR):
        hub_items.append(Item("App Hub downloads", core.CACHE_DIR.replace(HOME, "~", 1),
                              dir_size(core.CACHE_DIR), path=core.CACHE_DIR))
    for path in sorted(glob.glob(os.path.join(inventory.REMOVED_DIR, "*"))):
        hub_items.append(Item(f"Removed AppImage: {os.path.basename(path)}",
                              "kept so it can be reinstalled", dir_size(path), selected=False, path=path))
    groups.append(Group("hub", "App Hub storage",
                        "Download cache and AppImages kept for reinstalling. Deleted permanently.",
                        hub_items, action="delete"))

    for g in groups:
        if g.items and not g.granular:
            for i in g.items:
                i.selected = True
    return groups


def selected_size(groups, enabled):
    total = 0
    for g in groups:
        if g.granular:
            total += sum(i.size for i in g.items if i.selected)
        elif enabled.get(g.id):
            total += g.size
    return total


def clean_actions(groups, enabled):
    """(root_plan, user_cmds, local_fn) for the selected items."""
    root, user, trash, delete = [], [], [], []
    for g in groups:
        if g.granular:
            picked = [i for i in g.items if i.selected]
            if g.action == "trash":
                trash += [i.path for i in picked]
            elif g.action == "delete":
                delete += [i.path for i in picked]
            else:
                root.append([s for i in picked for s in [i.step]])
        elif enabled.get(g.id) and g.items:
            root.append(g.root_plan or [])
            user += g.user_cmds or []
    plan = inventory.merge_plans(root)

    def local(log):
        failed = 0
        for path in trash:
            try:
                Gio.File.new_for_path(path).trash(None)
                log(f"moved to Trash: {path}")
            except GLib.Error as e:
                failed += 1
                log(f"could not trash {path}: {e.message}")
        for path in delete:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.unlink(path)
            log(f"deleted {path}")
            history_forget_stored(path)
        return 1 if failed and not (plan or user) else 0
    return plan, user, (local if trash or delete else None)


def history_forget_stored(path):
    """Drop history entries whose stored AppImage lived under `path`."""
    for r in history._load():
        if r.stored and any(s.startswith(path) for s, _ in r.stored):
            history.forget(r)
