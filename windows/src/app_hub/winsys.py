"""Windows pieces in one place: folders, processes, UAC, Recycle Bin, icons and theme.

Everything here uses the standard library (ctypes, winreg), so core, the CLI and the
elevated helper can use it without GTK.
"""

import ctypes
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import zlib
from ctypes import wintypes

IS_WIN = os.name == "nt"
FROZEN = getattr(sys, "frozen", False)          # running from a PyInstaller build
NO_WINDOW = 0x08000000                          # CREATE_NO_WINDOW: no console flashing up

if IS_WIN:
    import winreg
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)


class ElevationCancelled(Exception):
    """The user said No (or nobody answered) at the UAC prompt."""


# --------------------------------------------------------------------------
# processes
# --------------------------------------------------------------------------

def popen_kwargs():
    return {"creationflags": NO_WINDOW} if IS_WIN else {}


def run(cmd, timeout=60, input=None, env=None, cwd=None):
    """subprocess.run with UTF-8 text, no console window and no exceptions."""
    extra = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, cwd=cwd, **extra, **popen_kwargs())
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def powershell(script, timeout=120, env=None):
    """Run a PowerShell snippet; its output comes back as UTF-8 text."""
    full = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $ProgressPreference = 'SilentlyContinue'; " + script
    return run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-Command", full], timeout=timeout, env=env)


def which(name):
    return shutil.which(name)


def launcher():
    """How to start App Hub again: [program, args...] (used for the elevated helper)."""
    if FROZEN:
        return [sys.executable]
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return [exe, "-m", "app_hub"]


def source_dir():
    """Folder that holds the app_hub package (for `python -m app_hub` from source)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# folders
# --------------------------------------------------------------------------

HOME = os.path.expanduser("~")
LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
APPDATA = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
PROGRAMDATA = os.environ.get("ProgramData") or r"C:\ProgramData"
PROGRAM_FILES = [p for p in dict.fromkeys([os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles"),
                                           os.environ.get("ProgramFiles(x86)")]) if p]
WINDIR = os.environ.get("SystemRoot") or r"C:\Windows"
SYSTEM_DRIVE = (os.environ.get("SystemDrive") or "C:") + "\\"


def known_folder(guid, fallback):
    """SHGetKnownFolderPath, so a moved Downloads/Documents folder is respected."""
    if IS_WIN:
        try:
            import uuid

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]

            u = uuid.UUID(guid)
            g = GUID(u.time_low, u.time_mid, u.time_hi_version,
                     (wintypes.BYTE * 8).from_buffer_copy(u.bytes[8:]))
            out = ctypes.c_wchar_p()
            if shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
                path = out.value
                ctypes.windll.ole32.CoTaskMemFree(out)
                return path
        except Exception:
            pass
    return os.path.join(HOME, fallback)


def config_dir():
    return os.path.join(APPDATA, "App Hub")


def data_dir():
    return os.path.join(LOCALAPPDATA, "App Hub")


def cache_dir():
    return os.path.join(data_dir(), "cache")


def downloads_dir():
    return known_folder("374DE290-123F-4565-9164-39C4925E467B", "Downloads")


def documents_dir():
    return known_folder("FDD39AD0-238F-46AF-ADB4-6C85480369C7", "Documents")


def start_menu_dirs():
    return [os.path.join(APPDATA, r"Microsoft\Windows\Start Menu\Programs"),
            os.path.join(PROGRAMDATA, r"Microsoft\Windows\Start Menu\Programs")]


def portable_root():
    """Where App Hub unpacks apps that ship as a .zip or a single .exe."""
    return os.path.join(LOCALAPPDATA, "Programs", "App Hub Apps")


def open_path(target):
    """Open a file, folder or URL with its default program."""
    os.startfile(target)  # noqa: S606 - opening what the user clicked is the point


def reveal(path):
    """Open Explorer with the file selected."""
    if os.path.exists(path):
        subprocess.Popen(["explorer.exe", "/select,", path], **popen_kwargs())
    elif os.path.isdir(os.path.dirname(path)):
        open_path(os.path.dirname(path))


def disk_usage(path):
    try:
        u = shutil.disk_usage(path)
        return u.free, u.total
    except OSError:
        return 0, 0


# --------------------------------------------------------------------------
# machine
# --------------------------------------------------------------------------

_arch = None


def machine_arch():
    """'x64', 'arm64' or 'x86': the machine's own architecture (not this process's)."""
    global _arch
    if _arch:
        return _arch
    native = 0
    if IS_WIN:
        try:
            proc, nat = ctypes.c_ushort(), ctypes.c_ushort()
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.IsWow64Process2.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_ushort),
                                                 ctypes.POINTER(ctypes.c_ushort)]
            if kernel32.IsWow64Process2(kernel32.GetCurrentProcess(), ctypes.byref(proc), ctypes.byref(nat)):
                native = nat.value
        except (AttributeError, OSError):
            native = 0
    if native:
        _arch = {0x8664: "x64", 0xAA64: "arm64", 0x14C: "x86"}.get(native, "x64")
    else:
        env = (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
        _arch = {"AMD64": "x64", "ARM64": "arm64", "X86": "x86"}.get(env, "x64")
    return _arch


def windows_version():
    """'Windows 11 24H2 (26100)' style text."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            build = int(winreg.QueryValueEx(k, "CurrentBuildNumber")[0])
            try:
                display = winreg.QueryValueEx(k, "DisplayVersion")[0]
            except OSError:
                display = ""
    except (OSError, ValueError, NameError):
        return "Windows"
    name = "Windows 11" if build >= 22000 else "Windows 10"
    return f"{name} {display} ({build})".replace("  ", " ")


