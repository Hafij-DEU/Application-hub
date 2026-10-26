"""Catalog, GitHub release lookup, downloads and install state for App Hub on Windows.

Shared by the GUI and the CLI. Nothing here needs administrator rights: privileged
work goes through the elevated helper with one UAC prompt (see helper.run_plan).
"""

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import __version__, winsys

LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(LIB_DIR, "catalog.json")
CACHE_DIR = winsys.cache_dir()
DATA_DIR = winsys.data_dir()
CONFIG_DIR = winsys.config_dir()
SETTINGS = os.path.join(CONFIG_DIR, "settings.json")

API = "https://api.github.com"
USER_AGENT = f"app-hub-windows/{__version__}"

# Words in asset names that mean another architecture than the machine's.
OTHER_ARCH_TOKENS = {
    "x64": ("arm64", "aarch64", "x86", "i386", "i686", "ia32", "win32", "32bit", "32-bit"),
    "arm64": ("x86", "i386", "i686", "ia32", "win32", "32bit", "32-bit"),
    "x86": ("x64", "amd64", "x86_64", "win64", "arm64", "aarch64", "64bit", "64-bit"),
}
OWN_ARCH_TOKENS = {"x64": ("x64", "amd64", "x86_64", "win64", "64bit", "64-bit"),
                   "arm64": ("arm64", "aarch64"), "x86": ("x86", "i386", "i686", "ia32", "win32")}
NOT_WINDOWS = re.compile(r"linux|darwin|macos|\bmac\b|osx|android|\.apk|freebsd|appimage|\.deb$|\.rpm$|"
                         r"\.dmg$|\.pkg|\.tar|\.flatpak|\.snap$|chromium|firefox|extension|-web-|\bweb\b|"
                         r"-cli-|\bcli\b|symbols|debug|\.blockmap|\.sig$|\.sha\d*$|\.asc$|sha256sums|\.yml$|"
                         r"\.txt$|\.json$", re.IGNORECASE)


class HubError(Exception):
    pass


class Cancelled(HubError):
    pass


@dataclass
class Release:
    tag: str
    version: str
    url: str
    asset_name: str = ""
    asset_url: str = ""
    asset_api: str = ""      # the same file through the API (downloads when signed in)
    size: int = 0
    sha256: str = ""
    sums_url: str = ""
    sums_api: str = ""
    kind: str = ""           # installer kind from the name: msi | exe | zip | msix
    notes: str = ""          # release notes (Markdown, as written on GitHub)
    published: str = ""      # ISO date
    stale: float = 0         # nonzero: GitHub could not be asked; cached answer (time, or -1 = unknown)
    skipped: str = ""        # newer tag without a Windows download
    extras: list = field(default_factory=list)   # browser-extension zips: [{folder, name, url, size, sha256}]


@dataclass
class App:
    id: str
    name: str
    summary: str
    repo: str
    package: str = ""                              # kept for catalog compatibility with the Linux App Hub
    match: str = ""                                # regex for the Settings ▸ Apps name ("" = starts with name)
    uninstall_key: str = ""                        # exact Uninstall registry key, e.g. "{GUID}_is1"
    asset_pattern: str = ""                        # regex the Windows download must match
    version_from: str = ""                         # "asset": take the version from the file name, not the tag
    exe: str = ""                                  # main program, to open portable installs
    installed: str | None = None
    icon: str = ""                                 # icon file (Library apps)
    owner: str = ""                                # Library group; "" = My Apps
    section: str = ""                              # "self" = App Hub itself, "mine" = added to My Apps
    browser_extensions: list = field(default_factory=list)
    # [{"pattern": regex, "folder": "Name/chrome"}]: release .zip files unpacked into Documents
    release: Release | None = None
    error: str = ""
    entry: object = None                           # inventory.Arp of the installed copy

    @property
    def repo_url(self):
        return f"https://github.com/{self.repo}"

    @property
    def update_available(self):
        if not (self.installed and self.release and self.release.asset_url):
            return False
        return is_newer(self.release.version, self.installed)

    @property
    def installable(self):
        return bool(self.release and self.release.asset_url)

    @property
    def machine(self):
        """Installed for all users (an update then needs administrator rights)."""
        return bool(self.entry and self.entry.machine)

    def status_text(self):
        parts = []
        if self.installed:
            where = " for all users" if self.machine else ""
            parts.append(f"Installed {clean_version(self.installed)}{where}")
        else:
            parts.append("Not installed")
        if self.error:
            parts.append(self.error)
        elif self.release is None:
            parts.append("checking…")
        elif not self.release.asset_url:
            parts.append(f"{self.release.tag} has no Windows download")
        elif self.installed and not self.update_available and is_newer(
                clean_version(self.installed), self.release.version):
            parts.append("newer than the latest release with a Windows download" if self.release.skipped
                         else "newer than the latest release")
        elif self.update_available:
            parts.append(f"update {self.release.version} available")
        elif self.installed:
            parts.append("up to date")
        else:
            parts.append(f"latest {self.release.version} · {human_size(self.release.size)}"
                         f" · {kind_label(self.release.kind)}")
        if self.release and self.release.stale:
            when = ("an earlier check" if self.release.stale < 0 else
                    time.strftime("%d %b %H:%M", time.localtime(self.release.stale)))
            parts.append(f"could not ask GitHub (limit or offline), info from {when}")
        return " · ".join(parts)


