"""Command-line front end. With no subcommand it opens the GUI."""

import argparse
import os
import sys

from . import __version__, core


def _bar(done, total, label):
    if not sys.stderr.isatty():
        return
    if total:
        pct = done * 100 // total
        fill = pct * 30 // 100
        line = f"\r  {label} [{'#' * fill}{'.' * (30 - fill)}] {pct:3d}% {core.human_size(done)}"
    else:
        line = f"\r  {label} {core.human_size(done)}"
    sys.stderr.write(line)
    sys.stderr.flush()


def _load(check=True):
    apps = core.load_catalog()
    core.refresh_installed(apps)
    if check:
        core.check_all(apps)
    return apps


def cmd_list(args):
    apps = _load()
    rows = [("APP", "INSTALLED", "LATEST", "STATUS")]
    for a in apps:
        latest = a.release.version if a.release else "-"
        if a.error:
            status = a.error
        elif a.update_available:
            status = "update available"
        elif a.installed:
            status = "up to date"
        elif a.installable:
            status = "not installed"
        else:
            status = "no .deb for this system"
        rows.append((a.id, core.clean_version(a.installed) if a.installed else "-", latest, status))
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)) + "  " + r[3])
    return 0


def _install(apps):
    staged = []
    for app in apps:
        if not app.installable:
            print(f"{app.name}: {app.error or 'no .deb for this system'}", file=sys.stderr)
            continue
        print(f"Downloading {app.release.asset_name}")
        path = core.download(app, lambda d, t, n=app.id: _bar(d, t, n))
        if sys.stderr.isatty():
            sys.stderr.write("\n")
        staged.append((app, path))
    if not staged:
        return 1
    rc, _ = core.run_helper(core.install_plan([(a.package, p) for a, p in staged]), print)
    if rc in core.AUTH_CANCELLED:
        print("Authentication cancelled.", file=sys.stderr)
        return rc
    if rc == 0:
        for app, _ in staged:
            core.clear_cache(app)
    return rc


def cmd_install(args):
    apps = core.find_apps(_load(), args.apps)
    todo = [a for a in apps if args.reinstall or not a.installed]
    for a in apps:
        if a not in todo:
            print(f"{a.name} is already installed ({a.installed}). Use 'app-hub update' or --reinstall.")
    return _install(todo) if todo else 0


def cmd_update(args):
    apps = _load()
    if args.apps:
        apps = core.find_apps(apps, args.apps)
    todo = [a for a in apps if a.update_available]
    if not todo:
        print("Everything is up to date.")
        return 0
    for a in todo:
        print(f"{a.name}: {core.clean_version(a.installed)} -> {a.release.version}")
    return _install(todo)


def cmd_remove(args):
    apps = core.find_apps(_load(check=False), args.apps)
    todo = [a for a in apps if a.installed]
    for a in apps:
        if a not in todo:
            print(f"{a.name} is not installed.")
    if not todo:
        return 0
    rc, _ = core.run_helper([["apt-remove", *[a.installed_as or a.package for a in todo]]], print)
    if rc in core.AUTH_CANCELLED:
        print("Authentication cancelled.", file=sys.stderr)
    return rc


def cmd_open(args):
    for app in core.find_apps(_load(check=False), args.apps):
        core.launch(app)
    return 0


def _table(rows):
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)) + "  " + r[-1])


def cmd_apps(args):
    from . import inventory
    apps = inventory.list_all(updates=not args.no_updates)
    if args.channel:
        apps = [a for a in apps if a.channel == args.channel]
    rows = [("CHANNEL", "ID", "VERSION", "SOURCE / UPDATE")]
    for a in apps:
        rows.append((a.channel, a.id, a.version or "-",
                     a.source + (f"  -> {a.update} available" if a.update else "")))
    _table(rows)
    return 0


def cmd_recent(args):
    from . import history, inventory
    cache = inventory.open_apt_cache()
    apps = inventory.list_all(cache, updates=False)
    rows = [("CHANNEL", "ID", "REMOVED", "NOTE")]
    for e in history.recent({a.key for a in apps}, cache):
        rows.append((e.channel, e.id, e.when(), e.reinstall_note or "can reinstall"))
    _table(rows)
    return 0


def cmd_install_deb(args):
    path = os.path.abspath(args.file)
    if not os.path.isfile(path):
        raise core.HubError(f"no such file: {args.file}")
    rc, _ = core.run_helper([["install-deb", "", path]], print)
    return rc


def cmd_ai(args):
    from . import ai
    answer = ai.find(" ".join(args.query), lambda s: print(f"  {s}", file=sys.stderr))
    for found in answer.apps:
        print(f"\n{found.name} — {found.summary}")
        for opt in found.options:
            mark = "ok " if opt.verified else "no "
            print(f"  [{mark}] {opt.recipe.channel_label:12} {opt.recipe.describe()}  ({opt.status})")
    if answer.sources:
        print("\nSources:")
        for title, uri in answer.sources[:8]:
            print(f"  {title}: {uri}")
    return 0


