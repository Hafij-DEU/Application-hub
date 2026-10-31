"""Every installed desktop app on the system, whatever its channel.

Channels: apt (.deb packages), snap, flatpak and AppImage files. Each app
can be updated or uninstalled; root work goes through one helper plan.
"""

import glob
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import threading
from dataclasses import dataclass, field

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

from . import core  # noqa: E402

CHANNELS = {"apt": "Deb", "snap": "Snap", "flatpak": "Flatpak", "appimage": "AppImage"}
HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local/share"), "app-hub")
REMOVED_DIR = os.path.join(DATA_DIR, "removed")
FLATPAK_DIRS = {"system": "/var/lib/flatpak", "user": os.path.join(HOME, ".local/share/flatpak")}
APPIMAGE_DIRS = ["~/Applications", "~/AppImages", "~/.local/bin", "~/Downloads", "~/Desktop",
                 "~/bin", "~/opt", "~/.local/share/AppImage"]
# Removing these breaks the desktop or the package manager.
PROTECTED_PACKAGES = {
    "app-hub", "apt", "dpkg", "sudo", "systemd", "polkitd", "pkexec", "gdm3", "gnome-shell",
    "gnome-session-bin", "ubuntu-session", "network-manager", "snapd", "flatpak", "xwayland",
}


@dataclass
class SysApp:
    channel: str
    id: str                    # package, snap name, flatpak app id, AppImage path
    name: str
    version: str = ""
    source: str = ""           # repository, snap channel, flatpak remote, folder
    size: int = 0
    desktop: str = ""          # .desktop file path
    icon: str = ""             # Gio icon string (theme name or file path)
    update: str = ""           # newer version, if any
    installation: str = ""     # flatpak: system | user
    branch: str = ""           # flatpak branch
    remote: str = ""           # flatpak remote / snap tracking channel
    classic: bool = False      # snap confinement
    protected: str = ""        # why it cannot be removed
    hub_id: str = ""           # App Hub catalog id
    extra: dict = field(default_factory=dict)

    @property
    def key(self):
        return f"{self.channel}:{self.id}"

    @property
    def channel_label(self):
        return CHANNELS.get(self.channel, self.channel)

    @property
    def flatpak_ref(self):
        return f"{self.id}//{self.branch}" if self.branch else self.id

    def subtitle(self):
        parts = [p for p in (self.source, self.version, core.human_size(self.size) if self.size else "") if p]
        if self.update:
            parts.append(f"update {self.update} available")
        return " · ".join(parts)


def _run(cmd, timeout=60):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _desktop(path):
    try:
        return Gio.DesktopAppInfo.new_from_filename(path)
    except TypeError:
        return None


def _icon_string(info):
    icon = info.get_icon() if info else None
    return icon.to_string() if icon else ""


def parse_size(text):
    m = re.match(r"([\d.,]+)\s*([kKMGT]?)B", text.strip())
    if not m:
        return 0
    mult = {"": 1, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}[m.group(2)]
    return int(float(m.group(1).replace(",", ".")) * mult)


# --- apt ------------------------------------------------------------------

def open_apt_cache():
    try:
        import apt
        return apt.Cache()
    except Exception:  # python3-apt missing or cache broken
        return None


_apt_lock = threading.Lock()


def with_apt_cache(fn):
    """fn(cache) with a fresh apt cache. An apt cache holds ~75 MB: only one is open at a time
    (two pages loading wait for each other) and the memory is handed back afterwards."""
    with _apt_lock:
        try:
            return fn(open_apt_cache())
        finally:
            core.release_memory()


def _apt_source(version):
    origins = [o for o in version.origins if o.archive != "now" and o.site]
    if not origins:
        return "Local .deb"
    o = origins[0]
    if "launchpad" in o.site:
        return "PPA " + (o.label or o.origin).removeprefix("PPA for ")
    if o.origin in ("Ubuntu", "Debian"):
        return f"{o.origin} {o.archive}/{o.component}" if o.component else f"{o.origin} {o.archive}"
    return o.label or o.origin or o.site


