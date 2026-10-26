"""Powerful Scan: what an uninstalled app left behind.

After an app's own uninstaller ran, App Hub looks for its folders (install folder,
AppData, ProgramData, Program Files), registry keys (Software\\Publisher\\App, App Paths,
start-up entries, a leftover Uninstall entry), Start menu and desktop shortcuts, Windows
services and scheduled tasks. Matches on the app's full name are selected; matches on a
shorter form of the name are listed but left for you to tick.

Nothing that another installed app could use is offered. Folders go to the Recycle Bin;
registry keys are exported to a .reg backup before they are deleted.
"""

import json
import os
import re
from dataclasses import dataclass, field

from . import cleaner, helper, inventory, winsys

if winsys.IS_WIN:
    import winreg

KINDS = {"folder": "Files and folders", "registry": "Registry", "shortcut": "Shortcuts",
         "service": "Services", "task": "Scheduled tasks"}
REG_ROOTS = (("HKCU", "Software"), ("HKLM64", "SOFTWARE"), ("HKLM32", "SOFTWARE"))
NEVER_KEYS = {"microsoft", "classes", "policies", "wow6432node", "clients", "registeredapplications",
              "odbc", "khronos", "python", "intel", "nvidia corporation", "amd", "realtek", "google",
              "mozilla", "apple inc.", "apple computer, inc.", "oracle", "javasoft", "app hub", "windows",
              "partner", "defaultuserenvironment", "wow6432node", "appdatalow", "netscape", "gnu", "cygwin",
              "msys2", "macromedia", "adobe", "chromium", "valve", "vmware, inc.", "wsl", "git-for-windows"}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_PATHS = r"Software\Microsoft\Windows\CurrentVersion\App Paths"
UNINSTALL = inventory.UNINSTALL


@dataclass
class Leftover:
    kind: str                 # folder | registry | shortcut | service | task
    label: str
    detail: str = ""
    size: int = 0
    selected: bool = True
    admin: bool = False
    step: list = field(default_factory=list)
    app: str = ""             # which app left it


@dataclass
class Target:
    """What is known about an app being removed (taken before its uninstaller runs)."""
    name: str
    publisher: str = ""
    location: str = ""
    exe: str = ""
    ref: str = ""             # Uninstall registry entry, e.g. HKLM64\{GUID}
    family: str = ""          # Store package family
    force: bool = False       # the uninstaller did not finish: also its install folder and entry

    @classmethod
    def from_sysapp(cls, app):
        entry = app.extra.get("arp") if isinstance(app.extra, dict) else None
        exe = inventory.main_exe(entry) if entry else ""
        return cls(name=app.name, publisher=app.source, location=app.location or (entry.location if entry else ""),
                   exe=exe, ref=entry.ref if entry else "",
                   family=app.extra.get("family", "") if isinstance(app.extra, dict) else "")

    def keys(self):
        """(strong, weak) normalized names: full name, then a shorter form."""
        strong = {inventory.norm(self.name), inventory.norm(inventory.base_name(self.name))}
        if self.exe:
            strong.add(inventory.norm(os.path.splitext(os.path.basename(self.exe))[0]))
        if self.location:
            strong.add(inventory.norm(os.path.basename(os.path.normpath(self.location))))
        weak = set()
        first = re.split(r"[\s\-_.]+", inventory.base_name(self.name).strip())[0]
        if len(first) >= 5:
            weak.add(inventory.norm(first))
        strong = {k for k in strong if len(k) >= 4}
        return strong, {k for k in weak if len(k) >= 5} - strong


def _taken(installed):
    """Names, publishers and folders of apps that stay installed: never offered for removal."""
    taken, folders = set(), []
    for a in installed:
        taken |= {inventory.norm(a.name), inventory.norm(inventory.base_name(a.name))}
        if a.location:
            taken.add(inventory.norm(os.path.basename(os.path.normpath(a.location))))
            folders.append(os.path.normcase(os.path.abspath(a.location)).rstrip("\\"))
    return {k for k in taken if k}, folders


