"""Installing what GitHub releases ship for Windows.

Installers (.msi, Inno Setup, NSIS, WiX Burn, Squirrel) run silently. A portable .zip
or a single .exe is unpacked to %LOCALAPPDATA%\\Programs\\App Hub Apps, gets a Start
menu shortcut and an entry in Settings ▸ Apps, so it can be opened, updated and
removed like any other app.
"""

import datetime
import os
import re
import shutil
import zipfile

from . import core, winsys

if winsys.IS_WIN:
    import winreg

KINDS = {"msi": "Windows Installer (.msi)", "inno": "Inno Setup installer", "nsis": "NSIS installer",
         "burn": "WiX installer", "squirrel": "Squirrel installer", "msix": "MSIX package",
         "zip": "portable app (.zip)", "portable-exe": "portable app (.exe)",
         "exe-installer": "installer (shows its own window)"}
SILENT = {
    "msi": lambda p: ["msiexec.exe", "/i", p, "/qn", "/norestart"],
    "inno": lambda p: [p, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/CLOSEAPPLICATIONS"],
    "nsis": lambda p: [p, "/S"],
    "burn": lambda p: [p, "/quiet", "/norestart"],
    "squirrel": lambda p: [p, "--silent"],
    "exe-installer": lambda p: [p],
}
OK_CODES = {0, 3010, 1641}          # 3010/1641: done, restart Windows to finish
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"


EXIT_CODES = {
    "inno": {1: "Setup could not start", 2: "cancelled before it began", 3: "a fatal error before installing",
             4: "a fatal error while installing", 5: "cancelled: a program using its files could not be closed",
             6: "it was ended", 7: "it could not prepare the installation", 8: "restart Windows first, then try again"},
    "msi": {1602: "cancelled", 1603: "a fatal error while installing", 1618: "another installation is running",
            1638: "another version is already installed", 1625: "blocked by a Windows policy",
            1633: "this installer does not fit this PC"},
    "nsis": {1: "cancelled", 2: "aborted by the installer"},
}


def explain(kind, rc):
    return EXIT_CODES.get(kind, {}).get(rc, "see the Activity Log")


def detect(path):
    """Installer kind of a downloaded file (see KINDS)."""
    low = path.lower()
    if low.endswith(".msi"):
        return "msi"
    if low.endswith((".msix", ".msixbundle", ".appx", ".appxbundle")):
        return "msix"
    if low.endswith(".zip"):
        return "zip"
    if not low.endswith(".exe"):
        return ""
    try:
        with open(path, "rb") as f:
            head = f.read(24 * 1024 * 1024)
    except OSError:
        return ""
    if b"JR.Inno.Setup" in head or b"Inno Setup Setup Data" in head:
        return "inno"
    if b"Nullsoft.NSIS.exehead" in head or b"NullsoftInst" in head:
        return "nsis"
    if b".wixburn" in head:
        return "burn"
    if b"SquirrelSetup" in head or (b"Squirrel" in head and b"Update.exe" in head):
        return "squirrel"
    info = winsys.version_info(path)
    text = " ".join(info.get(k, "") for k in ("FileDescription", "ProductName")).lower()
    if re.search(r"setup|install", text) or re.search(r"setup|install", os.path.basename(low)):
        return "exe-installer"
    return "portable-exe"


def needs_admin(kind, entry=None):
    """Whether installing this kind (over an existing install `entry`) belongs in the elevated plan."""
    if kind in ("msix", "zip", "portable-exe", "squirrel", "exe-installer"):
        return False          # per-user, or shows its own window and asks for itself
    if kind == "msi":
        return True
    if entry is not None:
        return entry.machine
    # A new install: the installer decides where it goes. Running it elevated avoids a second
    # prompt from the installer itself, but only when UAC keeps this same user account.
    return winsys.elevates_as_self()


def install_step(path, kind, label, app=None):
    if kind in ("zip", "portable-exe"):
        app_id = re.sub(r"[^A-Za-z0-9._-]", "-", app.id if app else os.path.splitext(os.path.basename(path))[0])
        name = app.name if app else label
        version = (app.release.version if app and app.release else "") or core.version_from_name(
            os.path.basename(path))
        return ["portable", path, app_id, name, version, app.exe if app else "", app.owner if app else ""]
    return ["install", kind, path, label]


def silent_command(kind, path):
    if kind not in SILENT:
        raise core.HubError(f"cannot install {os.path.basename(path)} ({kind or 'unknown file type'})")
    return SILENT[kind](path)


def start_self_update(path, log=print):
    """Run App Hub's own installer. It closes App Hub, updates it and starts it again
    (/RELAUNCH, see packaging/windows/installer.iss); its own window shows the progress."""
    if installer_kind := detect(path):
        if installer_kind != "inno":
            raise core.HubError(f"{os.path.basename(path)} is not App Hub's installer")
    log("App Hub closes now, updates itself and opens again")
    handle = winsys.shell_execute(path, "/SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /RELAUNCH",
                                  verb="open", show=1)
    winsys.close_handle(handle)


# --------------------------------------------------------------------------
# portable apps
# --------------------------------------------------------------------------

def _safe_extract(zf, dest):
    for name in zf.namelist():
        target = os.path.normpath(os.path.join(dest, name))
        if not target.startswith(os.path.normpath(dest) + os.sep) and target != os.path.normpath(dest):
            raise core.HubError(f"unsafe path in the .zip: {name}")
    zf.extractall(dest)


def _flatten(folder):
    """A zip holding one top folder: use that folder's contents."""
    items = os.listdir(folder)
    if len(items) == 1 and os.path.isdir(os.path.join(folder, items[0])):
        inner = os.path.join(folder, items[0])
        for item in os.listdir(inner):
            shutil.move(os.path.join(inner, item), os.path.join(folder, item))
        os.rmdir(inner)


def find_main_exe(folder, name, app_id, hint=""):
    if hint:
        path = os.path.join(folder, hint)
        if os.path.exists(path):
            return path
    exes = []
    for root, _dirs, files in os.walk(folder):
        depth = root[len(folder):].count(os.sep)
        for f in files:
            if f.lower().endswith(".exe") and depth <= 2 and not re.search(
                    r"unins|uninstall|update|crash|helper|elevate|setup|vc_?redist|notification", f, re.IGNORECASE):
                exes.append(os.path.join(root, f))
    if not exes:
        return ""
    keys = {re.sub(r"[^a-z0-9]", "", s.lower()) for s in (name, app_id) if s}

    def score(p):
        stem = re.sub(r"[^a-z0-9]", "", os.path.splitext(os.path.basename(p))[0].lower())
        exact = stem in keys or any(k.startswith(stem) or stem.startswith(k) for k in keys if len(stem) >= 3)
        return (exact, -p[len(folder):].count(os.sep), os.path.getsize(p))
    return max(exes, key=score)


def install_portable(path, app_id, name, version, hint="", publisher="", log=print):
    root = winsys.portable_root()
    target = os.path.join(root, app_id)
    tmp = target + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    if path.lower().endswith(".zip"):
        log(f"Unpacking {os.path.basename(path)}")
        with zipfile.ZipFile(path) as zf:
            _safe_extract(zf, tmp)
        _flatten(tmp)
    else:
        shutil.copy2(path, os.path.join(tmp, os.path.basename(path)))
    exe = find_main_exe(tmp, name, app_id, hint)
    if not exe:
        shutil.rmtree(tmp, ignore_errors=True)
        raise core.HubError(f"{name}: no program (.exe) found in {os.path.basename(path)}")
    if os.path.exists(target):
        old = target + ".old"
        shutil.rmtree(old, ignore_errors=True)
        try:
            os.replace(target, old)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise core.HubError(f"{name} is running; close it and try again") from None
        shutil.rmtree(old, ignore_errors=True)
    os.replace(tmp, target)
    exe = os.path.join(target, os.path.relpath(exe, tmp))
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(target) for f in fs)
    lnk = os.path.join(winsys.start_menu_dirs()[0], f"{_lnk_name(name)}.lnk")
    if not winsys.create_shortcut(lnk, exe, description=name):
        log(f"{name}: could not create the Start menu shortcut")
    uninstall = winsys.quote_args(winsys.launcher() + ["--remove-portable", app_id])
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{UNINSTALL_KEY}\AppHub_{app_id}") as k:
        for key, value in (("DisplayName", name), ("DisplayVersion", version or ""),
                           ("Publisher", publisher or "App Hub (portable)"), ("InstallLocation", target),
                           ("DisplayIcon", exe), ("UninstallString", uninstall),
                           ("InstallDate", datetime.date.today().strftime("%Y%m%d")),
                           ("AppHubShortcut", lnk)):
            winreg.SetValueEx(k, key, 0, winreg.REG_SZ, value)
        for key, value in (("NoModify", 1), ("NoRepair", 1), ("EstimatedSize", size // 1024)):
            winreg.SetValueEx(k, key, 0, winreg.REG_DWORD, value)
    log(f"{name} {version}: installed in {target}")
    return target


def _lnk_name(name):
    return re.sub(r'[\\/:*?"<>|]', "", name).strip() or "App"


def remove_portable(app_id, log=print):
    """Remove an app App Hub unpacked: folder (to the Recycle Bin), shortcut, Settings entry."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", app_id or ""):
        raise core.HubError(f"not an App Hub portable app: {app_id}")
    key = rf"{UNINSTALL_KEY}\AppHub_{app_id}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            location = winreg.QueryValueEx(k, "InstallLocation")[0]
            try:
                lnk = winreg.QueryValueEx(k, "AppHubShortcut")[0]
            except OSError:
                lnk = ""
    except OSError:
        raise core.HubError(f"{app_id} is not installed") from None
    root = os.path.normcase(os.path.abspath(winsys.portable_root()))
    if location and os.path.normcase(os.path.abspath(location)).startswith(root + os.sep):
        if os.path.isdir(location) and not winsys.recycle([location]):
            raise core.HubError(f"could not remove {location}; is the app still running?")
        log(f"moved {location} to the Recycle Bin")
    if lnk and lnk.lower().endswith(".lnk") and os.path.exists(lnk):
        os.unlink(lnk)
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
    return 0
