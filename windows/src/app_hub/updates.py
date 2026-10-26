"""Update everything at once, with a single UAC prompt.

GitHub-release apps (My Apps and Library) are downloaded first. Apps installed for
this user only update without a prompt; everything installed for all users (and winget
updates of such apps) goes into one elevated plan, so Windows asks only once. A failing
app does not stop the others ("keep-going").
"""

from dataclasses import dataclass

from . import core, helper, installer


@dataclass
class Pending:
    label: str        # shown to the user
    kind: str         # hub | winget
    old: str
    new: str
    obj: object       # core.App or inventory.SysApp


def collect(hub_apps, sys_apps):
    """Pending updates from GitHub-release apps and from winget."""
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
    return out


def winget_step(app, action="upgrade"):
    """(admin_plan, user_plan) for a winget action on an installed app."""
    step = ["winget", action, app.winget_id, app.winget_source, app.name]
    if app.machine and not helper.winsys.is_elevated():
        return [step], []
    return [], [step]


def hub_steps(staged):
    """Plans that install downloaded GitHub releases: [(app, path)] -> (admin_plan, user_plan)."""
    admin, user = [], []
    for app, path in staged:
        if app.section == "self":
            continue          # App Hub replaces itself last (installer.start_self_update)
        kind = installer.detect(path)
        step = installer.install_step(path, kind, app.name, app)
        (admin if installer.needs_admin(kind, app.entry) else user).append(step)
    return admin, user


def run(pending, log, system=False, progress=None, cancel=None, system_rows=()):
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
    a, u = hub_steps(staged)
    admin += a
    user += u
    for p in pending:
        if p.kind == "winget":
            a, u = winget_step(p.obj)
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