def _inside(path, folder):
    return bool(folder) and os.path.normcase(os.path.abspath(path)).startswith(
        os.path.normcase(os.path.abspath(folder)).rstrip("\\") + "\\")


def _used_by_other(path, other_folders):
    p = os.path.normcase(os.path.abspath(path)).rstrip("\\")
    return any(p == f or f.startswith(p + "\\") or p.startswith(f + "\\") for f in other_folders)


# --------------------------------------------------------------------------
# files and folders
# --------------------------------------------------------------------------

def _folder_roots():
    local_low = os.path.join(os.path.dirname(winsys.LOCALAPPDATA), "LocalLow")
    return [winsys.APPDATA, winsys.LOCALAPPDATA, local_low, os.path.join(winsys.LOCALAPPDATA, "Programs"),
            winsys.PROGRAMDATA, *winsys.PROGRAM_FILES]


def _is_machine(path):
    return any(_inside(path, r) or os.path.normcase(path) == os.path.normcase(r)
               for r in [winsys.PROGRAMDATA, *winsys.PROGRAM_FILES, winsys.WINDIR])


def scan_folders(t, taken, other_folders):
    strong, weak = t.keys()
    pub = inventory.norm(t.publisher)
    out, seen = [], set()

    def add(path, why, selected):
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or not os.path.exists(path) or helper.never_remove(path) or _used_by_other(path, other_folders):
            return
        seen.add(key)
        out.append(Leftover("folder", os.path.basename(path) or path, f"{path} · {why}",
                            cleaner.dir_size(path), selected, _is_machine(path), ["recycle", path], t.name))

    if t.location and os.path.isdir(t.location):
        root_level = any(os.path.normcase(os.path.abspath(t.location)) == os.path.normcase(os.path.abspath(r))
                         for r in _folder_roots())
        if not root_level:
            add(t.location, "install folder", True)
    if t.family:
        add(os.path.join(winsys.LOCALAPPDATA, "Packages", t.family), "app data", True)
    for root in _folder_roots():
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            k = inventory.norm(name)
            if k in taken:
                continue
            if k in strong:
                add(path, "named after the app", True)
            elif k in weak:
                add(path, "may belong to the app", False)
            elif pub and len(pub) >= 4 and k in (pub, inventory.norm(inventory.base_name(t.publisher))):
                # Publisher\App: the app's folder, or the publisher folder when nothing else is in it.
                try:
                    subs = [s for s in os.listdir(path)]
                except OSError:
                    continue
                mine = [s for s in subs if inventory.norm(s) in strong]
                for s in mine:
                    add(os.path.join(path, s), "named after the app", True)
                if mine and len(subs) == len(mine):
                    seen.update(os.path.normcase(os.path.abspath(os.path.join(path, s))) for s in mine)
                    out[:] = [o for o in out if not _inside(o.step[1], path)]
                    add(path, "publisher folder with only this app", True)
    return out


def scan_shortcuts(t):
    strong, _weak = t.keys()
    out = []
    roots = winsys.start_menu_dirs() + [winsys.known_folder("B4BFCC3A-DB2C-424C-B029-7FE99A87C641", "Desktop"),
                                        helper.SHORTCUT_ROOTS[-1]]
    for root in roots:
        for folder, dirs, files in os.walk(root):
            depth = folder[len(root):].count(os.sep)
            if depth > 2:
                dirs[:] = []
                continue
            for d in list(dirs):
                if inventory.norm(d) in strong:
                    path = os.path.join(folder, d)
                    out.append(Leftover("shortcut", d, f"{path} · Start menu folder", 0, True,
                                        helper._inside(path, winsys.PROGRAMDATA), ["recycle", path], t.name))
                    dirs.remove(d)
            for f in files:
                if not f.lower().endswith(".lnk"):
                    continue
                stem = inventory.norm(f[:-4])
                if stem in strong or any(stem.startswith(k) for k in strong if len(k) >= 5):
                    path = os.path.join(folder, f)
                    admin = helper._inside(path, winsys.PROGRAMDATA) or helper._inside(path, helper.SHORTCUT_ROOTS[-1])
                    out.append(Leftover("shortcut", f[:-4], path, 0, True, admin, ["recycle", path], t.name))
    return out


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