# --------------------------------------------------------------------------
# UAC: one elevated process for everything that needs administrator rights
# --------------------------------------------------------------------------

class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
ERROR_CANCELLED = 1223

if IS_WIN:
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


def is_elevated():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def elevation_type():
    """1 = no split token (standard user, or UAC off), 2 = elevated, 3 = admin running limited."""
    try:
        token = wintypes.HANDLE()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return 1
        value, size = wintypes.DWORD(), wintypes.DWORD()
        ok = advapi32.GetTokenInformation(token, 18, ctypes.byref(value), 4, ctypes.byref(size))
        kernel32.CloseHandle(token)
        return value.value if ok else 1
    except (AttributeError, OSError):
        return 1


def elevates_as_self():
    """True when a UAC prompt keeps this same user account (an administrator), so per-user
    installers run elevated still install for this user. A standard user's prompt would
    switch to another account, so per-user work must then stay unelevated."""
    return is_elevated() or elevation_type() == 3


def shell_execute(program, params="", verb="runas", directory=None, show=0):
    """Start a program through the shell; verb "runas" shows the UAC prompt.
    Returns the process handle (wait with wait_process, then close_handle)."""
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = verb
    info.lpFile = program
    info.lpParameters = params
    info.lpDirectory = directory
    info.nShow = show
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            raise ElevationCancelled()
        raise OSError(err, ctypes.FormatError(err))
    return info.hProcess


def wait_process(handle, timeout_ms):
    """True when the process has ended."""
    return kernel32.WaitForSingleObject(handle, timeout_ms) == 0


def exit_code(handle):
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    return code.value


def close_handle(handle):
    if handle:
        kernel32.CloseHandle(handle)


def quote_args(args):
    return subprocess.list2cmdline([str(a) for a in args])


# --------------------------------------------------------------------------
# running programs (an app must be closed before it is updated or removed)
# --------------------------------------------------------------------------

def _norm_dir(folder):
    return os.path.normcase(os.path.abspath(folder)).rstrip("\\") + "\\"


def running_in(folder):
    """[(pid, program path)] of processes started from inside `folder` (App Hub excluded)."""
    if not IS_WIN or not folder or not os.path.isdir(folder):
        return []
    prefix = _norm_dir(folder)
    pids = (wintypes.DWORD * 8192)()
    needed = wintypes.DWORD()
    if not kernel32.K32EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        return []
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    out, me = [], os.getpid()
    for pid in pids[:needed.value // ctypes.sizeof(wintypes.DWORD)]:
        if not pid or pid == me:
            continue
        handle = kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) and \
                    os.path.normcase(buf.value).startswith(prefix):
                out.append((pid, buf.value))
        finally:
            kernel32.CloseHandle(handle)
    return out


def _alive(pid):
    handle = kernel32.OpenProcess(0x00100000, False, pid)       # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) != 0
    finally:
        kernel32.CloseHandle(handle)


def close_programs(pids, wait=10.0):
    """Ask the programs' windows to close (like clicking ×), then end what is still running
    after `wait` seconds. Returns the pids that had to be ended by force."""
    import time
    pids = set(pids)
    if not pids:
        return []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

    def each(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in pids and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, 0x0010, 0, 0)               # WM_CLOSE
        return True
    user32.EnumWindows(WNDENUMPROC(each), 0)
    deadline = time.time() + wait
    while time.time() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.25)
    forced = [p for p in pids if _alive(p)]
    for pid in forced:                                            # tray apps hide instead of closing
        run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], timeout=30)
    return forced


