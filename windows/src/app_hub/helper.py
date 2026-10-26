"""Plans of install / update / uninstall / clean steps, and the elevated helper that runs them.

Work that needs administrator rights is collected into one plan and run by App Hub
itself started with UAC (`App Hub --helper PLAN --log LOG`), so Update Everything asks
once. The helper checks every step: installers must come from App Hub's download
folder, uninstall commands are read from the registry by the helper itself, winget ids
and clean-up targets are checked against strict patterns.

Steps (JSON lists):
  ["keep-going"]                              a failing step does not stop the others
  ["install", kind, path, label]              silent installer (installer.SILENT)
  ["portable", path, id, name, version, exe, publisher]   unpack a portable app (user only)
  ["uninstall", "HKLM64\\{key}", label]        Settings ▸ Apps entry, quietly when possible
  ["winget", "install|upgrade|uninstall", id, source, label]
  ["winget-upgrade-all"]
  ["store-remove", package_full_name, label]  Microsoft Store / MSIX app (user only)
  ["store-add", path, label]                  install an .msix (user only)
  ["remove-portable", id]                     (user only)
  ["delete-old", folder, days]                temporary files older than `days`
  ["recycle", path]                           leftover folder to the Recycle Bin
  ["clean", "windows-temp|update-cache|wer|delivery-optimization"]
  ["empty-recycle-bin"]
"""

import json
import os
import re
import shutil
import sys
import time

from . import core, installer, winget, winsys

RUN_DIR = os.path.join(core.DATA_DIR, "run")
USER_ONLY = {"portable", "store-remove", "store-add", "remove-portable"}
LEFTOVER_ROOTS = [*winsys.PROGRAM_FILES, winsys.PROGRAMDATA, winsys.LOCALAPPDATA, winsys.APPDATA,
                  os.path.join(winsys.LOCALAPPDATA, "Programs")]
SHORTCUT_ROOTS = [*winsys.start_menu_dirs(), os.path.join(winsys.HOME, "Desktop"),
                  winsys.known_folder("B4BFCC3A-DB2C-424C-B029-7FE99A87C641", "Desktop"),
                  os.path.join(os.environ.get("PUBLIC") or r"C:\Users\Public", "Desktop")]
NEVER_RECYCLE = {"microsoft", "windows", "common files", "windowsapps", "packages", "temp", "programs",
                 "app hub", "windows defender", "windows nt", "internet explorer", "microsoft shared",
                 "package cache", "nvidia corporation", "intel", "amd", "realtek", "google", "mozilla",
                 "windows mail", "windows media player", "windows photo viewer", "windowspowershell",
                 "modifiablewindowsapps", "reference assemblies", "dotnet", "msbuild", "uninstall information",
                 "windows sidebar", "windows portable devices", "windows security", "windows kits",
                 "installshield installation information", "msecache", "system32", "syswow64"}
SYSTEM_NAME = re.compile(r"^(windows|microsoft|msbuild|dotnet|common files|internet explorer)\b", re.IGNORECASE)


def never_remove(path):
    """Folders of Windows itself (or shared by many apps) that the Cleaner never touches."""
    name = os.path.basename(os.path.normpath(path))
    if name.lower() in NEVER_RECYCLE or SYSTEM_NAME.match(name):
        return True
    try:
        attrs = os.stat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & 0x4)          # FILE_ATTRIBUTE_SYSTEM
CLEAN_TARGETS = {
    "windows-temp": [os.path.join(winsys.WINDIR, "Temp")],
    "update-cache": [os.path.join(winsys.WINDIR, "SoftwareDistribution", "Download")],
    "wer": [os.path.join(winsys.PROGRAMDATA, r"Microsoft\Windows\WER\ReportArchive"),
            os.path.join(winsys.PROGRAMDATA, r"Microsoft\Windows\WER\ReportQueue")],
}


def user_clean_folders():
    """Folders whose old files a non-admin 'delete-old' step may remove."""
    return [os.environ.get("TEMP") or os.path.join(winsys.LOCALAPPDATA, "Temp"),
            os.path.join(winsys.LOCALAPPDATA, "CrashDumps"),
            os.path.join(winsys.LOCALAPPDATA, r"Microsoft\Windows\WER\ReportArchive"),
            os.path.join(winsys.LOCALAPPDATA, r"Microsoft\Windows\WER\ReportQueue"),
            core.CACHE_DIR]


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _inside(path, root):
    p, r = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return p.startswith(r.rstrip("\\") + "\\")