def _open(hive, path):
    root, view = inventory._hive(hive)
    return winreg.OpenKey(root, path, 0, winreg.KEY_READ | view)


def _subkeys(hive, path):
    try:
        with _open(hive, path) as k:
            out, i = [], 0
            while True:
                try:
                    out.append(winreg.EnumKey(k, i))
                except OSError:
                    return out
                i += 1
    except OSError:
        return []


def _values(hive, path):
    try:
        with _open(hive, path) as k:
            out, i = [], 0
            while True:
                try:
                    out.append(winreg.EnumValue(k, i))
                except OSError:
                    return out
                i += 1
    except OSError:
        return []


def _reg_item(hive, path, why, selected, app):
    label = f"{hive}\\{path}"
    return Leftover("registry", path.split("\\")[-1], f"{label} · {why}", 0, selected, hive != "HKCU",
                    ["reg-delete", hive, path], app)


def scan_registry(t, taken):
    strong, weak = t.keys()
    pub = inventory.norm(t.publisher)
    out, seen = [], set()

    def add(hive, path, why, selected):
        key = ("HKLM" if hive.startswith("HKLM") else hive, path.lower())
        if key in seen:
            return
        seen.add(key)
        out.append(_reg_item(hive, path, why, selected, t.name))

    if winsys.machine_arch() == "x86":
        roots = [r for r in REG_ROOTS if r[0] != "HKLM32"]
    else:
        roots = REG_ROOTS
    for hive, base in roots:
        for sub in _subkeys(hive, base):
            k = inventory.norm(sub)
            if sub.lower() in NEVER_KEYS or k in taken:
                continue
            if k in strong:
                add(hive, f"{base}\\{sub}", "settings of the app", True)
            elif k in weak:
                add(hive, f"{base}\\{sub}", "may belong to the app", False)
            elif pub and len(pub) >= 4 and k == pub:
                children = _subkeys(hive, f"{base}\\{sub}")
                mine = [c for c in children if inventory.norm(c) in strong]
                if mine and len(children) == len(mine) and not _values(hive, f"{base}\\{sub}"):
                    add(hive, f"{base}\\{sub}", "publisher key with only this app", True)
                else:
                    for c in mine:
                        add(hive, f"{base}\\{sub}\\{c}", "settings of the app", True)
        # App Paths: lets Windows start the program by its name
        for sub in _subkeys(hive, f"{base}\\{APP_PATHS[len('Software') + 1:]}"):
            path = f"{base}\\{APP_PATHS[len('Software') + 1:]}\\{sub}"
            target = next((str(v) for n, v, _t in _values(hive, path) if n == ""), "")
            stem = inventory.norm(os.path.splitext(sub)[0])
            if (t.location and target and _inside(target.strip('"'), t.location)) or (
                    stem in strong and target and not os.path.exists(target.strip('"'))):
                add(hive, path, "program path", True)
        # Start-up entries
        for name, value, _type in _values(hive, f"{base}\\{RUN_KEY[len('Software') + 1:]}"):
            text = str(value)
            if (t.location and t.location.lower().rstrip("\\") in text.lower()) or (
                    inventory.norm(name) in strong):
                out.append(Leftover("registry", name, f"{hive}\\{base}\\…\\Run · starts with Windows: {text[:80]}", 0,
                                    True, hive != "HKCU",
                                    ["reg-delete-value", hive, f"{base}\\{RUN_KEY[len('Software') + 1:]}", name],
                                    t.name))
    if t.ref:
        hive, _, key = t.ref.partition("\\")
        if inventory.read_entry(hive, key) is not None:
            entry = inventory.read_entry(hive, key)[0]
            exe = winsys.parse_icon_spec(entry.uninstall)[0] if entry.uninstall.startswith('"') else \
                entry.uninstall.split(" /")[0]
            if t.force or (exe and not os.path.exists(exe) and "msiexec" not in exe.lower()):
                add(hive, f"{'Software' if hive == 'HKCU' else 'SOFTWARE'}\\{UNINSTALL[len('Software') + 1:]}\\{key}",
                    "entry in Settings ▸ Apps", True)
    return out