# --------------------------------------------------------------------------
# Recycle Bin
# --------------------------------------------------------------------------

class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p),
                ("pTo", ctypes.c_void_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def recycle(paths):
    """Move files or folders to the Recycle Bin (restorable). Returns True on success."""
    paths = [os.path.abspath(p) for p in paths if p and os.path.lexists(p)]
    if not paths:
        return True
    buf = ctypes.create_unicode_buffer("\0".join(paths) + "\0\0")
    op = SHFILEOPSTRUCTW()
    op.wFunc = 3                                    # FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_void_p)
    op.fFlags = 0x40 | 0x10 | 0x04 | 0x400 | 0x200  # ALLOWUNDO NOCONFIRMATION SILENT NOERRORUI NOCONFIRMMKDIR
    rc = shell32.SHFileOperationW(ctypes.byref(op))
    return rc == 0 and not op.fAnyOperationsAborted


def recycle_bin_size():
    """(bytes, items) in the Recycle Bins of all drives."""
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    try:
        if shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
            return info.i64Size, info.i64NumItems
    except (AttributeError, OSError):
        pass
    return 0, 0


def empty_recycle_bin():
    return shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4) in (0, -2147418113)  # S_OK, E_UNEXPECTED (empty)


# --------------------------------------------------------------------------
# registry helpers
# --------------------------------------------------------------------------

def reg_value(root, path, name, default=None, view=0):
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | view) as k:
            return winreg.QueryValueEx(k, name)[0]
    except (OSError, NameError):
        return default


# --------------------------------------------------------------------------
# theme
# --------------------------------------------------------------------------

def apps_use_dark():
    return reg_value(winreg.HKEY_CURRENT_USER if IS_WIN else None,
                     r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                     "AppsUseLightTheme", 1) == 0


def accent_color():
    """The Windows accent colour as '#rrggbb', or None."""
    value = reg_value(winreg.HKEY_CURRENT_USER if IS_WIN else None, r"Software\Microsoft\Windows\DWM",
                      "AccentColor")
    if not isinstance(value, int):
        return None
    r, g, b = value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF
    return f"#{r:02x}{g:02x}{b:02x}"


# --------------------------------------------------------------------------
# file version resource
# --------------------------------------------------------------------------

def version_info(path):
    """ProductName, ProductVersion, CompanyName, FileDescription, FileVersion of an .exe/.dll."""
    out = {}
    try:
        ver = ctypes.WinDLL("version")
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return out
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return out
        ptr, length = ctypes.c_void_p(), wintypes.UINT()
        ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT)]
        langs = []
        if ver.VerQueryValueW(buf, r"\VarFileInfo\Translation", ctypes.byref(ptr), ctypes.byref(length)):
            raw = ctypes.string_at(ptr, length.value)
            langs = [struct.unpack_from("<HH", raw, i) for i in range(0, len(raw) - 3, 4)]
        langs += [(0x0409, 0x04B0), (0x0409, 0x04E4)]
        for key in ("ProductName", "ProductVersion", "CompanyName", "FileDescription", "FileVersion"):
            for lang, cp in langs:
                if ver.VerQueryValueW(buf, rf"\StringFileInfo\{lang:04x}{cp:04x}\{key}",
                                      ctypes.byref(ptr), ctypes.byref(length)) and length.value:
                    out[key] = ctypes.wstring_at(ptr, length.value).rstrip("\0").strip()
                    break
    except (OSError, AttributeError):
        pass
    return out


def signature(path):
    """(status, signer) from Authenticode, e.g. ('Valid', 'Microsoft Corporation')."""
    env = dict(os.environ, APPHUB_FILE=path)
    r = powershell("$s = Get-AuthenticodeSignature -LiteralPath $env:APPHUB_FILE; "
                   "$n = if ($s.SignerCertificate) { $s.SignerCertificate.GetNameInfo('SimpleName', $false) } else { '' }; "
                   "Write-Output (\"{0}|{1}\" -f $s.Status, $n)", timeout=30, env=env)
    status, _, signer = r.stdout.strip().partition("|")
    return status or "Unknown", signer


# --------------------------------------------------------------------------
# shortcuts
# --------------------------------------------------------------------------