def list_apt(cache, catalog):
    paths = sorted(glob.glob("/usr/share/applications/*.desktop"))
    owners = {}
    for line in _run(["dpkg-query", "-S", *paths]).stdout.splitlines():
        pkgs, sep, path = line.partition(": ")
        if sep and not pkgs.startswith("diversion"):
            owners[path.strip()] = pkgs.split(",")[0].strip().split(":")[0]
    hub = {pkg: a for a in catalog for pkg in [a.package, *a.replaces]}
    found = {}
    for path in paths:
        pkg = owners.get(path)
        info = _desktop(path)
        if not pkg or not info or info.get_nodisplay() or not info.should_show():
            continue
        stem = os.path.basename(path)[:-8]
        current = found.get(pkg)
        # Prefer the launcher named after the package.
        if current and not (stem == pkg or stem.endswith("." + pkg)):
            continue
        found[pkg] = SysApp("apt", pkg, info.get_name() or pkg, desktop=path, icon=_icon_string(info))
    for pkg, app in found.items():
        if pkg in hub:
            app.hub_id, app.source = hub[pkg].id, "GitHub · App Hub"
        if cache is not None and pkg in cache and cache[pkg].installed:
            p = cache[pkg]
            app.version = p.installed.version
            app.size = p.installed.installed_size
            app.source = app.source or _apt_source(p.installed)
            if p.is_upgradable:
                app.update = p.candidate.version
            if p.essential or p.installed.priority == "required":
                app.protected = "Required by the system"
        else:
            app.version = core.installed_version(pkg) or ""
            app.source = app.source or "Deb package"
        if pkg in PROTECTED_PACKAGES:
            app.protected = "This is App Hub itself" if pkg == "app-hub" else "Needed by the desktop"
    return list(found.values())


def phased_packages():
    """Upgrades apt defers because Ubuntu is still rolling them out (phased updates)."""
    out = _run(["apt-get", "-s", "upgrade"]).stdout.splitlines()
    names, grab = set(), False
    for line in out:
        if "deferred due to phasing" in line:
            grab = True
        elif grab and line.startswith("  "):
            names.update(line.split())
        else:
            grab = False
    return names


def system_upgrades(cache, skip=()):
    """[(package, installed, candidate, phased)] for upgradable apt packages not in `skip`."""
    if cache is None:
        return []
    phased = phased_packages()
    out = []
    for pkg in cache:
        if pkg.is_upgradable and pkg.name not in skip:
            out.append((pkg.name, pkg.installed.version, pkg.candidate.version, pkg.name in phased))
    return sorted(out)


def apt_removal_preview(package, purge=False):
    """Packages apt would remove alongside `package` (simulated, no root)."""
    r = _run(["apt-get", "-s", "purge" if purge else "remove", package])
    return [line.split()[1] for line in r.stdout.splitlines() if line.startswith(("Remv ", "Purg "))]


# --- snap -----------------------------------------------------------------

class _SnapdConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=10)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect("/run/snapd.socket")


def snapd(path):
    """GET from the snapd REST API; returns `result` or None."""
    if not os.path.exists("/run/snapd.socket"):
        return None
    try:
        conn = _SnapdConnection()
        conn.request("GET", path)
        data = json.load(conn.getresponse())
        conn.close()
        return data.get("result")
    except (OSError, ValueError, http.client.HTTPException):
        return None


def list_snap():
    apps = []
    for s in snapd("/v2/snaps") or []:
        desktops = [a["desktop-file"] for a in s.get("apps", []) if a.get("desktop-file")]
        if s.get("type") != "app" or not desktops:
            continue
        main = next((d for d in desktops if os.path.basename(d) == f"{s['name']}_{s['name']}.desktop"),
                    desktops[0])
        info = _desktop(main)
        tracking = s.get("tracking-channel") or s.get("channel") or ""
        publisher = (s.get("publisher") or {}).get("display-name", "")
        apps.append(SysApp(
            "snap", s["name"], (info.get_name() if info else "") or s.get("title") or s["name"],
            version=s.get("version", ""), size=s.get("installed-size", 0), desktop=main,
            icon=_icon_string(info), remote=tracking, classic=s.get("confinement") == "classic",
            source=" · ".join(p for p in (tracking, publisher) if p),
            extra={"revision": str(s.get("revision", ""))}))
    return apps


