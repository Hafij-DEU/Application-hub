"""Update everything at once, with a single password prompt.

GitHub-release apps (My Apps and Library) are downloaded first, user-installed
Flatpaks are updated without root, and everything that needs root goes into
one helper plan, so pkexec asks for the password only once. A failing app does
not stop the others ("keep-going").
"""

from dataclasses import dataclass

from . import core, inventory


@dataclass
class Pending:
    label: str        # shown to the user
    kind: str         # hub | apt | snap | flatpak
    old: str
    new: str
    obj: object       # core.App or inventory.SysApp


def collect(hub_apps, sys_apps):
    """Pending updates from GitHub-release apps and from the system inventory."""
    out, seen = [], set()
    for app in hub_apps:
        if app.update_available and app.id not in seen:
            seen.add(app.id)
            out.append(Pending(app.name, "hub", core.clean_version(app.installed or ""),
                               app.release.version, app))
    for app in sys_apps:
        if not app.update or app.hub_id or app.channel not in ("apt", "snap", "flatpak"):
            continue  # App Hub's own apps are covered above
        out.append(Pending(app.name, app.channel, app.version, app.update, app))
    return out


def run(pending, log, system=False, progress=None, cancel=None, phased=False):
    """Apply `pending` (and optionally every other apt upgrade; `phased` also takes
    Ubuntu's staged-rollout updates early). Returns (rc, output)."""
    lines = []

    def emit(line):
        lines.append(line)
        log(line)

    failed = 0
    # 1. Flatpaks in the user's own installation need no password.
    for p in pending:
        app = p.obj
        if p.kind == "flatpak" and app.installation == "user":
            cmd = ["flatpak", "update", "--user", "-y", "--noninteractive", app.flatpak_ref]
            emit("+ " + " ".join(cmd))
            rc, _ = core.run_command(cmd, emit)
            failed += rc != 0

    # 2. Download GitHub releases.
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

    # 3. One root plan for everything else.
    plan = [["keep-going"]]
    if system:
        plan.append(["apt-refresh"])
    for app, path in [(a, p) for a, p in staged if a.extension and not a.system]:
        try:   # personal extensions need no password
            core.install_user_extension(app, path, emit)
            core.finish_install(app, emit)
        except core.HubError as e:
            emit(str(e))
            failed += 1
    staged = [(a, p) for a, p in staged if not a.extension or a.system]
    plan += core.root_steps(staged)
    apt = [p.obj.id for p in pending if p.kind == "apt"]
    if apt and not system:
        plan.append(["apt-upgrade", *apt])
    for p in pending:
        if p.kind == "snap":
            plan.append(["snap-refresh", p.obj.id])
        elif p.kind == "flatpak" and p.obj.installation != "user":
            plan.append(["flatpak-update", p.obj.flatpak_ref])
    if system:
        plan.append(["apt-upgrade-all", "phased"] if phased else ["apt-upgrade-all"])

    rc = 0
    if len(plan) > 1:
        emit(f"Installing {len(plan) - 1} step(s) with one password prompt")
        rc, _ = core.run_helper(plan, emit)
        if rc == 0:
            for app, _ in staged:
                core.finish_install(app, emit)
    if rc in core.AUTH_CANCELLED:
        return rc, "\n".join(lines)
    return (1 if (rc or failed) else 0), "\n".join(lines)