def cmd_favorites(args):
    from . import favorites
    rows = [("CHANNEL", "NAME", "INSTALL")]
    for r in favorites.load():
        rows.append((r.channel_label, r.name, r.describe()))
    _table(rows)
    return 0


def cmd_update_all(args):
    from . import inventory, updates
    hub = _load()
    print("Checking deb, snap and flatpak updates…", file=sys.stderr)
    system = inventory.list_all()
    pending = updates.collect(hub, system)
    include_system = not args.apps_only
    sysup = []
    if include_system:
        skip = {a.id for a in system if a.channel == "apt"} | {a.package for a in hub}
        sysup = inventory.system_upgrades(inventory.open_apt_cache(), skip)
    if not pending and not sysup:
        print("Everything is up to date." if args.apps_only else
              "No updates known; refreshing the package lists to check again.")
        if args.apps_only:
            return 0
    for p in pending:
        print(f"  {p.label}: {p.old} -> {p.new} ({p.kind})")
    for name, old, new, phased in sysup:
        if phased and not args.include_phased:
            print(f"  {name}: {old} -> {new} (staged rollout, skipped; --include-phased installs it)")
        else:
            print(f"  {name}: {old} -> {new} (system)")
    if not args.yes:
        try:
            if input("Update all? You enter your password once. [Y/n] ").strip().lower() not in ("", "y", "yes"):
                return 1
        except EOFError:
            return 1
    rc, _ = updates.run(pending, print, system=include_system, phased=args.include_phased,
                        progress=lambda app, d, t: _bar(d, t, app.id))
    if sys.stderr.isatty():
        sys.stderr.write("\n")
    if rc in core.AUTH_CANCELLED:
        print("Authentication cancelled.", file=sys.stderr)
    return rc


def cmd_add(args):
    from . import library
    owner, repo = library.parse_github(args.repo)
    if not (owner and repo):
        raise core.HubError("give a GitHub repository: owner/repo or https://github.com/owner/repo")
    entry = library.resolve_repo(f"{owner}/{repo}")
    library.add(library.finish_entry(entry))
    print(f"Added {entry['name']} ({entry['repo']}, package {entry['package']}) to the Library.")
    print(f"Install it with: app-hub install {entry['id']}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="app-hub",
        description="Install, update and remove apps (your GitHub apps, deb, snap, flatpak, AppImage). "
                    "Run without a command to open the window.")
    parser.add_argument("--version", action="version", version=f"app-hub {__version__}")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("list", aliases=["check"], help="show installed and latest versions")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("install", help="install apps")
    p.add_argument("apps", nargs="+")
    p.add_argument("--reinstall", action="store_true", help="install even if already installed")
    p.set_defaults(func=cmd_install)
    p = sub.add_parser("update", aliases=["upgrade"], help="update installed apps (all by default)")
    p.add_argument("apps", nargs="*")
    p.set_defaults(func=cmd_update)
    p = sub.add_parser("remove", aliases=["uninstall"], help="uninstall apps")
    p.add_argument("apps", nargs="+")
    p.set_defaults(func=cmd_remove)
    p = sub.add_parser("open", aliases=["launch"], help="start an installed app")
    p.add_argument("apps", nargs="+")
    p.set_defaults(func=cmd_open)
    p = sub.add_parser("apps", help="list every installed app (deb, snap, flatpak, AppImage)")
    p.add_argument("--channel", choices=["apt", "snap", "flatpak", "appimage"])
    p.add_argument("--no-updates", action="store_true", help="skip the online update check")
    p.set_defaults(func=cmd_apps)
    p = sub.add_parser("recent", help="list recently uninstalled apps")
    p.set_defaults(func=cmd_recent)
    p = sub.add_parser("install-deb", help="install a local .deb file with its dependencies")
    p.add_argument("file")
    p.set_defaults(func=cmd_install_deb)
    p = sub.add_parser("ai", help="ask Gemini how to install an app (lists verified options)")
    p.add_argument("query", nargs="+")
    p.set_defaults(func=cmd_ai)
    p = sub.add_parser("update-all", aliases=["upgrade-all"],
                       help="update every app (GitHub, deb, snap, flatpak) with one password")
    p.add_argument("--apps-only", action="store_true",
                   help="only apps; skip other system updates (apt update + upgrade)")
    p.add_argument("--system", action="store_true", help=argparse.SUPPRESS)  # default now
    p.add_argument("--include-phased", action="store_true",
                   help="also install Ubuntu's staged-rollout (phased) updates early")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_update_all)
    p = sub.add_parser("add", help="add a GitHub project with .deb releases to the Library")
    p.add_argument("repo", help="owner/repo or https://github.com/owner/repo")
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("favorites", help="list favorite apps")
    p.set_defaults(func=cmd_favorites)

    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0].endswith(".deb") and os.path.isfile(argv[0]):
        from .gui import run
        return run([os.path.abspath(argv[0])])
    args = parser.parse_args(argv)
    if not args.command:
        from .gui import run
        return run()
    try:
        return args.func(args)
    except core.HubError as e:
        print(f"app-hub: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
