"""Recently uninstalled apps, and putting them back.

Sources: App Hub's own record (richest: icon, snap channel, stored
AppImage), apt's history.log, `flatpak history` and snapd's change list.
"""

import datetime
import glob
import gzip
import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field

from . import core, inventory

HISTORY = os.path.join(inventory.DATA_DIR, "history.json")
ICON_DIR = os.path.join(inventory.DATA_DIR, "icons")
KEEP_DAYS = 60


@dataclass
class Removed:
    channel: str
    id: str
    name: str
    time: float
    version: str = ""
    source: str = ""
    remote: str = ""          # snap channel or flatpak remote
    branch: str = ""
    installation: str = ""
    classic: bool = False
    icon: str = ""            # saved icon file
    hub_id: str = ""
    stored: list = field(default_factory=list)   # AppImage: [[stored, original], ...]
    reinstall_note: str = ""  # why it cannot be reinstalled (filled by recent())

    @property
    def key(self):
        return f"{self.channel}:{self.id}"

    @property
    def channel_label(self):
        return inventory.CHANNELS.get(self.channel, self.channel)

    def when(self):
        secs = max(0, time.time() - self.time)
        for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
            if secs >= size:
                n = int(secs // size)
                return f"{n} {unit}{'s' if n > 1 else ''} ago"
        return "just now"


def _load():
    try:
        with open(HISTORY, encoding="utf-8") as f:
            return [Removed(**{k: v for k, v in r.items() if k in Removed.__dataclass_fields__})
                    for r in json.load(f)]
    except (OSError, ValueError, TypeError):
        return []


def _save(records):
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    tmp = HISTORY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in records], f, indent=1)
    os.replace(tmp, HISTORY)


def record(app, icon_file=""):
    """Remember an app App Hub just uninstalled (app is an inventory.SysApp)."""
    icon = ""
    if icon_file and os.path.isfile(icon_file):
        os.makedirs(ICON_DIR, exist_ok=True)
        ext = os.path.splitext(icon_file)[1] or ".png"
        icon = os.path.join(ICON_DIR, re.sub(r"[^A-Za-z0-9._-]", "_", app.key) + ext)
        shutil.copyfile(icon_file, icon)
    entry = Removed(
        channel=app.channel, id=app.id, name=app.name, time=time.time(), version=app.version,
        source=app.source, remote=app.remote, branch=app.branch, installation=app.installation,
        classic=app.classic, icon=icon, hub_id=app.hub_id, stored=app.extra.get("stored", []))
    records = [r for r in _load() if r.key != entry.key] + [entry]
    _save(records)


def forget(entry):
    _save([r for r in _load() if r.key != entry.key])


# --- system logs ----------------------------------------------------------

def _apt_log_text():
    texts = []
    for path in sorted(glob.glob("/var/log/apt/history.log*"), reverse=True):
        try:
            opener = gzip.open if path.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8", errors="replace") as f:
                texts.append(f.read())
        except OSError:
            pass
    return "\n".join(texts)


def from_apt_log(since):
    """Packages removed on purpose (named on the command line), newest last."""
    found = []
    for block in _apt_log_text().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.strip().splitlines() if ": " in line)
        removed = fields.get("Remove", "") + ", " + fields.get("Purge", "")
        cmd = fields.get("Commandline", "").split()
        if not removed.strip(", ") or not cmd:
            continue
        try:
            stamp = datetime.datetime.strptime(fields["Start-Date"].strip(), "%Y-%m-%d  %H:%M:%S").timestamp()
        except (KeyError, ValueError):
            continue
        if stamp < since:
            continue
        verb = next((i for i, w in enumerate(cmd) if w in ("remove", "purge")), None)
        if verb is None:
            continue
        named = {w.split(":")[0].rstrip("+-") for w in cmd[verb + 1:] if not w.startswith("-")}
        for m in re.finditer(r"([a-z0-9][a-z0-9+.-]*)(?::\S+)? \(([^)]*)\)", removed):
            if m.group(1) in named:
                found.append(Removed("apt", m.group(1), m.group(1), stamp, version=m.group(2)))
    return found


