"""Command-line front end. With no command it opens the window.

    app-hub                         open the window (--page updates|my-apps|library|all-apps|ai|cleaner)
    app-hub FILE.exe|.msi|.zip      open the window and offer to install the file
    app-hub list [--json]           My Apps and Library: installed vs latest
    app-hub install NAME...         install from the latest GitHub release
    app-hub update [NAME...]        update GitHub apps (all when no name is given)
    app-hub update-all [-y] [--dry-run] [--json]   every app update plus winget, one UAC prompt
    app-hub apps [--json] [--updates]              every installed app
    app-hub recent                  recently uninstalled apps
    app-hub add owner/repo          add a GitHub project to the Library
    app-hub ai "pdf editor"         ask Gemini; every suggestion is checked
    app-hub favorites               favorite apps
"""

import argparse
import json
import os
import sys
import time

from . import __version__, core

PAGES = ["updates", "my-apps", "library", "all-apps", "ai", "cleaner"]
COMMANDS = {"list", "install", "update", "update-all", "apps", "recent", "add", "ai", "favorites"}


def _out(text=""):
    try:
        print(text)
    except (OSError, ValueError):     # no console (windowed build)
        pass


def _bar(done, total, label):
    if not (sys.stderr and sys.stderr.isatty()):
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


def _status(a):
    if a.error:
        return a.error
    if a.release and not a.release.asset_url:
        return "no Windows download yet"
    if a.update_available:
        return "update available"
    if a.installed:
        return "up to date"
    if a.installable:
        return "not installed"
    return "no Windows download"


def _json(data):
    _out(json.dumps(data, indent=1, ensure_ascii=False))


def cmd_list(args):
    apps = _load()
    if args.json:
        _json({"version": __version__, "checked_at": int(time.time()), "apps": [{
            "id": a.id, "name": a.name, "summary": a.summary,
            "installed": core.clean_version(a.installed) if a.installed else None,
            "latest": a.release.version if a.release else None, "status": _status(a),
            "update_available": a.update_available, "error": a.error or None,
            "download": a.release.asset_name if a.release else None,
        } for a in apps]})
        return 0
    width = max(len(a.name) for a in apps)
    for a in apps:
        inst = core.clean_version(a.installed) if a.installed else "-"
        latest = a.release.version if a.release and a.release.asset_url else "-"
        _out(f"{a.name:<{width}}  {inst:<16} {latest:<16} {_status(a)}")
    return 0


def _install(apps, log=_out):
    from . import helper, updates
    staged = []
    for app in apps:
        if not app.installable:
            log(f"{app.name}: {_status(app)}")
            continue
        log(f"Downloading {app.release.asset_name}")
        staged.append((app, core.download(app, lambda d, t, n=app.name: _bar(d, t, n))))
        if sys.stderr and sys.stderr.isatty():
            sys.stderr.write("\n")
    admin, user = updates.hub_steps(staged)
    rc, _ = helper.execute([["keep-going"], *admin], [["keep-going"], *user], None, log)
    if rc == 0:
        for app, _ in staged:
            core.finish_install(app, log)
    return rc


def cmd_install(args):
    apps = core.find_apps(_load(), args.names)
    return _install(apps)


def cmd_update(args):
    apps = _load()
    if args.names:
        apps = core.find_apps(apps, args.names)
    todo = [a for a in apps if a.update_available]
    if not todo:
        _out("Everything is up to date")
        return 0
    return _install(todo)


def _inventory(updates=True):
    from . import inventory
    apps = inventory.list_all()
    system = inventory.apply_winget(apps) if updates else []
    return apps, system


def cmd_apps(args):
    apps, _ = _inventory()
    if args.updates:
        apps = [a for a in apps if a.update]
    if args.json:
        _json([{"name": a.name, "channel": a.channel, "version": a.version, "publisher": a.source,
                "update": a.update or None, "winget_id": a.winget_id or None, "scope": a.scope,
                "id": a.id} for a in apps])
        return 0
    for a in apps:
        up = f"  → {a.update}" if a.update else ""
        _out(f"{a.name[:48]:<48} {a.channel_label:<9} {a.version[:18]:<18}{up}")
    return 0


def cmd_update_all(args):
    from . import updates
    hub = _load()
    sys_apps, system = _inventory()
    pending = updates.collect(hub, sys_apps)
    if args.dry_run or args.json:
        if args.json:
            _json({"apps": [{"name": p.label, "kind": p.kind, "from": p.old, "to": p.new} for p in pending],
                   "system": [{"name": r["name"], "id": r["id"], "from": r["version"], "to": r["available"]}
                              for r in system]})
        else:
            for p in pending:
                _out(f"{p.label}: {p.old} → {p.new} ({'GitHub' if p.kind == 'hub' else 'winget'})")
            for r in system:
                _out(f"{r['name']}: {r['version']} → {r['available']} (winget, system)")
            if not pending and not system:
                _out("Everything is up to date")
        return 0
    if not pending and not system:
        _out("Everything is up to date")
        return 0
    if not args.yes:
        for p in pending:
            _out(f"  {p.label}: {p.old} → {p.new}")
        for r in system:
            _out(f"  {r['name']}: {r['version']} → {r['available']}")
        try:
            if input(f"Install {len(pending) + len(system)} update(s)? [Y/n] ").strip().lower() not in ("", "y", "yes"):
                return 1
        except EOFError:
            return 1
    rc, _ = updates.run(pending, _out, system=True, system_rows=system)
    return rc


