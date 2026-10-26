"""Find and remove what uninstalled apps leave behind, and other space Windows wastes.

Leftover folders and broken shortcuts go to the Recycle Bin (restorable). Temporary
files and caches are deleted. Items under Program Files, ProgramData or Windows go into
one elevated plan (one UAC prompt).
"""

import json
import os
import re
import time
from dataclasses import dataclass, field

from . import core, helper, history, inventory, winsys

USER_ROOTS = [winsys.APPDATA, winsys.LOCALAPPDATA, os.path.join(winsys.LOCALAPPDATA, "Programs")]
MACHINE_ROOTS = [*winsys.PROGRAM_FILES, winsys.PROGRAMDATA]
EMPTY_ROOTS = [*winsys.PROGRAM_FILES, os.path.join(winsys.LOCALAPPDATA, "Programs")]


@dataclass
class Item:
    label: str
    detail: str = ""
    size: int = 0
    selected: bool = True
    step: list = None        # plan step for this item
    admin: bool = False      # the step needs administrator rights


@dataclass
class Group:
    id: str
    title: str
    description: str
    items: list = field(default_factory=list)
    granular: bool = True    # items can be picked one by one
    default_on: bool = True
    admin_plan: list = field(default_factory=list)   # whole-group steps when not granular
    user_plan: list = field(default_factory=list)
    size_override: int = -1

    @property
    def size(self):
        return self.size_override if self.size_override >= 0 else sum(i.size for i in self.items)


def dir_size(path, older_than=None):
    if os.path.isfile(path) or os.path.islink(path):
        try:
            st = os.lstat(path)
            return 0 if older_than and st.st_mtime > older_than else st.st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if older_than and st.st_mtime > older_than:
                continue
            total += st.st_size
    return total


def _old_entries(folder, days):
    """(count, bytes) of top-level entries older than `days` (what 'delete-old' removes)."""
    cutoff = time.time() - days * 86400
    count = size = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0, 0
    for name in names:
        path = os.path.join(folder, name)
        if folder == core.CACHE_DIR and name == "api-cache.json":
            continue
        try:
            if os.lstat(path).st_mtime > cutoff:
                continue
        except OSError:
            continue
        count += 1
        size += dir_size(path)
    return count, size


def _is_empty_tree(path):
    for _root, _dirs, files in os.walk(path, onerror=lambda e: None):
        if files:
            return False
    return True


def _keys_for(name):
    keys = {inventory.norm(name), inventory.norm(inventory.base_name(name))}
    first = re.split(r"[\s\-_]+", inventory.base_name(name).strip())[0] if name else ""
    if len(first) >= 5:
        keys.add(inventory.norm(first))
    return {k for k in keys if len(k) >= 4}


def _installed_keys(apps):
    keys = set()
    for a in apps:
        keys |= _keys_for(a.name)
        if a.source:
            keys.add(inventory.norm(a.source))
        if a.location:
            keys.add(inventory.norm(os.path.basename(os.path.normpath(a.location))))
    return keys


def _is_machine(path):
    return any(helper._inside(path, r) for r in MACHINE_ROOTS)


def leftovers(installed, removed):
    """Folders named after recently removed apps that no installed app uses. A folder that
    matches only the first word of the app's name ("Draco" for "Draco Download Manager")
    is listed but not selected."""
    taken = _installed_keys(installed)
    wanted, strong = {}, set()
    for r in removed:
        full = {inventory.norm(r.name), inventory.norm(inventory.base_name(r.name))}
        for k in _keys_for(r.name):
            if k not in taken:
                wanted.setdefault(k, r.name)
                if k in full:
                    strong.add(k)
    if not wanted:
        return []
    items, seen = [], set()
    for root in USER_ROOTS + MACHINE_ROOTS:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if not os.path.isdir(path) or helper.never_remove(path):
                continue
            candidates = [(name, path)]
            # Publisher\App layout (e.g. %APPDATA%\Vendor\App): look one level deeper too.
            if inventory.norm(name) not in wanted:
                try:
                    candidates = [(sub, os.path.join(path, sub)) for sub in os.listdir(path)
                                  if os.path.isdir(os.path.join(path, sub))][:40]
                except OSError:
                    candidates = []
            for cname, cpath in candidates:
                key = inventory.norm(cname)
                if key not in wanted or key in taken or cpath.lower() in seen:
                    continue
                if helper.never_remove(cpath):
                    continue
                seen.add(cpath.lower())
                admin = _is_machine(cpath)
                items.append(Item(cname, f"{cpath} Â· left by {wanted[key]}", dir_size(cpath),
                                  selected=key in strong, step=["recycle", cpath], admin=admin))
    return items


def empty_folders(installed):
    taken = _installed_keys(installed)
    items = []
    for root in EMPTY_ROOTS:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if (not os.path.isdir(path) or helper.never_remove(path)
                    or inventory.norm(name) in taken or not _is_empty_tree(path)):
                continue
            items.append(Item(name, f"{path} Â· empty folder", 0, step=["recycle", path],
                              admin=_is_machine(path)))
    return items


_LNK_PS = r"""
$sh = New-Object -ComObject WScript.Shell
$out = @()
foreach ($root in $env:AH_ROOTS.Split('|')) {
  if (Test-Path -LiteralPath $root) {
    Get-ChildItem -LiteralPath $root -Recurse -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {
      try { $out += @{ p = $_.FullName; t = $sh.CreateShortcut($_.FullName).TargetPath } } catch {}
    }
  }
}
$out | ConvertTo-Json -Compress
"""