def kind_label(kind):
    return {"msi": "Windows Installer", "exe": "installer", "zip": "portable .zip",
            "msix": "MSIX package"}.get(kind, kind or "download")


def _make_app(entry):
    fields = App.__dataclass_fields__
    return App(**{k: v for k, v in entry.items() if k in fields and k not in ("release", "entry")})


def load_catalog(include_library=True):
    """My Apps (catalog.json), followed by the Library apps unless include_library is False."""
    with open(CATALOG, encoding="utf-8") as f:
        data = json.load(f)
    apps = [_make_app(e) for e in data["apps"]]
    if include_library:
        from . import library
        mine = {a.id for a in apps}
        apps += [a for a in library.load_apps() if a.id not in mine]
    return apps


def compile_match(app):
    """Regex that recognises the app's name in Settings ▸ Apps."""
    pattern = app.match or r"^" + re.escape(app.name) + r"(\s|$|\()"
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return re.compile(r"^" + re.escape(app.name), re.IGNORECASE)


# --- admin PIN: Add App in My Apps only for the admin ----------------------
# catalog.json may hold {"admin_pin": {"salt": HEX, "hash": HEX, "iterations": N}} (the same
# PIN as the Linux App Hub). Entering the PIN in Preferences stores the hash in settings.json.

def admin_pin():
    try:
        with open(CATALOG, encoding="utf-8") as f:
            pin = json.load(f).get("admin_pin")
    except (OSError, ValueError):
        return None
    return pin if isinstance(pin, dict) and pin.get("hash") and pin.get("salt") else None


def pin_hash(pin, salt_hex, iterations):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt_hex), int(iterations)).hex()


def check_admin_pin(pin):
    stored = admin_pin()
    if not stored:
        return True
    import hmac
    return hmac.compare_digest(pin_hash(pin, stored["salt"], stored.get("iterations", 600000)),
                               stored["hash"])


def is_admin():
    stored = admin_pin()
    return not stored or _settings().get("admin_unlock") == stored["hash"]


def find_apps(apps, names):
    """Match CLI names against id or name (case-insensitive)."""
    found, missing = [], []
    for name in names:
        key = name.lower()
        match = next((a for a in apps if key in (a.id, a.package, a.name.lower())), None)
        (found if match else missing).append(match or name)
    if missing:
        raise HubError("unknown app: " + ", ".join(missing)
                       + "\nknown: " + ", ".join(a.id for a in apps))
    return found


# --- versions -------------------------------------------------------------

def clean_version(v):
    """Strip a tag's 'v' and words like 'version'; keep the rest."""
    v = (v or "").strip()
    v = re.sub(r"^(release|version|ver)[-_ ]?", "", v, flags=re.IGNORECASE)
    if v[:1] in "vV" and v[1:2].isdigit():
        v = v[1:]
    return v.replace("+", "-")


def version_from_name(name):
    """'Foo 1.2.3 (x64)' / 'FooSetup-1.2.3.exe' -> '1.2.3' ('' when there is none)."""
    m = re.search(r"(?<![A-Za-z\d])v?(\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9.]+)?)", name or "")
    return m.group(1) if m else ""


def _key(v):
    out = []
    for part in re.findall(r"\d+|[A-Za-z]+", clean_version(v)):
        out.append((1, int(part), "") if part.isdigit() else (0, 0, part.lower()))
    return out


