"""Every installed app on Windows, whatever installed it.

Sources: the Uninstall registry keys (what Settings ▸ Apps lists), Microsoft Store /
MSIX apps (Get-StartApps + Get-AppxPackage), and apps App Hub unpacked itself
("portable"). winget adds package ids and available updates.
"""

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

from . import core, winget, winsys

if winsys.IS_WIN:
    import winreg

CHANNELS = {"desktop": "Desktop", "store": "Store", "portable": "Portable"}
UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
PORTABLE_PREFIX = "AppHub_"
# Never offered for removal: Windows itself, the package manager, App Hub.
PROTECTED_NAMES = re.compile(
    r"^(App Hub|App Installer|Microsoft Edge( WebView2 Runtime)?|Windows .*Runtime|Microsoft Store|"
    r"Microsoft Update Health Tools|Windows PC Health Check|Update for Windows.*)$", re.IGNORECASE)
PROTECTED_STORE = re.compile(r"^(Microsoft\.(DesktopAppInstaller|WindowsStore|StorePurchaseApp|"
                             r"SecHealthUI|Windows\.ShellExperienceHost|WindowsTerminal))_", re.IGNORECASE)


# --------------------------------------------------------------------------
# registry: Programs and Features
# --------------------------------------------------------------------------

@dataclass
class Arp:
    """One Uninstall registry entry."""
    hive: str                 # HKLM64 | HKLM32 | HKCU
    key: str                  # sub key name ({GUID}, {GUID}_is1, Markd, ...)
    name: str
    version: str = ""
    publisher: str = ""
    location: str = ""
    icon: str = ""            # DisplayIcon: "path,index"
    uninstall: str = ""
    quiet_uninstall: str = ""
    size: int = 0             # bytes (EstimatedSize)
    installed_on: str = ""    # YYYYMMDD
    msi: bool = False
    no_remove: bool = False

    @property
    def ref(self):
        return f"{self.hive}\\{self.key}"

    @property
    def machine(self):
        return self.hive.startswith("HKLM")

    @property
    def portable(self):
        return self.hive == "HKCU" and self.key.startswith(PORTABLE_PREFIX)


HIVES = {"HKLM64": ("HKEY_LOCAL_MACHINE", 0x0100), "HKLM32": ("HKEY_LOCAL_MACHINE", 0x0200),
         "HKCU": ("HKEY_CURRENT_USER", 0)}


def _hive(name):
    root, view = HIVES[name]
    return getattr(winreg, root), view


def _val(k, name, default=""):
    try:
        return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return default


def read_entry(hive, key):
    """One Arp entry by hive and key name, or None."""
    root, view = _hive(hive)
    try:
        with winreg.OpenKey(root, f"{UNINSTALL}\\{key}", 0, winreg.KEY_READ | view) as k:
            name = str(_val(k, "DisplayName") or "").strip()
            if not name:
                return None
            size = _val(k, "EstimatedSize", 0)
            return Arp(
                hive=hive, key=key, name=name, version=str(_val(k, "DisplayVersion") or "").strip(),
                publisher=str(_val(k, "Publisher") or "").strip(),
                location=str(_val(k, "InstallLocation") or "").strip().strip('"'),
                icon=str(_val(k, "DisplayIcon") or "").strip(),
                uninstall=str(_val(k, "UninstallString") or "").strip(),
                quiet_uninstall=str(_val(k, "QuietUninstallString") or "").strip(),
                size=int(size) * 1024 if isinstance(size, int) else 0,
                installed_on=str(_val(k, "InstallDate") or ""),
                msi=_val(k, "WindowsInstaller", 0) == 1,
                no_remove=_val(k, "NoRemove", 0) == 1,
            ), (_val(k, "SystemComponent", 0) == 1 or bool(_val(k, "ParentKeyName", ""))
                or str(_val(k, "ReleaseType", "")) in ("Update", "Hotfix", "Security Update"))
    except OSError:
        return None