def snap_updates():
    out = _run(["snap", "refresh", "--list"]).stdout.splitlines()
    return {line.split()[0]: line.split()[1] for line in out[1:] if len(line.split()) > 2}


# --- flatpak --------------------------------------------------------------

def list_flatpak():
    if not shutil.which("flatpak"):
        return []
    r = _run(["flatpak", "list", "--app",
              "--columns=application,name,version,branch,origin,installation,size"])
    apps = []
    for line in r.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) < 7:
            continue
        app_id, name, version, branch, origin, inst, size = cols[:7]
        base = FLATPAK_DIRS.get(inst, FLATPAK_DIRS["system"])
        desktop = os.path.join(base, "exports/share/applications", f"{app_id}.desktop")
        info = _desktop(desktop) if os.path.exists(desktop) else None
        apps.append(SysApp(
            "flatpak", app_id, name or app_id, version=version or branch, size=parse_size(size),
            desktop=desktop if info else "", icon=_icon_string(info) or app_id, branch=branch,
            remote=origin, installation=inst, source=f"{origin} · {inst}"))
    return apps


def flatpak_updates():
    if not shutil.which("flatpak"):
        return {}
    r = _run(["flatpak", "remote-ls", "--updates", "--app", "--columns=application,version,branch"], 90)
    ups = {}
    for line in r.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) >= 3:
            ups[(cols[0], cols[2])] = cols[1] or "new build"
    return ups


# --- AppImage -------------------------------------------------------------

def _appimage_desktops():
    found = {}
    for path in glob.glob(os.path.join(HOME, ".local/share/applications/*.desktop")):
        info = _desktop(path)
        exe = (info.get_string("Exec") or "") if info else ""
        m = re.search(r"(/\S+?\.appimage)", exe, re.IGNORECASE)
        if m:
            found[os.path.realpath(m.group(1).strip("\"'"))] = (path, info)
    return found


def list_appimage():
    desktops = _appimage_desktops()
    paths = set(desktops)
    for folder in APPIMAGE_DIRS:
        for path in glob.glob(os.path.join(os.path.expanduser(folder), "*")):
            if path.lower().endswith(".appimage") and os.path.isfile(path):
                paths.add(os.path.realpath(path))
    apps = []
    for path in sorted(paths):
        if not os.path.isfile(path):
            continue
        desktop, info = desktops.get(path, ("", None))
        stem = os.path.basename(path)[:-9]
        m = re.search(r"[-_ ]v?(\d+(?:\.\d+)+)", stem)
        name = (info.get_name() if info else "") or re.split(r"[-_ ]v?\d", stem)[0] or stem
        apps.append(SysApp(
            "appimage", path, name, version=m.group(1) if m else "", size=os.path.getsize(path),
            desktop=desktop, icon=_icon_string(info) or "application-x-executable",
            source=os.path.dirname(path).replace(HOME, "~", 1)))
    return apps


# --- all ------------------------------------------------------------------

def list_all(cache=None, catalog=None, updates=True):
    catalog = catalog if catalog is not None else core.load_catalog()
    cache = cache if cache is not None else open_apt_cache()
    apps = list_apt(cache, catalog) + list_snap() + list_flatpak() + list_appimage()
    if updates:
        apply_updates(apps, catalog)
    apps.sort(key=lambda a: a.name.lower())
    return apps


def apply_updates(apps, catalog):
    """Fill `update` for snaps, flatpaks and App Hub apps (apt is done from the cache)."""
    snaps = snap_updates() if any(a.channel == "snap" for a in apps) else {}
    flats = flatpak_updates() if any(a.channel == "flatpak" for a in apps) else {}
    used = {x.id for x in apps if x.hub_id}
    hub = {pkg: a for a in catalog for pkg in [a.package, *a.replaces] if pkg in used}
    if hub:
        unique = list({id(a): a for a in hub.values()}.values())
        core.refresh_installed(unique)
        core.check_all(unique)
    for app in apps:
        if app.channel == "snap" and app.id in snaps:
            app.update = snaps[app.id]
        elif app.channel == "flatpak" and (app.id, app.branch) in flats:
            app.update = flats[(app.id, app.branch)]
        elif app.hub_id and app.id in hub:
            h = hub[app.id]
            app.update = h.release.version if h.update_available else ""
            app.extra["hub_app"] = h