def compare(a, b):
    """-1, 0 or 1. '1.2' == '1.2.0', '1.2.0-beta' < '1.2.0', '1.10' > '1.9'."""
    ka, kb = _key(a), _key(b)
    n = max(len(ka), len(kb))
    pad = (1, 0, "")
    ka += [pad] * (n - len(ka))
    kb += [pad] * (n - len(kb))
    return (ka > kb) - (ka < kb)


def is_newer(latest, installed):
    if not latest or not installed:
        return False
    return compare(latest, installed) > 0


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


# --- installed state ---------------------------------------------------------

def refresh_installed(apps):
    """Find each app in Settings ▸ Apps (the Uninstall registry keys)."""
    from . import inventory
    entries = inventory.read_arp()
    for app in apps:
        app.installed, app.entry = None, None
        rx = compile_match(app)
        hit = None
        for e in entries:
            if (app.uninstall_key and e.key.lower() == app.uninstall_key.lower()) or rx.search(e.name):
                if hit is None or (not hit.version and e.version):
                    hit = e
        if hit:
            app.entry = hit
            app.installed = hit.version or version_from_name(hit.name) or "?"
        elif app.section == "self":
            app.installed = __version__          # running from source: this is the version


def launch(app):
    """Open an installed app: its Start-menu shortcut, else its program."""
    from . import inventory
    target = ""
    if app.entry:
        target = inventory.shortcut_for(app.entry.name) or inventory.main_exe(app.entry)
        if not target and app.exe and app.entry.location:
            exe = os.path.join(app.entry.location, app.exe)
            target = exe if os.path.exists(exe) else ""
    if not target:
        target = inventory.shortcut_for(app.name)
    if not target:
        raise HubError(f"could not find how to start {app.name}")
    winsys.open_path(target)


# --- GitHub ---------------------------------------------------------------

_token = None
_token_lock = threading.Lock()


def _settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def github_token():
    """Token for api.github.com: GITHUB_TOKEN/GH_TOKEN, the one saved in Preferences, or the
    GitHub CLI login (gh auth token). Signed-in requests get 5000/hour instead of 60."""
    global _token
    with _token_lock:             # parallel checks must all wait for the same answer
        if _token is None:
            settings = _settings()
            token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
                     or settings.get("github_token", "").strip())
            if not token and settings.get("use_gh_login", True) and shutil.which("gh"):
                r = winsys.run(["gh", "auth", "token", "--hostname", "github.com"], timeout=15)
                token = r.stdout.strip() if r.returncode == 0 else ""
            _token = token
        return _token


def forget_token():
    global _token
    with _token_lock:
        _token = None