_arp_cache = (0.0, [])
_arp_lock = threading.Lock()


def read_arp(max_age=3.0):
    """Visible Uninstall entries of all three registry views (cached for a few seconds)."""
    global _arp_cache
    if not winsys.IS_WIN:
        return []
    with _arp_lock:
        if time.time() - _arp_cache[0] < max_age:
            return _arp_cache[1]
        out, seen = [], set()
        for hive in HIVES:
            root, view = _hive(hive)
            if hive == "HKLM32" and winsys.machine_arch() == "x86":
                continue
            try:
                k = winreg.OpenKey(root, UNINSTALL, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with k:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    got = read_entry(hive, sub)
                    if not got:
                        continue
                    entry, hidden = got
                    if hidden or re.match(r"^(KB\d+|Security Update|Hotfix|Update for)\b", entry.name):
                        continue
                    dedupe = (entry.machine, entry.key.lower())
                    if dedupe in seen:
                        continue
                    seen.add(dedupe)
                    out.append(entry)
        _arp_cache = (time.time(), out)
        return out


def forget_arp():
    global _arp_cache
    _arp_cache = (0.0, [])


def find_entry(ref):
    hive, _, key = ref.partition("\\")
    if hive not in HIVES or not key:
        return None
    got = read_entry(hive, key)
    return got[0] if got else None


VERSION_TAIL = re.compile(r"[\s(_-]+v?(\d+(?:\.\d+)+)(?:[\s)]|$).*$", re.IGNORECASE)


def base_name(name):
    """'Draco Download Manager version 1.8.6' -> 'Draco Download Manager'; drops (x64) etc."""
    n = re.sub(r"\s+version\s+\S+.*$", "", name, flags=re.IGNORECASE)
    n = re.sub(r"\s*\((x64|x86|64-bit|32-bit|64 bit|arm64|user|machine)\)", "", n, flags=re.IGNORECASE)
    n = VERSION_TAIL.sub("", n)
    return n.strip(" -") or name


def norm(name):
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


# --------------------------------------------------------------------------
# Start menu shortcuts: how apps are opened
# --------------------------------------------------------------------------

_SKIP_LNK = re.compile(r"uninstall|readme|read me|help|manual|website|documentation|release notes|"
                       r"license|changelog|repair|support|web site|\bfaq\b", re.IGNORECASE)
_lnk_cache = (0.0, {})


def shortcuts():
    """{normalized name: .lnk path} of the Start menu (user entries win)."""
    global _lnk_cache
    if time.time() - _lnk_cache[0] < 10:
        return _lnk_cache[1]
    found = {}
    for root in reversed(winsys.start_menu_dirs()):
        for folder, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith(".lnk") and not _SKIP_LNK.search(f):
                    found[norm(f[:-4])] = os.path.join(folder, f)
    _lnk_cache = (time.time(), found)
    return found


def shortcut_for(name, location=""):
    lnks = shortcuts()
    for candidate in (name, base_name(name)):
        hit = lnks.get(norm(candidate))
        if hit:
            return hit
    key = norm(base_name(name))
    if len(key) >= 4:
        best = [p for k, p in lnks.items() if k.startswith(key) or (len(k) >= 4 and key.startswith(k))]
        if best:
            return min(best, key=len)
    return ""


def main_exe(entry):
    """The program an Uninstall entry most likely starts (from DisplayIcon or its folder)."""
    path, _ = winsys.parse_icon_spec(entry.icon) if entry.icon else ("", 0)
    if path.lower().endswith(".exe") and os.path.exists(path) and not re.search(
            r"unins|uninstall|setup|install|update", os.path.basename(path), re.IGNORECASE):
        return path
    return ""


# --------------------------------------------------------------------------
# installed apps
# --------------------------------------------------------------------------

@dataclass
class SysApp:
    channel: str               # desktop | store | portable
    id: str                    # registry ref (HKCU\key) or the Store package full name
    name: str
    version: str = ""
    source: str = ""           # publisher
    size: int = 0
    icon: str = ""             # PNG file
    launch: str = ""           # .lnk / .exe / shell:AppsFolder\AUMID
    update: str = ""           # newer version, if any
    winget_id: str = ""        # winget package id, when winget knows the app
    winget_source: str = ""    # winget | msstore
    scope: str = ""            # machine | user
    location: str = ""
    protected: str = ""        # why it cannot be removed
    hub_id: str = ""           # App Hub catalog id
    installed_on: str = ""     # YYYY-MM-DD when Windows knows it
    extra: dict = field(default_factory=dict)

    @property
    def key(self):
        if self.channel == "store" and isinstance(self.extra, dict) and self.extra.get("family"):
            return f"store:{self.extra['family']}"      # stays the same across Store updates
        return f"{self.channel}:{self.id}"

    @property
    def channel_label(self):
        return CHANNELS.get(self.channel, self.channel)

    @property
    def machine(self):
        return self.scope == "machine"

    def subtitle(self):
        parts = [p for p in (self.source, self.version, core.human_size(self.size) if self.size else "",
                             f"installed {self.installed_on}" if self.installed_on else "") if p]
        if self.update:
            parts.append(f"update {self.update} available")
        return " · ".join(parts)


def _hub_lookup(catalog):
    return [(a, core.compile_match(a)) for a in catalog]


def list_desktop(catalog, with_icons=True):
    hub = _hub_lookup(catalog)
    apps = []
    for e in read_arp(max_age=0):
        portable = e.portable
        app = SysApp("portable" if portable else "desktop", e.ref, base_name(e.name) if not portable else e.name,
                     version=e.version or core.version_from_name(e.name), source=e.publisher, size=e.size,
                     scope="machine" if e.machine else "user", location=e.location)
        app.extra = {"arp": e}
        if re.fullmatch(r"(19|20)\d{6}", e.installed_on):
            app.installed_on = f"{e.installed_on[:4]}-{e.installed_on[4:6]}-{e.installed_on[6:]}"
        if e.no_remove or not e.uninstall:
            app.protected = "Windows says this app cannot be removed here"
        elif PROTECTED_NAMES.match(app.name) or PROTECTED_NAMES.match(e.name):
            app.protected = "Part of Windows or App Hub"
        for a, rx in hub:
            if rx.search(e.name) or (a.uninstall_key and a.uninstall_key.lower() == e.key.lower()):
                app.hub_id = a.id
                break
        exe = main_exe(e)
        app.launch = shortcut_for(e.name) or exe
        if with_icons:
            app.icon = winsys.icon_png(e.icon) if e.icon else ""
            if not app.icon and exe:
                app.icon = winsys.icon_png(exe)
        apps.append(app)
    return apps


_STORE_PS = r"""
$apps = @(Get-StartApps | Where-Object { $_.AppID -like '*!*' } | ForEach-Object { @{ n = $_.Name; a = $_.AppID } })
$pk = @{}
Get-AppxPackage -PackageTypeFilter Main | ForEach-Object {
  $pk[$_.PackageFamilyName] = @{ f = $_.PackageFullName; v = $_.Version; p = $_.Publisher;
    l = $_.InstallLocation; s = [string]$_.SignatureKind; r = $_.NonRemovable; fw = $_.IsFramework }
}
@{ apps = $apps; packages = $pk } | ConvertTo-Json -Depth 4 -Compress
"""


def _publisher(dn):
    m = re.search(r"CN=([^,]+)", dn or "")
    name = (m.group(1) if m else dn or "").strip().strip('"')
    return "" if re.fullmatch(r"[0-9A-F-]{20,}", name) else name


def _store_logo(location):
    """Best-looking logo file of an MSIX package (from AppxManifest.xml)."""
    try:
        with open(os.path.join(location, "AppxManifest.xml"), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    paths = re.findall(r'Square44x44Logo="([^"]+)"', text) + re.findall(r"<Logo>([^<]+)</Logo>", text)
    for rel in paths:
        base = os.path.join(location, rel.replace("/", "\\"))
        stem, ext = os.path.splitext(base)
        candidates = [f"{stem}.targetsize-48{ext}", f"{stem}.targetsize-48_altform-unplated{ext}",
                      f"{stem}.scale-200{ext}", f"{stem}.scale-150{ext}", f"{stem}.scale-100{ext}", base]
        for c in candidates:
            if os.path.exists(c):
                return c
        folder = os.path.dirname(base)
        try:
            pick = sorted((f for f in os.listdir(folder) if f.startswith(os.path.basename(stem) + ".")
                           and f.endswith(ext)), key=len)
        except OSError:
            pick = []
        if pick:
            return os.path.join(folder, pick[0])
    return ""


def list_store(with_icons=True):
    r = winsys.powershell(_STORE_PS, timeout=90)
    try:
        data = json.loads(r.stdout.strip() or "{}")
    except ValueError:
        return []
    packages = data.get("packages") or {}
    apps, seen = [], set()
    entries = data.get("apps") or []
    if isinstance(entries, dict):
        entries = [entries]
    for e in entries:
        aumid = e.get("a", "")
        family = aumid.split("!")[0]
        pkg = packages.get(family)
        if not pkg or pkg.get("fw") or pkg.get("s") == "System" or family in seen:
            continue
        seen.add(family)
        full = pkg.get("f", "")
        app = SysApp("store", full, e.get("n") or family, version=pkg.get("v", ""),
                     source=_publisher(pkg.get("p")), launch=f"shell:AppsFolder\\{aumid}", scope="user",
                     location=pkg.get("l") or "")
        app.extra = {"family": family, "aumid": aumid}
        if pkg.get("r") or PROTECTED_STORE.match(full):
            app.protected = "Part of Windows"
        if with_icons and app.location:
            app.icon = _store_logo(app.location)
        apps.append(app)
    return apps


def list_all(catalog=None, store=True):
    """Installed apps, sorted by name (no winget information yet)."""
    catalog = catalog if catalog is not None else core.load_catalog()
    apps = list_desktop(catalog)
    if store:
        try:
            apps += list_store()
        except Exception:  # noqa: BLE001 - the Store list is a bonus
            pass
    apps.sort(key=lambda a: a.name.lower())
    return apps


def apply_winget(apps):
    """Fill winget ids and available updates from `winget list`. Returns winget rows that
    match no listed app but have an update (runtimes, drivers): 'system updates'."""
    rows = winget.list_installed()
    if not rows:
        return []
    by_ref = {}
    for a in apps:
        if a.channel in ("desktop", "portable"):
            by_ref[("ARP", a.extra["arp"].machine, a.extra["arp"].key.lower())] = a
        elif a.channel == "store":
            by_ref[("MSIX", a.id.lower())] = a
    by_name = {}
    for a in apps:
        by_name.setdefault(norm(a.name), a)
        if a.channel != "store":
            by_name.setdefault(norm(a.extra["arp"].name), a)
    leftover = []
    for row in rows:
        rid = row["id"]
        app = None
        if rid.upper().startswith("ARP\\"):
            parts = rid.split("\\")
            if len(parts) >= 4:
                app = by_ref.get(("ARP", parts[1].lower() == "machine", parts[3].lower()))
        elif rid.upper().startswith("MSIX\\"):
            app = by_ref.get(("MSIX", rid[5:].lower()))
        if app is None:
            name = row["name"].rstrip("…").rstrip(".")
            app = by_name.get(norm(name))
            if app is None and row["name"].endswith("…"):
                key = norm(name)
                app = next((a for k, a in by_name.items() if key and k.startswith(key)), None)
        known = row["source"] in ("winget", "msstore") and winget.ID_RE.match(rid)
        if app is not None:
            if known:
                app.winget_id, app.winget_source = rid, row["source"]
            if row["available"] and known and core.is_newer(row["available"], row["version"] or app.version):
                app.update = row["available"]
        elif known and row["available"]:
            leftover.append(row)
    return leftover


NAME_CACHE = os.path.join(core.DATA_DIR, "winget-name-matches.json")
NAME_CACHE_DAYS = 2


def match_by_name(apps, on_found=None):
    """Desktop apps winget did not link to a package: look the name up in winget (exact name,
    one result only). A match gets winget_id and extra['by_name']; its update installs the
    newest version over the old one. Answers are cached for two days."""
    try:
        with open(NAME_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    now = time.time()
    changed = False
    for app in apps:
        if app.channel != "desktop" or app.winget_id or app.hub_id or app.protected:
            continue
        name = base_name(app.name)
        key = norm(name)
        if len(key) < 4:
            continue
        hit = cache.get(key)
        if not hit or now - hit.get("time", 0) > NAME_CACHE_DAYS * 86400:
            rows = [r for r in winget.search(name, timeout=60, exact_name=True) if r["source"] in ("winget", "")
                    and norm(r["name"]) in (key, norm(app.name)) and winget.ID_RE.match(r["id"])]
            hit = {"id": rows[0]["id"], "version": rows[0]["version"]} if len(rows) == 1 else {}
            hit["time"] = now
            cache[key] = hit
            changed = True
        if hit.get("id"):
            app.winget_id, app.winget_source = hit["id"], "winget"
            app.extra["by_name"] = True
            latest = hit.get("version", "")
            # '9.0.30729.6161' over '9.0.30729' is the same build written more precisely.
            if latest and app.version and core.is_newer(latest, app.version) \
                    and not latest.startswith(app.version + "."):
                app.update = latest
            if on_found:
                on_found(app)
    if changed:
        os.makedirs(os.path.dirname(NAME_CACHE), exist_ok=True)
        with open(NAME_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=1)


# --------------------------------------------------------------------------
# uninstall commands
# --------------------------------------------------------------------------

def uninstall_kind(entry):
    """How an Uninstall entry's program can be run quietly: msi | inno | nsis | quiet | interactive."""
    cmd = entry.uninstall.lower()
    if entry.msi or "msiexec" in cmd:
        return "msi"
    exe = os.path.basename(winsys.parse_icon_spec(entry.uninstall)[0] if entry.uninstall.startswith('"')
                           else entry.uninstall.split(" /")[0]).lower()
    if re.match(r"unins\d{3}\.exe", exe) and entry.key.endswith("_is1"):
        return "inno"
    if entry.quiet_uninstall:
        return "quiet"
    if "uninstall" in exe and entry.key and not entry.key.startswith("{"):
        try:
            with open(winsys.parse_icon_spec(entry.uninstall)[0], "rb") as f:
                head = f.read(4 * 1024 * 1024)
            if b"Nullsoft" in head or b"NSIS" in head:
                return "nsis"
        except OSError:
            pass
    return "interactive"


def uninstall_command(entry):
    """(command line string, quiet) for an Uninstall entry."""
    kind = uninstall_kind(entry)
    if kind == "msi":
        m = re.search(r"\{[0-9A-Fa-f-]{36}\}", entry.uninstall) or re.search(r"\{[0-9A-Fa-f-]{36}\}", entry.key)
        if m:
            return f'msiexec.exe /x {m.group(0)} /qn /norestart', True
        return entry.uninstall, False
    if kind == "quiet":
        return entry.quiet_uninstall, True
    if kind == "inno":
        return f"{entry.uninstall} /VERYSILENT /SUPPRESSMSGBOXES /NORESTART", True
    if kind == "nsis":
        return f"{entry.uninstall} /S", True
    return entry.uninstall, False