# --------------------------------------------------------------------------
# services and scheduled tasks
# --------------------------------------------------------------------------

_SYSTEM_PS = r"""
$s = @(Get-CimInstance Win32_Service -ErrorAction SilentlyContinue | ForEach-Object { @{ n = $_.Name; d = $_.DisplayName; p = $_.PathName } })
$t = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskPath -notlike '\Microsoft\*' } |
  ForEach-Object { @{ n = $_.TaskName; p = $_.TaskPath; e = (($_.Actions | ForEach-Object { $_.Execute }) -join '|') } })
@{ services = $s; tasks = $t } | ConvertTo-Json -Depth 3 -Compress
"""
_system_cache = None


def _system():
    global _system_cache
    if _system_cache is None:
        r = winsys.powershell(_SYSTEM_PS, timeout=120)
        try:
            _system_cache = json.loads(r.stdout.strip() or "{}")
        except ValueError:
            _system_cache = {}
    return _system_cache


def forget_system():
    global _system_cache
    _system_cache = None


def scan_system(t):
    strong, _weak = t.keys()
    data = _system()
    out = []
    services = data.get("services") or []
    if isinstance(services, dict):
        services = [services]
    for s in services:
        path = (s.get("p") or "").strip()
        exe = path[1:path.find('"', 1)] if path.startswith('"') else path.split(" ")[0]
        mine = (t.location and exe and _inside(exe, t.location)) or inventory.norm(s.get("n")) in strong \
            or inventory.norm(s.get("d")) in strong
        if mine and re.fullmatch(r"[\w.\-]+", s.get("n") or ""):
            out.append(Leftover("service", s.get("d") or s["n"], f"Service {s['n']} · {path[:90]}", 0, True, True,
                                ["service-delete", s["n"]], t.name))
    tasks = data.get("tasks") or []
    if isinstance(tasks, dict):
        tasks = [tasks]
    for task in tasks:
        exe = (task.get("e") or "").strip('"')
        name = task.get("n") or ""
        mine = (t.location and exe and any(_inside(x.strip('"'), t.location) for x in exe.split("|"))) or \
            inventory.norm(name) in strong or any(inventory.norm(name).startswith(k) for k in strong if len(k) >= 5)
        if mine:
            full = (task.get("p") or "\\") + name
            out.append(Leftover("task", name, f"Task {full} · runs {exe[:80]}", 0, True, True,
                                ["task-delete", full], t.name))
    return out


# --------------------------------------------------------------------------

def scan(targets, installed):
    """Leftovers of `targets` (apps just removed). `installed`: the apps that stay."""
    taken, other_folders = _taken(installed)
    forget_system()
    out = []
    for t in targets:
        out += scan_folders(t, taken, other_folders)
        out += scan_shortcuts(t)
        out += scan_registry(t, taken)
        out += scan_system(t)
    # The same thing found for two apps: keep one.
    unique, seen = [], set()
    for item in out:
        key = tuple(item.step)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def plans(items):
    """(admin_plan, user_plan) that delete the selected leftovers."""
    admin, user = [["keep-going"]], [["keep-going"]]
    order = {"service-delete": 0, "task-delete": 1, "reg-delete-value": 2, "reg-delete": 3, "recycle": 4}
    for item in sorted((i for i in items if i.selected), key=lambda i: order.get(i.step[0], 9)):
        (admin if item.admin and not winsys.is_elevated() else user).append(item.step)
    return admin, user