def _request(url, accept="application/vnd.github+json"):
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    token = github_token()
    if token and url.startswith(API):
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """GitHub answers an asset download with a redirect to its file servers: the token must
    not go along (it is only for api.github.com, and the signed link needs none)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not newurl.startswith(API):
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(_DropAuthOnRedirect)


def open_download(public_url, api_url="", timeout=30):
    """Open a release file. Signed in, it is fetched through the API (works for private
    repositories and accounts GitHub hides from visitors); otherwise from the public link."""
    if api_url and github_token():
        return _opener.open(_request(api_url, "application/octet-stream"), timeout=timeout)
    return urllib.request.urlopen(_request(public_url, "application/octet-stream"), timeout=timeout)


_api_lock = threading.Lock()
API_CACHE = os.path.join(CACHE_DIR, "api-cache.json")


def _api_cache_load():
    try:
        with open(API_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _api_cache_store(url, etag, data):
    with _api_lock:
        cache = _api_cache_load()
        cache[url] = {"etag": etag, "data": data, "time": time.time()}
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = f"{API_CACHE}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        for _ in range(5):          # another thread may hold the file for a moment
            try:
                os.replace(tmp, API_CACHE)
                return
            except PermissionError:
                time.sleep(0.05)
        _unlink(tmp)


class Stale(dict):
    """A cached API answer used because GitHub could not be asked (rate limit, offline)."""
    since = 0.0
    reason = ""


def _stale(cached, reason):
    out = Stale(cached["data"]) if isinstance(cached["data"], dict) else cached["data"]
    if isinstance(out, Stale):
        out.since, out.reason = cached.get("time") or -1.0, reason
    return out


def _get_json(url):
    """GET a GitHub API URL. Uses ETags: unchanged answers (304) don't count against the
    rate limit, and the cached copy is used (marked Stale) when limited or offline."""
    with _api_lock:
        cached = _api_cache_load().get(url)
    req = _request(url)
    if cached and cached.get("etag"):
        req.add_header("If-None-Match", cached["etag"])
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
            _api_cache_store(url, resp.headers.get("ETag", ""), data)
            return data
    except urllib.error.HTTPError as e:
        if e.code == 304 and cached:
            _api_cache_store(url, cached.get("etag", ""), cached["data"])
            return cached["data"]
        if e.code == 404:
            raise HubError("no release published") from None
        if e.code in (403, 429):
            if cached:
                return _stale(cached, "GitHub limit reached")
            raise HubError("GitHub limit reached, try again later or sign in (Preferences)") from None
        raise HubError(f"GitHub error {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if cached:
            return _stale(cached, "offline")
        reason = getattr(e, "reason", e)
        raise HubError(f"offline? ({reason})") from None


def asset_kind(name):
    low = name.lower()
    for ext, kind in ((".msi", "msi"), (".exe", "exe"), (".zip", "zip"), (".msix", "msix"),
                      (".msixbundle", "msix"), (".appx", "msix"), (".appxbundle", "msix")):
        if low.endswith(ext):
            return kind
    return ""


def _tokens(low):
    return set(re.split(r"[^a-z0-9]+", low.replace("x86_64", "x64").replace("x86-64", "x64")))


def asset_score(name, arch=None):
    """How well a release file fits this PC (0 = not usable). Prefers installers over
    portable zips, this machine's architecture over a generic build."""
    arch = arch or winsys.machine_arch()
    low = name.lower()
    kind = asset_kind(name)
    if not kind or NOT_WINDOWS.search(low):
        return 0
    tokens = _tokens(low)
    if kind in ("zip",) and not ({"win", "windows", "win64", "win32", "win10", "win11", "portable"} & tokens
                                 or "windows" in low or "win-" in low or "-win" in low):
        return 0              # a .zip without "win" in its name is source or another platform
    if any(t in tokens for t in OTHER_ARCH_TOKENS.get(arch, ())):
        if not (arch == "arm64" and tokens & {"x64", "amd64", "win64"}):
            return 0
        own = -2              # arm64 PCs run x64 apps, but a native build is better
    else:
        own = 2 if tokens & set(OWN_ARCH_TOKENS.get(arch, ())) else 1
    portable = "portable" in low or kind == "zip"
    installer = kind == "msi" or (kind == "exe" and re.search(r"setup|install", low))
    base = {"exe": 7, "msi": 6, "msix": 4, "zip": 3}[kind]
    if installer:
        base += 3
    if portable:
        base -= 2
    return max(1, base * 3 + own)


def pick_asset(assets, pattern="", arch=None):
    best, best_score = None, 0
    for asset in assets:
        name = asset["name"]
        if pattern and not re.search(pattern, name):
            continue
        score = asset_score(name, arch)
        if score > best_score:
            best, best_score = asset, score
    return best


def fetch_release(app):
    data = _get_json(f"{API}/repos/{app.repo}/releases/latest")
    skipped = ""
    if not pick_asset(data.get("assets", []), app.asset_pattern):
        # The newest release may ship only other platforms (e.g. a Linux-only fix):
        # use the newest stable release that has a Windows download.
        try:
            recent = _get_json(f"{API}/repos/{app.repo}/releases?per_page=15")
        except HubError:
            recent = []
        for r in recent if isinstance(recent, list) else []:
            if not r.get("draft") and not r.get("prerelease") and pick_asset(r.get("assets", []),
                                                                             app.asset_pattern):
                skipped = data.get("tag_name", "")
                stale = data if isinstance(data, Stale) else None
                data = r
                if stale:
                    data = Stale(r)
                    data.since, data.reason = stale.since, stale.reason
                break
    tag = data.get("tag_name", "")
    rel = Release(tag=tag, version=clean_version(tag), url=data.get("html_url", app.repo_url),
                  notes=(data.get("body") or "").strip(), published=data.get("published_at") or "",
                  stale=data.since if isinstance(data, Stale) else 0, skipped=skipped)
    assets = data.get("assets", [])
    asset = pick_asset(assets, app.asset_pattern)
    if asset:
        rel.asset_name = asset["name"]
        rel.asset_url = asset["browser_download_url"]
        rel.asset_api = asset.get("url", "")
        rel.size = asset.get("size", 0)
        rel.kind = asset_kind(asset["name"])
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            rel.sha256 = digest[7:]
        if app.version_from == "asset":
            rel.version = version_from_name(asset["name"]) or rel.version
    for spec in app.browser_extensions:
        extra = next((a for a in assets if a["name"].lower().endswith(".zip")
                      and re.search(spec.get("pattern", r"\.zip$"), a["name"])), None)
        if extra:
            digest = extra.get("digest") or ""
            rel.extras.append({"folder": spec["folder"], "name": extra["name"],
                               "url": extra["browser_download_url"], "api": extra.get("url", ""), "size": extra.get("size", 0),
                               "sha256": digest[7:] if digest.startswith("sha256:") else ""})
    sums = next((a for a in assets if a["name"].upper().startswith("SHA256SUMS")), None)
    if sums:
        rel.sums_url = sums["browser_download_url"]
        rel.sums_api = sums.get("url", "")
    return rel