class StepError(core.HubError):
    pass


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

RUN_KEY_TAIL = r"Microsoft\Windows\CurrentVersion\Run"
PROTECTED_KEYS = {"microsoft", "classes", "policies", "wow6432node", "clients", "registeredapplications",
                  "odbc", "app hub", "windows"}
REG_BACKUPS = os.path.join(core.DATA_DIR, "registry-backups")


def _winreg():
    import winreg
    return winreg


def _check_reg_path(hive, path):
    parts = [p for p in path.split("\\") if p]
    if hive not in ("HKCU", "HKLM64", "HKLM32") or len(parts) < 2 or parts[0].lower() != "software" \
            or any(p in (".", "..") for p in parts):
        raise StepError(f"refusing to delete {hive}\\{path}")
    if parts[1].lower() == "microsoft":
        # Only one app's own entry under Uninstall or App Paths.
        tail = [p.lower() for p in parts[2:5]]
        if not (len(parts) == 6 and tail[:3] == ["windows", "currentversion", tail[2]]
                and tail[2] in ("uninstall", "app paths")):
            raise StepError(f"refusing to delete {hive}\\{path}")
    elif parts[1].lower() in PROTECTED_KEYS:
        raise StepError(f"refusing to delete {hive}\\{path}")


def _reg_name(hive, path):
    return ("HKCU\\" if hive == "HKCU" else "HKLM\\") + path


def _reg_view(hive):
    return ["/reg:32"] if hive == "HKLM32" else ["/reg:64"] if hive == "HKLM64" else []


def _reg_exists(hive, path):
    return winsys.run(["reg.exe", "query", _reg_name(hive, path), *_reg_view(hive)], timeout=30).returncode == 0


def _reg_backup(hive, path, log):
    """Export a key to a .reg file before changing it. Returns the file ('' if it failed)."""
    folder = os.path.join(REG_BACKUPS, time.strftime("%Y-%m-%d"))
    os.makedirs(folder, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{hive}_{path}")[-120:]
    out = os.path.join(folder, f"{time.strftime('%H%M%S')}_{name}.reg")
    r = winsys.run(["reg.exe", "export", _reg_name(hive, path), out, "/y", *_reg_view(hive)], timeout=60)
    if r.returncode:
        log(f"warning: no backup of {hive}\\{path}")
        return ""
    return out


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

def _delete_old(folder, days, log):
    cutoff = time.time() - days * 86400
    freed = removed = skipped = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(folder, name)
        if folder == core.CACHE_DIR and name in ("api-cache.json",):
            continue
        try:
            st = os.lstat(path)
            if st.st_mtime > cutoff:
                continue
            if os.path.isdir(path) and not os.path.islink(path):
                size = sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(path) for f in fs
                           if os.path.exists(os.path.join(r, f)))
                shutil.rmtree(path)
            else:
                size = st.st_size
                os.unlink(path)
            freed += size
            removed += 1
        except OSError:
            skipped += 1                  # in use: Windows keeps it
    log(f"{folder}: removed {removed} item(s), {core.human_size(freed)}"
        + (f"; {skipped} in use were kept" if skipped else ""))
    return 0


def _wait_gone(entry, seconds=60):
    from . import inventory
    for _ in range(seconds * 2):
        inventory.forget_arp()
        if inventory.read_entry(entry.hive, entry.key) is None:
            return True
        time.sleep(0.5)
    return False


def _run_winget(args, log):
    exe = winget.exe()
    if not exe:
        raise StepError("winget is not installed (get “App Installer” from the Microsoft Store)")
    rc, _ = core.run_command([exe, *args[1:]], log)
    if rc and winget.explain(rc):
        log(f"winget: {winget.explain(rc)}")
    if (rc & 0xFFFFFFFF) == 0x8A15002B:    # nothing newer: not a failure
        return 0
    return rc