# --- actions --------------------------------------------------------------
# Each returns (root_plan, user_commands, local_fn). Run them with execute().

def uninstall_actions(app, purge=False):
    if app.channel == "apt":
        return [["apt-purge" if purge else "apt-remove", app.id]], [], None
    if app.channel == "snap":
        return [["snap-remove", app.id, "", "purge" if purge else ""]], [], None
    if app.channel == "flatpak":
        if app.installation == "user":
            cmd = ["flatpak", "uninstall", "--user", "-y", "--noninteractive"]
            return [], [cmd + (["--delete-data"] if purge else []) + [app.flatpak_ref]], None
        return [["flatpak-uninstall", app.flatpak_ref, "delete-data" if purge else ""]], [], None
    if app.channel == "appimage":
        return [], [], lambda log: _stash_appimage(app, log)
    raise core.HubError(f"cannot uninstall {app.channel} apps")


def update_actions(app):
    if app.channel == "apt":
        return [["apt-upgrade", app.id]], [], None
    if app.channel == "snap":
        return [["snap-refresh", app.id]], [], None
    if app.channel == "flatpak":
        if app.installation == "user":
            return [], [["flatpak", "update", "--user", "-y", "--noninteractive", app.flatpak_ref]], None
        return [["flatpak-update", app.flatpak_ref]], [], None
    raise core.HubError(f"{app.name} cannot be updated here")


def _stash_appimage(app, log):
    """Move the AppImage and its launcher into REMOVED_DIR so it can be restored."""
    folder = os.path.join(REMOVED_DIR, re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(app.id)))
    os.makedirs(folder, exist_ok=True)
    moved = []
    for src in (app.id, app.desktop):
        if src and os.path.exists(src):
            dest = os.path.join(folder, os.path.basename(src))
            shutil.move(src, dest)
            moved.append([dest, src])
            log(f"moved {src} -> {dest}")
    app.extra["stored"] = moved
    return 0


def execute(root_plan, user_cmds, local_fn, log):
    """Run local work, then user commands, then one pkexec plan. Returns (rc, output)."""
    out = []

    def emit(line):
        out.append(line)
        log(line)

    if local_fn:
        try:
            rc = local_fn(emit)
        except (OSError, core.HubError) as e:
            emit(str(e))
            return 1, "\n".join(out)
        if rc:
            return rc, "\n".join(out)
    for cmd in user_cmds:
        emit("+ " + " ".join(cmd))
        rc, _ = core.run_command(cmd, emit)
        if rc:
            return rc, "\n".join(out)
    if root_plan:
        rc, _ = core.run_helper(root_plan, emit)
        return rc, "\n".join(out)
    return 0, "\n".join(out)


def merge_plans(plans):
    """Combine root plans, folding consecutive steps of the same apt verb together."""
    merged = []
    for plan in plans:
        for step in plan:
            if merged and step[0] in ("apt-remove", "apt-purge", "apt-upgrade", "apt-install") \
                    and merged[-1][0] == step[0]:
                merged[-1].extend(step[1:])
            else:
                merged.append(list(step))
    return merged


def combine(actions):
    """Merge several (root_plan, user_cmds, local_fn) triples into one."""
    actions = list(actions)
    root = merge_plans([a[0] for a in actions])
    user = [c for a in actions for c in a[1]]
    locals_ = [a[2] for a in actions if a[2]]
    if not locals_:
        return root, user, None

    def run_all(log):
        for fn in locals_:
            try:
                rc = fn(log)
            except core.HubError as e:
                log(str(e))
                return 1
            if rc:
                return rc
        return 0
    return root, user, run_all