def download_stats(app):
    """[(tag, date, downloads)] of the app's Windows downloads per release, newest first."""
    try:
        data = _get_json(f"{API}/repos/{app.repo}/releases?per_page=100")
    except HubError:
        raise
    except Exception as e:  # noqa: BLE001 - e.g. a stale cache entry that is not a list
        raise HubError(str(e)) from None
    out = []
    for r in data if isinstance(data, list) else []:
        if r.get("draft"):
            continue
        n = sum(a.get("download_count", 0) for a in r.get("assets", [])
                if asset_score(a.get("name", ""), "x64") or asset_score(a.get("name", ""), "arm64"))
        out.append((r.get("tag_name", ""), (r.get("published_at") or "")[:10], n))
    return out


def check_all(apps, on_done=None):
    """Fetch latest releases in parallel; on_done(app) runs in a worker thread."""
    def one(app):
        try:
            app.release, app.error = fetch_release(app), ""
        except HubError as e:
            app.release, app.error = None, str(e)
        if app.release and app.release.extras and app.installed and not app.update_available:
            sync_browser_extensions(app)        # app up to date, extension copy behind: refresh it
        if on_done:
            on_done(app)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(one, apps))


# --- download -------------------------------------------------------------

def _expected_sha256(rel):
    if rel.sha256:
        return rel.sha256
    if not rel.sums_url:
        return ""
    try:
        with open_download(rel.sums_url, rel.sums_api, timeout=20) as resp:
            text = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*").split("/")[-1] == rel.asset_name:
            return parts[0].lower()
    return ""


def download(app, progress=None, cancel: threading.Event | None = None):
    """Download the app's Windows release file into the cache and verify it. Returns the path."""
    rel = app.release
    if not rel or not rel.asset_url:
        raise HubError(f"{app.name}: no Windows download")
    folder = os.path.join(CACHE_DIR, re.sub(r"[^A-Za-z0-9._-]", "_", app.id))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, os.path.basename(rel.asset_name))
    expected = _expected_sha256(rel)

    if os.path.exists(path) and expected and _sha256(path) == expected:
        if progress:
            progress(rel.size, rel.size)
        return path

    part = path + ".part"
    digest = hashlib.sha256()
    try:
        with open_download(rel.asset_url, rel.asset_api) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or rel.size or 0)
            done = 0
            while True:
                if cancel and cancel.is_set():
                    raise Cancelled("download cancelled")
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except (urllib.error.URLError, OSError) as e:
        _unlink(part)
        raise HubError(f"{app.name}: download failed ({getattr(e, 'reason', e)})") from None
    except BaseException:
        _unlink(part)
        raise

    if expected and digest.hexdigest() != expected:
        _unlink(part)
        raise HubError(f"{app.name}: checksum mismatch, download discarded")
    os.replace(part, path)
    return path


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def clear_cache(app):
    shutil.rmtree(os.path.join(CACHE_DIR, re.sub(r"[^A-Za-z0-9._-]", "_", app.id)), ignore_errors=True)


def finish_install(app, log=None):
    """After a successful install: drop the download; put the app's browser extension (if it
    has one) in Documents."""
    clear_cache(app)
    sync_browser_extensions(app, log, force=True)


# --- browser extensions shipped as release .zip files ----------------------

def browser_extension_dir(folder):
    return os.path.join(winsys.documents_dir(), *folder.split("/"))