def from_flatpak(since):
    r = inventory._run(["flatpak", "history", "--columns=time,change,ref,installation,remote"], 30)
    found, year = [], datetime.date.today().year
    remotes = None
    for line in r.stdout.splitlines():
        cols = (line.split("\t") + [""] * 5)[:5]
        if cols[1] != "uninstall" or not cols[2].startswith("app/"):
            continue
        try:
            when = datetime.datetime.strptime(f"{year} {cols[0]}", "%Y %b %d %H:%M:%S")
        except ValueError:
            continue
        if when > datetime.datetime.now():
            when = when.replace(year=year - 1)
        if when.timestamp() < since:
            continue
        _, app_id, _arch, branch = (cols[2].split("/") + ["", "", ""])[:4]
        remote = cols[4]
        if not remote:
            # Old history entries lack the remote; use the only (or flathub) remote.
            if remotes is None:
                remotes = inventory._run(["flatpak", "remotes", "--columns=name"]).stdout.split()
            remote = "flathub" if "flathub" in remotes else (remotes[0] if len(set(remotes)) == 1 else "")
        found.append(Removed("flatpak", app_id, app_id.rsplit(".", 1)[-1], when.timestamp(),
                             source=app_id, remote=remote,
                             branch=branch, installation=cols[3] or "system"))
    return found


def from_snapd(since):
    found = []
    for ch in inventory.snapd("/v2/changes?select=all") or []:
        if ch.get("kind") != "remove-snap" or ch.get("status") != "Done":
            continue
        m = re.match(r'Remove "([^"]+)" snap', ch.get("summary", ""))
        try:
            stamp = datetime.datetime.fromisoformat(ch.get("ready-time", "")).timestamp()
        except ValueError:
            continue
        if m and stamp >= since:
            found.append(Removed("snap", m.group(1), m.group(1), stamp))
    return found


def recent(installed_keys, cache=None, catalog=None, days=KEEP_DAYS):
    """Apps uninstalled in the last `days` days that are not installed again."""
    since = time.time() - days * 86400
    catalog = catalog if catalog is not None else core.load_catalog()
    hub = {pkg: a for a in catalog for pkg in [a.package, *a.replaces]}
    merged = {}
    for entry in from_apt_log(since) + from_flatpak(since) + from_snapd(since):
        if entry.key not in merged or merged[entry.key].time < entry.time:
            merged[entry.key] = entry
    for own in _load():
        if own.time >= since:
            old = merged.get(own.key)
            if old and old.time > own.time + 120:
                own.time = old.time
            merged[own.key] = own
    out = []
    for entry in merged.values():
        if entry.key in installed_keys or (entry.channel == "apt" and core.installed_version(entry.id)):
            continue
        if entry.channel == "apt":
            if entry.id in hub:
                entry.hub_id = hub[entry.id].id
                entry.name = hub[entry.id].name
            elif cache is not None and entry.id in cache:
                cand = cache[entry.id].candidate
                if entry.name == entry.id and cand and cand.summary:
                    entry.source = cand.summary
                if not cand or not [o for o in cand.origins if o.archive != "now" and o.site]:
                    entry.reinstall_note = "Not in any software source; use Install .deb"
            else:
                entry.reinstall_note = "Not in any software source; use Install .deb"
        elif entry.channel == "appimage" and not all(os.path.exists(s) for s, _ in entry.stored):
            entry.reinstall_note = "The stored AppImage was deleted"
        elif entry.channel == "flatpak" and not entry.remote:
            entry.reinstall_note = "Unknown Flatpak remote"
        out.append(entry)
    out.sort(key=lambda e: e.time, reverse=True)
    return out


def reinstall_actions(entry):
    """(root_plan, user_cmds, local_fn) that put the app back. Hub apps are handled by the caller."""
    if entry.channel == "apt":
        return [["apt-install", entry.id]], [], None
    if entry.channel == "snap":
        return [["snap-install", entry.id, entry.remote, "classic" if entry.classic else ""]], [], None
    if entry.channel == "flatpak":
        ref = f"{entry.id}//{entry.branch}" if entry.branch else entry.id
        if entry.installation == "user":
            return [], [["flatpak", "install", "--user", "-y", "--noninteractive", entry.remote, ref]], None
        return [["flatpak-install", entry.remote, ref]], [], None
    if entry.channel == "appimage":
        def restore(log):
            for stored, original in entry.stored:
                os.makedirs(os.path.dirname(original), exist_ok=True)
                shutil.move(stored, original)
                log(f"restored {original}")
            shutil.rmtree(os.path.dirname(entry.stored[0][0]), ignore_errors=True)
            return 0
        return [], [], restore
    raise core.HubError(f"cannot reinstall {entry.channel} apps")