def run_step(step, log, elevated):
    kind = step[0]
    if kind in USER_ONLY and elevated:
        raise StepError(f"{kind} runs only without administrator rights")

    if kind == "install":
        _, itype, path, label = step
        if not (_inside(path, core.CACHE_DIR) and os.path.isfile(path)):
            raise StepError(f"refusing to run {path}: not an App Hub download")
        log(f"Installing {label} ({installer.KINDS.get(itype, itype)})")
        rc, _ = core.run_command(installer.silent_command(itype, path), log)
        if rc in (3010, 1641):
            log(f"{label}: restart Windows to finish")
        return 0 if rc in installer.OK_CODES else rc

    if kind == "portable":
        _, path, app_id, name, version, exe, publisher = step
        installer.install_portable(path, app_id, name, version, exe, publisher, log)
        return 0

    if kind == "uninstall":
        from . import inventory
        _, ref, label = step
        entry = inventory.find_entry(ref)
        if entry is None:
            log(f"{label} is not installed any more")
            return 0
        cmd, quiet = inventory.uninstall_command(entry)
        log(f"Uninstalling {label}" + ("" if quiet else " (its uninstaller opens a window)"))
        rc, _ = core.run_command(cmd, log)
        if rc in (0, 3010, 1641, 1605):
            if quiet and not _wait_gone(entry):
                log(f"{label}: the uninstaller is still finishing")
            return 0
        return rc

    if kind == "winget":
        _, action, package_id, source, label = step
        if action not in ("install", "upgrade", "uninstall") or not winget.ID_RE.match(package_id):
            raise StepError(f"bad winget step: {step}")
        log(f"{action.capitalize()} {label} with winget")
        cmd = {"install": winget.install_cmd, "upgrade": winget.upgrade_cmd,
               "uninstall": winget.uninstall_cmd}[action](package_id, source)
        return _run_winget(cmd, log)

    if kind == "winget-upgrade-all":
        log("Updating every app winget knows")
        return _run_winget(winget.upgrade_all_cmd(), log)

    if kind == "store-remove":
        _, full, label = step
        if not re.fullmatch(r"[\w.~-]+_[\w.~-]+", full):
            raise StepError(f"bad package name {full}")
        log(f"Removing {label}")
        r = winsys.powershell("Remove-AppxPackage -Package $env:AH_PKG", env=dict(os.environ, AH_PKG=full))
        if r.returncode:
            log(r.stdout.strip() or r.stderr.strip())
        return r.returncode

    if kind == "store-add":
        _, path, label = step
        log(f"Installing {label}")
        r = winsys.powershell("Add-AppxPackage -Path $env:AH_PKG -ForceApplicationShutdown",
                              timeout=600, env=dict(os.environ, AH_PKG=path))
        if r.returncode:
            log(r.stdout.strip() or r.stderr.strip())
        return r.returncode

    if kind == "remove-portable":
        return installer.remove_portable(step[1], log)

    if kind == "delete-old":
        _, folder, days = step
        allowed = user_clean_folders() + (sum(CLEAN_TARGETS.values(), []) if elevated else [])
        if not any(_same(folder, a) for a in allowed):
            raise StepError(f"not a folder App Hub cleans: {folder}")
        return _delete_old(folder, int(days), log)

    if kind == "clean":
        what = step[1]
        if what == "delivery-optimization":
            log("Clearing the Delivery Optimization cache")
            r = winsys.powershell("Delete-DeliveryOptimizationCache -Force", timeout=300)
            return 0 if r.returncode == 0 else r.returncode
        if what not in CLEAN_TARGETS:
            raise StepError(f"unknown clean target {what}")
        for folder in CLEAN_TARGETS[what]:
            _delete_old(folder, 1 if what == "windows-temp" else 0, log)
        return 0

    if kind == "recycle":
        path = step[1]
        name = os.path.basename(os.path.normpath(path)).lower()
        shortcut = name.endswith(".lnk") and any(_inside(path, r) for r in SHORTCUT_ROOTS)
        if not shortcut and (not any(_inside(path, r) for r in LEFTOVER_ROOTS) or never_remove(path)
                             or any(_same(path, r) for r in LEFTOVER_ROOTS)):
            raise StepError(f"refusing to remove {path}")
        if not os.path.exists(path):
            return 0
        ok = winsys.recycle([path])
        log(("moved to the Recycle Bin: " if ok else "could not remove (in use?): ") + path)
        return 0 if ok else 1

    if kind == "reg-delete":
        _, hive, path = step
        _check_reg_path(hive, path)
        if not _reg_exists(hive, path):
            return 0
        backup = _reg_backup(hive, path, log)
        r = winsys.run(["reg.exe", "delete", _reg_name(hive, path), "/f", *_reg_view(hive)], timeout=60)
        if r.returncode:
            log(f"could not delete {hive}\\{path}: {(r.stderr or r.stdout).strip()}")
            return 1
        log(f"deleted registry key {hive}\\{path}" + (f" (backup: {backup})" if backup else ""))
        return 0

    if kind == "reg-delete-value":
        _, hive, path, name = step
        if path.lower().split("\\", 1)[1:] != [RUN_KEY_TAIL.lower()]:
            raise StepError(f"refusing to change {hive}\\{path}")
        backup = _reg_backup(hive, path, log)
        r = winsys.run(["reg.exe", "delete", _reg_name(hive, path), "/v", name, "/f", *_reg_view(hive)], timeout=60)
        log((f"removed start-up entry {name}" + (f" (backup: {backup})" if backup else "")) if r.returncode == 0
            else f"could not remove start-up entry {name}")
        return r.returncode

    if kind == "service-delete":
        name = step[1]
        if not re.fullmatch(r"[\w.\-]+", name):
            raise StepError(f"bad service name {name}")
        image = str(winsys.reg_value(_winreg().HKEY_LOCAL_MACHINE,
                                     rf"SYSTEM\CurrentControlSet\Services\{name}", "ImagePath", "") or "")
        if not image or "\\windows\\" in image.lower() or image.lower().startswith(("system32", "\\systemroot")):
            raise StepError(f"refusing to remove service {name}: it belongs to Windows")
        winsys.run(["sc.exe", "stop", name], timeout=60)
        r = winsys.run(["sc.exe", "delete", name], timeout=60)
        log(f"removed service {name}" if r.returncode == 0 else f"could not remove service {name}: {r.stdout.strip()}")
        return r.returncode

    if kind == "task-delete":
        path = step[1]
        if not path.startswith("\\") or path.lower().startswith("\\microsoft\\"):
            raise StepError(f"refusing to remove task {path}")
        r = winsys.run(["schtasks.exe", "/Delete", "/TN", path, "/F"], timeout=60)
        log(f"removed scheduled task {path}" if r.returncode == 0 else f"could not remove task {path}")
        return r.returncode

    if kind == "restore-point":
        label = step[1][:200]
        log("Creating a system restore point")
        r = winsys.powershell("Checkpoint-Computer -Description $env:AH_DESC -RestorePointType APPLICATION_UNINSTALL "
                              "-WarningVariable w -WarningAction SilentlyContinue; if ($w) { $w }", timeout=600,
                              env=dict(os.environ, AH_DESC=label))
        note = (r.stdout + r.stderr).strip()
        if r.returncode:
            log("no restore point: " + (note.splitlines()[0] if note else "System Protection may be off for drive C:"))
        elif note:
            log("restore point skipped: " + note.splitlines()[0])
        else:
            log("restore point created")
        return 0                # never block the uninstall on it

    if kind == "empty-recycle-bin":
        size, items = winsys.recycle_bin_size()
        winsys.empty_recycle_bin()
        log(f"Recycle Bin emptied ({items} item(s), {core.human_size(size)})")
        return 0

    raise StepError(f"unknown step {kind}")