def _browser_extension_version(path):
    try:
        with open(os.path.join(path, "manifest.json"), encoding="utf-8") as f:
            return str(json.load(f).get("version", ""))
    except (OSError, ValueError):
        return ""


def browser_extensions_outdated(app):
    rel = app.release
    if not (app.installed and rel and rel.extras):
        return []
    return [x for x in rel.extras
            if is_newer(rel.version, _browser_extension_version(browser_extension_dir(x["folder"])) or "0")]


def browser_extension_root(app):
    tops = {x["folder"].split("/")[0] for x in app.browser_extensions}
    return browser_extension_dir(tops.pop()) if len(tops) == 1 else winsys.documents_dir()


def _fetch_bytes(x):
    try:
        with open_download(x["url"], x.get("api", "")) as resp:
            data = resp.read()
    except (urllib.error.URLError, OSError) as e:
        raise HubError(f"{x['name']}: download failed ({getattr(e, 'reason', e)})") from None
    if x.get("sha256") and hashlib.sha256(data).hexdigest() != x["sha256"]:
        raise HubError(f"{x['name']}: checksum mismatch")
    return data


def save_browser_extension_zips(app, dest=None, log=None):
    rel = app.release
    if not rel or not rel.extras:
        raise HubError(f"{app.name}: this release has no browser-extension .zip")
    dest = dest or winsys.downloads_dir()
    os.makedirs(dest, exist_ok=True)
    out = []
    for x in rel.extras:
        data = _fetch_bytes(x)
        path = os.path.join(dest, x["name"])
        with open(path, "wb") as f:
            f.write(data)
        out.append(path)
        if log:
            log(f"{app.name}: saved {path}")
    return out


def sync_browser_extensions(app, log=None, force=False):
    """Unpack the release's browser-extension zips into Documents (replacing an older copy)."""
    rel = app.release
    if not rel or not rel.extras or not (app.installed or force):
        return []
    todo = rel.extras if force else browser_extensions_outdated(app)
    done = []
    for x in todo:
        target = browser_extension_dir(x["folder"])
        try:
            import io
            import zipfile
            data = _fetch_bytes(x)
            tmp = target + ".app-hub-new"
            shutil.rmtree(tmp, ignore_errors=True)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for name in z.namelist():                     # refuse paths leaving the folder
                    dest = os.path.normpath(os.path.join(tmp, name))
                    if not dest.startswith(os.path.normpath(tmp) + os.sep):
                        raise HubError(f"{x['name']}: unsafe path {name}")
                z.extractall(tmp)
            if not os.path.exists(os.path.join(tmp, "manifest.json")):
                raise HubError(f"{x['name']}: not a browser extension (no manifest.json)")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.rmtree(target, ignore_errors=True)
            os.replace(tmp, target)
            done.append(target)
            if log:
                log(f"{app.name}: browser extension {rel.version} → {target} "
                    "(click Reload on its card in the browser's extensions page)")
        except (HubError, OSError, ValueError) as e:
            if log:
                log(f"{app.name}: browser extension not updated ({getattr(e, 'reason', e)})")
    return done


# --- running programs -------------------------------------------------------

AUTH_CANCELLED = (1223,)


def run_command(cmd, on_line=None, cwd=None):
    """Run cmd (list or command-line string), streaming merged output lines.
    Returns (returncode, full_output)."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=cwd, **winsys.popen_kwargs())
    except OSError as e:
        return 1, str(e)
    lines = []
    buf = b""
    while True:
        chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk
        while True:
            cut = min([i for i in (buf.find(b"\n"), buf.find(b"\r")) if i >= 0], default=-1)
            if cut < 0:
                break
            line, buf = buf[:cut], buf[cut + 1:]
            text = _decode(line).strip()
            if text and not _is_spinner(text):
                lines.append(text)
                if on_line:
                    on_line(text)
    if buf.strip():
        text = _decode(buf).strip()
        lines.append(text)
        if on_line:
            on_line(text)
    return proc.wait(), "\n".join(lines)


def _decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("mbcs", "replace") if winsys.IS_WIN else data.decode("latin-1")


def _is_spinner(text):
    """winget's progress bars and spinners: not worth a log line each."""
    return bool(re.fullmatch(r"[-\\|/\s]+|[█▒\s]+.*\d+(\.\d+)?\s*(%|[KMG]?B\s*/\s*[\d.]+\s*[KMG]?B)", text))
