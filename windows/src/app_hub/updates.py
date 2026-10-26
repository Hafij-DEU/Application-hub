"""Update everything at once, with a single UAC prompt.

GitHub-release apps (My Apps and Library) are downloaded first. Apps installed for
this user only update without a prompt; everything installed for all users (and winget
updates of such apps) goes into one elevated plan, so Windows asks only once. A failing
app does not stop the others ("keep-going").
"""

import json
import os
import time
from dataclasses import dataclass

from . import core, helper, installer

IGNORED = os.path.join(core.DATA_DIR, "ignored-updates.json")


@dataclass
class Pending:
    label: str        # shown to the user
    kind: str         # hub | winget
    old: str
    new: str
    obj: object       # core.App or inventory.SysApp

    @property
    def key(self):
        """Stable name of the app for the ignore list."""
        if self.kind == "hub":
            return f"hub:{self.obj.id}"
        return f"winget:{self.obj.winget_id or self.obj.key}"


# --- ignored updates: "Skip This Version" and "Don't Update This App" ------
# e.g. a patched or licence-locked app that must stay at its version.

def _ignored():
    try:
        with open(IGNORED, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_ignored(data):
    os.makedirs(os.path.dirname(IGNORED), exist_ok=True)
    tmp = IGNORED + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, IGNORED)


def ignore(pending, always):
    """always: never offer updates of this app; else only skip this version."""
    data = _ignored()
    data[pending.key] = {"name": pending.label, "version": "*" if always else pending.new, "time": time.time()}
    _save_ignored(data)


def allow(key):
    data = _ignored()
    data.pop(key, None)
    _save_ignored(data)


def ignored_how(pending):
    """'' (not ignored), 'version' (this version skipped) or 'app' (all updates)."""
    rule = _ignored().get(pending.key)
    if not rule:
        return ""
    if rule.get("version") == "*":
        return "app"
    return "version" if rule.get("version") == pending.new else ""


def collect(hub_apps, sys_apps, with_ignored=False):
    """Pending updates from GitHub-release apps and from winget (without the ignored ones,
    unless with_ignored)."""
    out, seen = [], set()
    for app in hub_apps:
        if app.update_available and app.id not in seen:
            seen.add(app.id)
            out.append(Pending(app.name, "hub", core.clean_version(app.installed or ""),
                               app.release.version, app))
    for app in sys_apps:
        if not app.update or app.hub_id or not app.winget_id:
            continue  # App Hub's own apps are covered above
        out.append(Pending(app.name, "winget", app.version, app.update, app))
    if not with_ignored:
        out = [p for p in out if not ignored_how(p)]
    return out


def ignored(hub_apps, sys_apps):
    return [p for p in collect(hub_apps, sys_apps, with_ignored=True) if ignored_how(p)]


def app_ignored(app):
    """For the Update buttons on cards and rows: is this app's pending update ignored?"""
    if getattr(app, "release", None) is not None and hasattr(app, "update_available"):
        return app.update_available and bool(ignored_how(Pending(app.name, "hub", "", app.release.version, app)))
    if getattr(app, "update", "") and getattr(app, "winget_id", ""):
        return bool(ignored_how(Pending(app.name, "winget", app.version, app.update, app)))
    return False


def row_ignored(row):
    """A winget 'system update' row (runtimes etc.) that is ignored."""
    rule = _ignored().get(f"winget:{row['id']}")
    return bool(rule) and rule.get("version") in ("*", row["available"])


def close_step(name, location):
    """Close the app's running programs first (only after the user agreed, see gui.ensure_closed)."""
    return [["close-apps", location, name]] if location and os.path.isdir(location) else []


def winget_step(app, action="upgrade", close=False):
    """(admin_plan, user_plan) for a winget action on an installed app."""
    if action == "upgrade" and isinstance(app.extra, dict) and app.extra.get("by_name"):
        action = "install"     # winget cannot link the installed copy: install the newest over it
    steps = (close_step(app.name, app.location) if close else []) + \
        [["winget", action, app.winget_id, app.winget_source, app.name]]
    if app.machine and not helper.winsys.is_elevated():
        return steps, []
    return [], steps


def hub_steps(staged, close=False):
    """Plans that install downloaded GitHub releases: [(app, path)] -> (admin_plan, user_plan).
    close: first close the apps if they are running (an installer cannot replace open files)."""
    admin, user = [], []
    for app, path in staged:
        if app.section == "self":
            continue          # App Hub replaces itself last (installer.start_self_update)
        kind = installer.detect(path)
        steps = close_step(app.name, app.entry.location) if close and app.entry else []
        steps.append(installer.install_step(path, kind, app.name, app))
        (admin if installer.needs_admin(kind, app.entry) else user).extend(steps)
    return admin, user


def run(pending, log, system=False, progress=None, cancel=None, system_rows=(), close=False):
    """Apply `pending` (and, with `system`, every other update winget offers).
    Returns (rc, output)."""
    lines = []

    def emit(line):
        lines.append(line)
        log(line)

    failed = 0
    staged = []
    hub = [p.obj for p in pending if p.kind == "hub"]
    for i, app in enumerate(hub, 1):
        if cancel is not None and cancel.is_set():
            emit("cancelled")
            return 1, "\n".join(lines)
        emit(f"Downloading {app.release.asset_name} ({i}/{len(hub)})")
        try:
            staged.append((app, core.download(app, (lambda d, t, a=app: progress(a, d, t)) if progress else None,
                                              cancel)))
        except core.HubError as e:
            emit(str(e))
            failed += 1

    admin, user = [["keep-going"]], [["keep-going"]]
    a, u = hub_steps(staged, close)
    admin += a
    user += u
    for p in pending:
        if p.kind == "winget":
            a, u = winget_step(p.obj, close=close)
            admin += a
            user += u
    if system:
        done = {p.obj.winget_id for p in pending if p.kind == "winget"}
        for row in system_rows:
            if row["id"] not in done:
                admin.append(["winget", "upgrade", row["id"], row["source"], row["name"]])

    steps = len(admin) + len(user) - 2
    if steps:
        emit(f"Installing {steps} update(s)" + (" with one administrator prompt" if len(admin) > 1 else ""))
    rc, _ = helper.execute(admin, user, None, emit)
    if rc in core.AUTH_CANCELLED:
        return rc, "\n".join(lines)
    if rc == 0:
        for app, _ in staged:
            if app.section != "self":
                core.finish_install(app, emit)
    for app, path in staged:
        if app.section == "self":
            installer.start_self_update(path, emit)
    return (1 if (rc or failed) else 0), "\n".join(lines)
