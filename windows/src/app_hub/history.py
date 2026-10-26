"""Recently uninstalled apps, and putting them back.

Windows keeps no log of removed programs, so App Hub remembers what was installed
(a snapshot after every scan) and notices what disappeared since, whatever removed
it. Apps App Hub removed itself are recorded right away, with their icon.
"""

import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass

from . import core, inventory

HISTORY = os.path.join(core.DATA_DIR, "history.json")
SNAPSHOT = os.path.join(core.DATA_DIR, "installed-snapshot.json")
ICON_DIR = os.path.join(core.DATA_DIR, "icons")
KEEP_DAYS = 60


@dataclass
class Removed:
    channel: str
    id: str
    name: str
    time: float
    version: str = ""
    source: str = ""          # publisher
    icon: str = ""            # saved icon file
    hub_id: str = ""
    winget_id: str = ""
    winget_source: str = ""
    family: str = ""          # Store package family name (to open its Store page)
    noticed: bool = False     # found missing at a scan (time is when App Hub noticed)
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


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _load():
    out = []
    for r in _read(HISTORY, []):
        try:
            out.append(Removed(**{k: v for k, v in r.items() if k in Removed.__dataclass_fields__}))
        except TypeError:
            pass
    return out


def _save(records):
    _write(HISTORY, [asdict(r) for r in records])


def _keep_icon(key, icon_file):
    if not icon_file or not os.path.isfile(icon_file):
        return ""
    os.makedirs(ICON_DIR, exist_ok=True)
    ext = os.path.splitext(icon_file)[1] or ".png"
    icon = os.path.join(ICON_DIR, re.sub(r"[^A-Za-z0-9._-]", "_", key)[:120] + ext)
    if os.path.abspath(icon) != os.path.abspath(icon_file):
        try:
            shutil.copyfile(icon_file, icon)
        except OSError:
            return ""
    return icon


def _from_app(app, when, noticed=False):
    family = app.extra.get("family", "") if isinstance(app.extra, dict) else ""
    return Removed(channel=app.channel, id=family if app.channel == "store" and family else app.id,
                   name=app.name, time=when, version=app.version, source=app.source,
                   icon=_keep_icon(app.key, app.icon), hub_id=app.hub_id, winget_id=app.winget_id,
                   winget_source=app.winget_source, family=family, noticed=noticed)


def record(app):
    """Remember an app App Hub just uninstalled (app is an inventory.SysApp)."""
    entry = _from_app(app, time.time())
    _save([r for r in _load() if r.key != entry.key] + [entry])


def forget(entry):
    _save([r for r in _load() if r.key != entry.key])


def update_snapshot(apps):
    """Compare with the last scan: apps that vanished are recorded as uninstalled.
    `apps` must be the complete list (desktop and Store), with winget ids filled when known."""
    old = _read(SNAPSHOT, {})
    now = {a.key: {"channel": a.channel, "id": a.id, "name": a.name, "version": a.version,
                   "source": a.source, "icon": a.icon, "hub_id": a.hub_id, "winget_id": a.winget_id,
                   "winget_source": a.winget_source,
                   "family": a.extra.get("family", "") if isinstance(a.extra, dict) else ""}
           for a in apps}
    # Keep winget ids learned earlier (winget may not have answered this time).
    for key, info in now.items():
        if not info["winget_id"] and old.get(key, {}).get("winget_id"):
            info["winget_id"] = old[key]["winget_id"]
            info["winget_source"] = old[key].get("winget_source", "")
    gone = [info for key, info in old.items() if key not in now and isinstance(info, dict)]
    if gone and old:
        records = _load()
        known = {r.key for r in records}
        stamp = time.time()
        for info in gone:
            app = inventory.SysApp(info.get("channel", "desktop"), info.get("id", ""), info.get("name", ""),
                                   version=info.get("version", ""), source=info.get("source", ""),
                                   icon=info.get("icon", ""), hub_id=info.get("hub_id", ""),
                                   winget_id=info.get("winget_id", ""),
                                   winget_source=info.get("winget_source", ""),
                                   extra={"family": info.get("family", "")})
            if app.key not in known and app.name:
                records.append(_from_app(app, stamp, noticed=True))
        _save(records)
    _write(SNAPSHOT, now)


def recent(installed, catalog=None, days=KEEP_DAYS):
    """Apps uninstalled in the last `days` days that are not installed again.
    `installed`: the current SysApp list."""
    since = time.time() - days * 86400
    keys = {a.key for a in installed}
    names = {inventory.norm(a.name) for a in installed}
    winget_ids = {a.winget_id.lower() for a in installed if a.winget_id}
    catalog = catalog if catalog is not None else core.load_catalog()
    hub = {a.id: a for a in catalog}
    records = [r for r in _load() if r.time >= since]
    out = []
    for entry in records:
        if (entry.key in keys or inventory.norm(entry.name) in names
                or (entry.winget_id and entry.winget_id.lower() in winget_ids)):
            continue
        if entry.hub_id and entry.hub_id in hub:
            entry.name = hub[entry.hub_id].name
        elif not entry.winget_id and entry.channel != "store":
            entry.reinstall_note = "winget does not know this app; find it with AI"
        out.append(entry)
    _save(records)            # drop the old ones
    out.sort(key=lambda e: e.time, reverse=True)
    return out