def broken_shortcuts():
    public = helper.SHORTCUT_ROOTS[-1]
    roots = winsys.start_menu_dirs() + [winsys.known_folder("B4BFCC3A-DB2C-424C-B029-7FE99A87C641", "Desktop"),
                                        public]
    r = winsys.powershell(_LNK_PS, timeout=90, env=dict(os.environ, AH_ROOTS="|".join(roots)))
    try:
        data = json.loads(r.stdout.strip() or "[]")
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    items = []
    for d in data:
        target, path = (d.get("t") or "").strip(), d.get("p") or ""
        # Only shortcuts to a missing file on a local fixed drive (not URLs, not Store apps, not drives that
        # may be unplugged).
        if not target or not re.match(r"^[A-Za-z]:\\", target) or os.path.exists(target):
            continue
        if not os.path.exists(os.path.splitdrive(target)[0] + "\\"):
            continue
        if not target.lower().startswith(winsys.SYSTEM_DRIVE.lower()):
            continue
        items.append(Item(os.path.basename(path)[:-4], f"{path} â†’ {target} (missing)", 0,
                          step=["recycle", path],
                          admin=helper._inside(path, winsys.PROGRAMDATA) or helper._inside(path, public)))
    return items


def scan(installed=None):
    """Groups of things to clean. `installed`: current SysApps (for leftover matching)."""
    installed = installed if installed is not None else inventory.list_all(store=False)
    removed = history.recent(installed)
    groups = []

    g = Group("leftovers", "Leftovers of removed apps",
              "Folders named after apps you uninstalled recently, in AppData, ProgramData and Program "
              "Files. Moved to the Recycle Bin.")
    g.items = leftovers(installed, removed) + empty_folders(installed)
    groups.append(g)

    g = Group("shortcuts", "Broken shortcuts",
              "Start menu and desktop shortcuts whose program is gone. Moved to the Recycle Bin.")
    g.items = broken_shortcuts()
    groups.append(g)

    temp = os.environ.get("TEMP") or os.path.join(winsys.LOCALAPPDATA, "Temp")
    n, size = _old_entries(temp, 2)
    g = Group("temp", "Your temporary files",
              f"{temp}, older than two days. Files in use are kept.", granular=False)
    if n:
        g.items = [Item(f"{n} item(s)", temp, size)]
        g.user_plan = [["delete-old", temp, "2"]]
    groups.append(g)

    crash_user = [os.path.join(winsys.LOCALAPPDATA, "CrashDumps"),
                  os.path.join(winsys.LOCALAPPDATA, r"Microsoft\Windows\WER\ReportArchive"),
                  os.path.join(winsys.LOCALAPPDATA, r"Microsoft\Windows\WER\ReportQueue")]
    g = Group("crash", "Crash dumps and error reports",
              "Copies Windows keeps after programs crash.", granular=False)
    for folder in crash_user:
        n, size = _old_entries(folder, 0)
        if n:
            g.items.append(Item(f"{n} item(s)", folder, size))
            g.user_plan.append(["delete-old", folder, "0"])
    n = sum(_old_entries(f, 0)[0] for f in helper.CLEAN_TARGETS["wer"])
    if n:
        size = sum(_old_entries(f, 0)[1] for f in helper.CLEAN_TARGETS["wer"])
        g.items.append(Item(f"{n} item(s) for all users", "ProgramData\\Microsoft\\Windows\\WER", size, admin=True))
        g.admin_plan.append(["clean", "wer"])
    groups.append(g)

    n, size = _old_entries(core.CACHE_DIR, 0)
    g = Group("apphub", "App Hub downloads", "Installers App Hub downloaded and did not need any more.",
              granular=False)
    if n:
        g.items = [Item(f"{n} item(s)", core.CACHE_DIR, size)]
        g.user_plan = [["delete-old", core.CACHE_DIR, "0"]]
    groups.append(g)

    wintemp = helper.CLEAN_TARGETS["windows-temp"][0]
    n, size = _old_entries(wintemp, 1)
    g = Group("wintemp", "Windows temporary files",
              f"{wintemp}, older than a day. Needs administrator permission.", granular=False)
    if n:
        g.items = [Item(f"{n} item(s)", wintemp, size, admin=True)]
        g.admin_plan = [["clean", "windows-temp"]]
    groups.append(g)

    wu = helper.CLEAN_TARGETS["update-cache"][0]
    size = dir_size(wu)
    g = Group("update", "Windows Update downloads",
              "Update files Windows already installed. Windows downloads them again if it needs them. "
              "Needs administrator permission.", granular=False, default_on=False)
    if size:
        g.items = [Item("Downloaded updates", wu, size, admin=True)]
        g.admin_plan = [["clean", "update-cache"]]
    groups.append(g)

    size, count = winsys.recycle_bin_size()
    g = Group("bin", "Recycle Bin", "Empties the Recycle Bin on every drive. This cannot be undone.",
              granular=False, default_on=False)
    if count:
        g.items = [Item(f"{count} item(s)", "Recycle Bin", size)]
        g.user_plan = [["empty-recycle-bin"]]
    groups.append(g)
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
    """(admin_plan, user_plan) for what is selected."""
    admin, user = [["keep-going"]], [["keep-going"]]
    for g in groups:
        if g.granular:
            for i in g.items:
                if i.selected and i.step:
                    (admin if i.admin else user).append(i.step)
        elif enabled.get(g.id) and g.items:
            admin += g.admin_plan
            user += g.user_plan
    return admin, user