def run_plan(plan, log, elevated=False):
    keep_going = any(s and s[0] == "keep-going" for s in plan)
    failed = 0
    for step in plan:
        if not step or step[0] == "keep-going":
            continue
        try:
            rc = run_step([str(x) for x in step], log, elevated)
        except (core.HubError, OSError, ValueError) as e:
            log(f"error: {e}")
            rc = 1
        if rc:
            failed = rc
            if not keep_going:
                return rc
    return 1 if failed else 0


# --------------------------------------------------------------------------
# running a plan elevated (one UAC prompt)
# --------------------------------------------------------------------------

def run_elevated(plan, log):
    """Run `plan` as administrator, streaming its log. Returns the exit code
    (1223 when the UAC prompt was declined)."""
    if winsys.is_elevated():
        return run_plan(plan, log, elevated=True)
    os.makedirs(RUN_DIR, exist_ok=True)
    stamp = f"{os.getpid()}-{int(time.time() * 1000)}"
    plan_path = os.path.join(RUN_DIR, f"plan-{stamp}.json")
    log_path = os.path.join(RUN_DIR, f"log-{stamp}.txt")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f)
    open(log_path, "w", encoding="utf-8").close()
    cmd = winsys.launcher() + ["--helper", plan_path, "--log", log_path]
    log("Asking Windows for administrator permission (one prompt for every step)")
    try:
        handle = winsys.shell_execute(cmd[0], winsys.quote_args(cmd[1:]), "runas",
                                      directory=None if winsys.FROZEN else winsys.source_dir())
    except winsys.ElevationCancelled:
        log("Administrator permission was not given")
        core._unlink(plan_path)
        core._unlink(log_path)
        return 1223
    except OSError as e:
        log(f"could not start the helper: {e}")
        return 1
    pos, pending = 0, ""
    try:
        while True:
            done = winsys.wait_process(handle, 250)
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except OSError:
                chunk = ""
            pending += chunk
            *lines, pending = pending.split("\n")
            for line in lines:
                if line.strip():
                    log(line.rstrip())
            if done:
                break
        if pending.strip():
            log(pending.rstrip())
        return winsys.exit_code(handle)
    finally:
        winsys.close_handle(handle)
        core._unlink(plan_path)
        core._unlink(log_path)