def create_shortcut(lnk, target, args="", workdir="", icon="", description=""):
    """Write a .lnk file (Start menu / desktop)."""
    os.makedirs(os.path.dirname(lnk), exist_ok=True)
    env = dict(os.environ, AH_LNK=lnk, AH_TARGET=target, AH_ARGS=args, AH_DIR=workdir or os.path.dirname(target),
               AH_ICON=icon or target, AH_DESC=description)
    r = powershell("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:AH_LNK); "
                   "$s.TargetPath = $env:AH_TARGET; $s.Arguments = $env:AH_ARGS; "
                   "$s.WorkingDirectory = $env:AH_DIR; $s.IconLocation = $env:AH_ICON; "
                   "$s.Description = $env:AH_DESC; $s.Save()", timeout=30, env=env)
    return r.returncode == 0 and os.path.exists(lnk)


# --------------------------------------------------------------------------
# icons of programs: .exe / .dll / .ico -> PNG (cached)
# --------------------------------------------------------------------------

class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG), ("bmHeight", wintypes.LONG),
                ("bmWidthBytes", wintypes.LONG), ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


if IS_WIN:
    user32.PrivateExtractIconsW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.UINT),
                                            wintypes.UINT, wintypes.UINT]
    user32.PrivateExtractIconsW.restype = wintypes.UINT
    user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]


def parse_icon_spec(spec):
    """'"C:\\x\\app.exe",0' / 'C:\\x\\app.ico' -> (path, index)."""
    spec = (spec or "").strip()
    index = 0
    if spec.startswith('"'):
        end = spec.find('"', 1)
        path, rest = spec[1:end], spec[end + 1:]
        rest = rest.strip().lstrip(",").strip()
        if rest.lstrip("-").isdigit():
            index = int(rest)
    else:
        head, sep, tail = spec.rpartition(",")
        if sep and tail.strip().lstrip("-").isdigit():
            path, index = head, int(tail.strip())
        else:
            path = spec
    return os.path.expandvars(path.strip().strip('"')), index


def _bits(hdc, hbm, w, h):
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(bmi)
    bmi.biWidth, bmi.biHeight, bmi.biPlanes, bmi.biBitCount = w, -h, 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    if not gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bmi), 0):
        return None
    return buf.raw


def _hicon_rgba(hicon):
    info = ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(info)):
        return None
    try:
        if not info.hbmColor:
            return None
        bm = BITMAP()
        gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm))
        w, h = bm.bmWidth, abs(bm.bmHeight)
        hdc = user32.GetDC(None)
        try:
            bgra = _bits(hdc, info.hbmColor, w, h)
            mask = _bits(hdc, info.hbmMask, w, h) if info.hbmMask else None
        finally:
            user32.ReleaseDC(None, hdc)
        if not bgra:
            return None
        px = bytearray(bgra)
        if not any(px[3::4]):           # old icon without alpha: use the AND mask
            alpha = bytes(0 if (mask and mask[i]) else 255 for i in range(0, len(px), 4))
            px[3::4] = alpha
        px[0::4], px[2::4] = bgra[2::4], bgra[0::4]       # BGRA -> RGBA
        return w, h, bytes(px)
    finally:
        if info.hbmColor:
            gdi32.DeleteObject(info.hbmColor)
        if info.hbmMask:
            gdi32.DeleteObject(info.hbmMask)


def png_bytes(w, h, rgba):
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def icon_png(spec, size=48):
    """PNG file with the icon of an .exe/.dll/.ico ('path,index'), cached; '' when there is none."""
    if not IS_WIN or not spec:
        return ""
    path, index = parse_icon_spec(spec)
    if path.lower().endswith(".png"):
        return path if os.path.exists(path) else ""
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return ""
    folder = os.path.join(data_dir(), "icon-cache")
    name = hashlib.sha1(f"{path.lower()}|{index}|{size}|{stamp}".encode()).hexdigest()[:20] + ".png"
    out = os.path.join(folder, name)
    if os.path.exists(out):
        return out
    hicon, icon_id = wintypes.HICON(), wintypes.UINT()
    try:
        n = user32.PrivateExtractIconsW(path, index, size, size, ctypes.byref(hicon), ctypes.byref(icon_id), 1, 0)
    except OSError:
        return ""
    if not n or n == 0xFFFFFFFF or not hicon:
        return ""
    try:
        got = _hicon_rgba(hicon)
    finally:
        user32.DestroyIcon(hicon)
    if not got:
        return ""
    os.makedirs(folder, exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "wb") as f:
        f.write(png_bytes(*got))
    os.replace(tmp, out)
    return out