def cmd_recent(_args):
    from . import history
    apps, _ = _inventory(updates=False)
    for e in history.recent(apps):
        _out(f"{e.name[:40]:<40} {e.when():<16} {e.version}")
    return 0


def cmd_add(args):
    from . import library
    owner, repo = library.parse_github(args.repo)
    if not (owner and repo):
        raise core.HubError("give a GitHub repository: owner/repo or its link")
    entry = library.resolve_repo(f"{owner}/{repo}")
    library.add(library.finish_entry(entry))
    _out(f"Added {entry['name']} ({entry['repo']}, {entry['asset']}) to the Library")
    return 0


def cmd_ai(args):
    from . import ai
    answer = ai.find(" ".join(args.query), lambda s: _out(f"  {s}"))
    for found in answer.apps:
        _out(f"\n{found.name}: {found.summary}")
        for opt in found.options:
            _out(f"  {'✓' if opt.verified else '✗'} {opt.recipe.describe()}  ({opt.status})")
    return 0


def cmd_favorites(_args):
    from . import favorites
    for r in favorites.load():
        _out(f"{r.name:<30} {r.channel_label:<8} {r.describe()}")
    return 0


def _selftest():
    import traceback
    from . import winsys
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            detail = fn()
            _out(f"  ok    {name}" + (f": {detail}" if detail else ""))
        except Exception:  # noqa: BLE001
            ok = False
            _out(f"  FAIL  {name}")
            traceback.print_exc()

    _out(f"App Hub {__version__} selftest (frozen={winsys.FROZEN}, lib={core.LIB_DIR})")
    check("catalog", lambda: f"{len(core.load_catalog())} apps")
    check("machine", lambda: f"{winsys.windows_version()}, {winsys.machine_arch()}")
    check("registry", lambda: f"{len(__import__('app_hub.inventory', fromlist=['x']).read_arp())} entries")

    def gtk():
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk
        return (f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}, "
                f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}, display={Gtk.init_check()}")
    check("GTK 4 + libadwaita", gtk)
    check("GUI module", lambda: __import__("app_hub.gui", fromlist=["x"]) and "import ok")
    _out("SELFTEST OK" if ok else "SELFTEST FAILED")
    return 0 if ok else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # Internal entry points (no argparse, so nothing else is accepted with them).
    if argv[:1] == ["--helper"] and len(argv) == 4 and argv[2] == "--log":
        from . import helper
        return helper.main(argv[1], argv[3])
    if argv[:1] == ["--remove-portable"] and len(argv) == 2:
        from . import installer
        try:
            return installer.remove_portable(argv[1])
        except core.HubError as e:
            _out(str(e))
            return 1
    if argv[:1] == ["--selftest"]:
        return _selftest()
    if argv[:1] == ["--version"]:
        _out(f"app-hub {__version__}")
        return 0

    if argv and argv[0] in COMMANDS:
        ap = argparse.ArgumentParser(prog="app-hub", description="App Hub for Windows")
        sub = ap.add_subparsers(dest="cmd", required=True)
        p = sub.add_parser("list")
        p.add_argument("--json", action="store_true")
        p.set_defaults(fn=cmd_list)
        p = sub.add_parser("install")
        p.add_argument("names", nargs="+")
        p.set_defaults(fn=cmd_install)
        p = sub.add_parser("update")
        p.add_argument("names", nargs="*")
        p.set_defaults(fn=cmd_update)
        p = sub.add_parser("update-all")
        p.add_argument("-y", "--yes", action="store_true")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--json", action="store_true")
        p.set_defaults(fn=cmd_update_all)
        p = sub.add_parser("apps")
        p.add_argument("--json", action="store_true")
        p.add_argument("--updates", action="store_true")
        p.set_defaults(fn=cmd_apps)
        p = sub.add_parser("recent")
        p.set_defaults(fn=cmd_recent)
        p = sub.add_parser("add")
        p.add_argument("repo")
        p.set_defaults(fn=cmd_add)
        p = sub.add_parser("ai")
        p.add_argument("query", nargs="+")
        p.set_defaults(fn=cmd_ai)
        p = sub.add_parser("favorites")
        p.set_defaults(fn=cmd_favorites)
        args = ap.parse_args(argv)
        try:
            return args.fn(args)
        except core.HubError as e:
            _out(f"app-hub: {e}")
            return 1

    ap = argparse.ArgumentParser(prog="app-hub", description="App Hub for Windows",
                                 epilog="Commands: " + ", ".join(sorted(COMMANDS)))
    ap.add_argument("--page", choices=PAGES, help="open this page")
    ap.add_argument("file", nargs="?", help="an .exe, .msi or .zip to install")
    args = ap.parse_args(argv)
    from . import ipc
    message = {"page": args.page or "", "file": os.path.abspath(args.file) if args.file else ""}
    if not ipc.claim() and not os.environ.get("APP_HUB_SNAPSHOT"):   # screenshots may run beside it
        return 0 if ipc.send(message) else 1
    from . import gui
    return gui.run(page=args.page, file=message["file"])