def execute(admin_plan, user_plan, local, log, admin_first=False):
    """local(log, admin_plan, user_plan) -> rc runs first (downloads; it may add install steps
    to the plans), then the user plan, then the admin plan with one UAC prompt (or the admin
    plan first, e.g. for a restore point before anything changes). Returns (rc, output)."""
    lines = []
    admin_plan, user_plan = list(admin_plan or []), list(user_plan or [])

    def emit(line):
        lines.append(line)
        log(line)

    if local:
        try:
            rc = local(emit, admin_plan, user_plan)
        except core.HubError as e:
            emit(str(e))
            rc = 1
        if rc:
            return rc, "\n".join(lines)
    failed = 0
    has_user = any(s[0] != "keep-going" for s in user_plan)
    has_admin = any(s[0] != "keep-going" for s in admin_plan)
    if has_user and not admin_first:
        failed = run_plan(user_plan, emit, elevated=False)
    if has_admin:
        rc = run_elevated(admin_plan, emit)
        if rc in core.AUTH_CANCELLED:
            return rc, "\n".join(lines)
        failed = failed or rc
    if has_user and admin_first:
        failed = run_plan(user_plan, emit, elevated=False) or failed
    return (1 if failed else 0), "\n".join(lines)


def combine(actions):
    """[(admin_plan, user_plan, local), ...] -> one (admin_plan, user_plan, local) that keeps going."""
    admin, user, locals_ = [["keep-going"]], [["keep-going"]], []
    for a, u, loc in actions:
        admin += [s for s in a if s[0] != "keep-going"]
        user += [s for s in u if s[0] != "keep-going"]
        if loc:
            locals_.append(loc)

    def local(log, admin_plan, user_plan):
        failed = 0
        for fn in locals_:
            try:
                failed |= bool(fn(log, admin_plan, user_plan))
            except core.HubError as e:
                log(str(e))
                failed = 1
        steps = sum(1 for s in admin_plan + user_plan if s[0] != "keep-going")
        return 1 if failed and not steps else 0       # keep going with what did download
    return admin, user, local if locals_ else None


def main(plan_path, log_path):
    """Entry point of the elevated helper process."""
    with open(log_path, "a", encoding="utf-8", buffering=1) as out:
        def log(line):
            out.write(line + "\n")
            out.flush()
        try:
            with open(plan_path, encoding="utf-8") as f:
                plan = json.load(f)
            if not isinstance(plan, list) or not all(isinstance(s, list) and s for s in plan):
                raise StepError("bad plan")
        except (OSError, ValueError, StepError) as e:
            log(f"error: {e}")
            return 2
        if not winsys.is_elevated():
            log("error: the helper needs administrator rights")
            return 2
        return run_plan(plan, log, elevated=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
