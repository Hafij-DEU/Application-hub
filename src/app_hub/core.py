"""Catalog, GitHub release lookup, downloads and dpkg state for App Hub.

Shared by the GUI and the CLI. Nothing here needs root: privileged work goes
through app-hub-helper via pkexec (see run_helper).
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

from . import __version__

LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(LIB_DIR, "catalog.json")
HELPER = os.path.join(LIB_DIR, "app-hub-helper")
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "app-hub")

API = "https://api.github.com"
USER_AGENT = f"app-hub/{__version__}"
SETTINGS = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                        "app-hub", "settings.json")

OTHER_ARCH_TOKENS = {
    "amd64": ("arm64", "aarch64", "armhf", "armv7", "i386", "i686", "ppc64el", "s390x", "riscv64"),
    "arm64": ("amd64", "x86_64", "x64", "armhf", "armv7", "i386", "i686", "ppc64el", "s390x", "riscv64"),
}


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
    size: int = 0
    sha256: str = ""
    sums_url: str = ""
    notes: str = ""          # release notes (Markdown, as written on GitHub)
    published: str = ""      # ISO date
    stale: float = 0         # nonzero: GitHub could not be asked; cached answer (time, or -1 = unknown)
    skipped: str = ""        # newer tag without a .deb for this system
    extras: list = field(default_factory=list)   # browser-extension zips: [{folder, name, url, size, sha256}]


@dataclass
class App:
    id: str
    name: str
    summary: str
    repo: str
    package: str
    desktop: str = ""
    asset_pattern: str = ""
    replaces: list = field(default_factory=list)   # old package names of this app
    installed: str | None = None
    installed_as: str = ""                         # package actually installed
    installed_alt: str = ""                        # extension: metadata "version" (the integer GNOME counts),
                                                   # when releases are tagged with it instead of version-name
    icon: str = ""                                 # icon file (Library apps)
    owner: str = ""                                # Library group; "" = My Apps
    section: str = ""                              # "self" = App Hub itself, "mine" = added to My Apps,
                                                   # "extension" = GNOME Shell extension (My Apps ▸ Extensions)
    extension: str = ""                            # GNOME Shell extension UUID (installed from a release .zip)
    system: bool = False                           # extension goes to /usr/share (needs root), not ~/.local
    gdm: bool = False                              # system extension is also turned on for the login screen
    browser_extensions: list = field(default_factory=list)
    # [{"pattern": regex, "folder": "Name/chrome"}]: release .zip files unpacked into Documents
    # with the app (and refreshed when a newer one is released), e.g. Draco's browser extension
    release: Release | None = None
    error: str = ""

    @property
    def repo_url(self):
        return f"https://github.com/{self.repo}"

    @property
    def update_available(self):
        if not (self.installed and self.release and self.release.asset_url):
            return False
        if self.renamed:
            return True
        if self.installed_alt and clean_version(self.installed_alt) == self.release.version:
            return False
        return is_newer(self.release.version, self.installed)

    @property
    def renamed(self):
        """Installed under an old package name (the update switches to the new one)."""
        return bool(self.installed_as and self.installed_as != self.package)

    @property
    def installable(self):
        return bool(self.release and self.release.asset_url)

    def status_text(self):
        parts = []
        if self.installed:
            parts.append(f"Installed {clean_version(self.installed)}"
                         + (f" as {self.installed_as}" if self.renamed else ""))
        else:
            parts.append("Not installed")
        if self.error:
            parts.append(self.error)
        elif self.release is None:
            parts.append("checking…")
        elif not self.release.asset_url:
            parts.append(f"{self.release.tag} has no " + ("extension .zip" if self.extension
                                                           else ".deb for this system"))
        elif self.installed and not self.update_available and is_newer(
                clean_version(self.installed), self.release.version):
            parts.append("newer than the latest release with a .deb" if self.release.skipped
                         else "newer than the latest release")
        elif self.update_available:
            parts.append(f"update {self.release.version} available")
        elif self.installed:
            parts.append("up to date")
        else:
            parts.append(f"latest {self.release.version} · {human_size(self.release.size)}")
        if self.release and self.release.stale:
            when = ("an earlier check" if self.release.stale < 0 else
                    time.strftime("%d %b %H:%M", time.localtime(self.release.stale)))
            parts.append(f"could not ask GitHub (limit or offline), info from {when}")
        return " · ".join(parts)


def load_catalog(include_library=True):
    """My Apps (catalog.json), followed by the Library apps unless include_library is False."""
    with open(CATALOG, encoding="utf-8") as f:
        data = json.load(f)
    fields = App.__dataclass_fields__
    apps = [App(**{k: v for k, v in entry.items() if k in fields}) for entry in data["apps"]]
    if include_library:
        from . import library
        mine = {a.id for a in apps}
        apps += [a for a in library.load_apps() if a.id not in mine]
    return apps


# --- admin PIN: Add App in My Apps only for the admin ----------------------
# catalog.json may hold {"admin_pin": {"salt": HEX, "hash": HEX, "iterations": N}}, written by
# set-admin-pin.py. Entering the PIN in Preferences stores the hash in settings.json, which
# shows Add App in My Apps on that computer. Without an admin_pin, Add App is always shown.

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
    """True if no admin PIN is set, or `pin` matches it."""
    stored = admin_pin()
    if not stored:
        return True
    import hmac
    return hmac.compare_digest(pin_hash(pin, stored["salt"], stored.get("iterations", 600000)),
                               stored["hash"])


def is_admin():
    """Admin mode is on for this user (PIN entered in Preferences), or no PIN is set."""
    stored = admin_pin()
    return not stored or _settings().get("admin_unlock") == stored["hash"]


def find_apps(apps, names):
    """Match CLI names against id, package or name (case-insensitive)."""
    found, missing = [], []
    for name in names:
        key = name.lower()
        match = next((a for a in apps if key in (a.id, a.package, a.name.lower(), *a.replaces)), None)
        (found if match else missing).append(match or name)
    if missing:
        raise HubError("unknown app: " + ", ".join(missing)
                       + "\nknown: " + ", ".join(a.id for a in apps))
    return found


# --- system state ---------------------------------------------------------

def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def dpkg_arch():
    out = _run(["dpkg", "--print-architecture"]).stdout.strip()
    return out or "amd64"


def os_release():
    info = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep:
                    info[key] = value.strip('"')
    except OSError:
        pass
    return info.get("ID", ""), info.get("VERSION_ID", "")


def installed_version(package):
    r = _run(["dpkg-query", "-W", "-f=${db:Status-Abbrev}|${Version}", package])
    if r.returncode != 0:
        return None
    status, _, version = r.stdout.partition("|")
    return version.strip() if status.startswith("ii") else None


def refresh_installed(apps):
    for app in apps:
        app.installed, app.installed_as, app.installed_alt = None, "", ""
        if app.extension:
            app.installed = extension_version(app)
            app.installed_alt = extension_version(app, raw=True) or ""
            continue
        for pkg in [app.package, *app.replaces]:
            version = installed_version(pkg)
            if version:
                app.installed, app.installed_as = version, pkg
                break


def clean_version(v):
    """Strip tag 'v', distro suffix (~ubuntu24.04) and Debian revision (-1)."""
    v = v.strip()
    if v[:1] in "vV" and v[1:2].isdigit():
        v = v[1:]
    v = v.split("~", 1)[0]
    v = re.sub(r"-\d+(ubuntu\d*)?$", "", v)
    return v.replace("+", "-")


def is_newer(latest, installed):
    a, b = clean_version(latest), clean_version(installed)
    if a == b:
        return False
    return _run(["dpkg", "--compare-versions", a, "gt", b]).returncode == 0


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


# --- GitHub ---------------------------------------------------------------

_token = None


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
    if _token is None:
        settings = _settings()
        _token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
                  or settings.get("github_token", "").strip())
        if not _token and settings.get("use_gh_login", True) and shutil.which("gh"):
            try:
                r = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"],
                                   capture_output=True, text=True, timeout=5)
                _token = r.stdout.strip() if r.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                _token = ""
    return _token


def forget_token():
    """Re-read the token next time (after Preferences change)."""
    global _token
    _token = None


def _request(url, accept="application/vnd.github+json"):
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    token = github_token()
    if token and url.startswith(API):
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


_api_lock = threading.Lock()
API_CACHE = os.path.join(CACHE_DIR, "api-cache.json")


API_CACHE_DAYS = 30     # answers not asked for again in this long are dropped
_RELEASE_KEYS = ("tag_name", "html_url", "body", "published_at", "draft", "prerelease")
_ASSET_KEYS = ("name", "browser_download_url", "size", "digest", "download_count")
_api_mem = None         # the cache file, read once per process


def _slim_release(r, body=True):
    out = {k: r.get(k) for k in _RELEASE_KEYS if body or k != "body"}
    out["assets"] = [{k: a.get(k) for k in _ASSET_KEYS} for a in r.get("assets") or []]
    return out


def _slim(data):
    """Keep only the release fields App Hub reads (GitHub sends uploader objects, URLs
    for every asset, …): the cache file shrinks to about a quarter."""
    if isinstance(data, dict) and "tag_name" in data:
        return _slim_release(data)
    if isinstance(data, list):
        return [_slim_release(r, body=i < 10) if isinstance(r, dict) and "tag_name" in r else r
                for i, r in enumerate(data)]
    return data


def _api_cache_load():
    """The cache as a dict (shared, read once). A broken file is deleted, old entries dropped."""
    global _api_mem
    if _api_mem is None:
        try:
            with open(API_CACHE, encoding="utf-8") as f:
                cache = json.load(f)
            if not isinstance(cache, dict):
                raise ValueError("not a dict")
        except FileNotFoundError:
            cache = {}
        except (OSError, ValueError):
            _unlink(API_CACHE)
            cache = {}
        cutoff = time.time() - API_CACHE_DAYS * 86400
        _api_mem = {u: dict(e, data=_slim(e["data"])) for u, e in cache.items()
                    if isinstance(e, dict) and "data" in e and (e.get("time") or 0) > cutoff}
    return _api_mem


def _api_cache_store(url, etag, data):
    with _api_lock:
        cache = _api_cache_load()
        cache[url] = {"etag": etag, "data": data, "time": time.time()}
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = f"{API_CACHE}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, separators=(",", ":"))
        os.replace(tmp, API_CACHE)


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
    """GET a GitHub API URL. Uses ETags: unchanged answers (304) don't count
    against the rate limit, and the cached copy is used (marked Stale) when limited."""
    with _api_lock:
        cached = _api_cache_load().get(url)
    req = _request(url)
    if cached and cached.get("etag"):
        req.add_header("If-None-Match", cached["etag"])
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = _slim(json.load(resp))
            _api_cache_store(url, resp.headers.get("ETag", ""), data)
            return data
    except urllib.error.HTTPError as e:
        if e.code == 304 and cached:
            _api_cache_store(url, cached.get("etag", ""), cached["data"])  # confirmed fresh now
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


def _distro_score(name, distro, distro_version):
    """3 = built for this exact release, 2 = generic, 1 = older release of this distro."""
    m = re.search(r"(ubuntu|debian|deb)[-_.]?(\d+(?:\.\d+)?)", name.lower())
    if not m:
        return 2
    if m.group(1) == distro and m.group(2) == distro_version:
        return 3
    try:
        older = tuple(map(int, m.group(2).split("."))) <= tuple(map(int, distro_version.split(".")))
    except ValueError:
        older = False
    return 1 if m.group(1) == distro and older else 0


def pick_asset(assets, pattern="", arch=None, distro=None):
    arch = arch or dpkg_arch()
    distro_id, distro_version = distro or os_release()
    others = OTHER_ARCH_TOKENS.get(arch, ())
    best, best_score = None, 0
    for asset in assets:
        name = asset["name"]
        low = name.lower()
        if not low.endswith(".deb") or (pattern and not re.search(pattern, name)):
            continue
        if any(tok in low for tok in others):
            continue
        score = _distro_score(name, distro_id, distro_version)
        if score > best_score:
            best, best_score = asset, score
    return best


def pick_zip(assets, pattern=""):
    """The GNOME Shell extension .zip of a release (prefers *.shell-extension.zip)."""
    zips = [a for a in assets if a["name"].lower().endswith(".zip")
            and (not pattern or re.search(pattern, a["name"]))]
    return next((a for a in zips if "shell-extension" in a["name"]), zips[0] if zips else None)


def _pick(app, assets):
    return pick_zip(assets, app.asset_pattern) if app.extension else pick_asset(assets, app.asset_pattern)


def fetch_release(app):
    data = _get_json(f"{API}/repos/{app.repo}/releases/latest")
    skipped = ""
    if not _pick(app, data.get("assets", [])):
        # The newest release may ship only other platforms (e.g. a Windows-only fix):
        # use the newest stable release that has a .deb for this system.
        try:
            recent = _get_json(f"{API}/repos/{app.repo}/releases?per_page=10")
        except HubError:
            recent = []
        for r in recent if isinstance(recent, list) else []:
            if not r.get("draft") and not r.get("prerelease") and _pick(app, r.get("assets", [])):
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
    asset = _pick(app, assets)
    if asset:
        rel.asset_name = asset["name"]
        rel.asset_url = asset["browser_download_url"]
        rel.size = asset.get("size", 0)
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            rel.sha256 = digest[7:]
    for spec in app.browser_extensions:
        extra = next((a for a in assets if a["name"].lower().endswith(".zip")
                      and re.search(spec.get("pattern", r"\.zip$"), a["name"])), None)
        if extra:
            digest = extra.get("digest") or ""
            rel.extras.append({"folder": spec["folder"], "name": extra["name"],
                               "url": extra["browser_download_url"], "size": extra.get("size", 0),
                               "sha256": digest[7:] if digest.startswith("sha256:") else ""})
    sums = next((a for a in assets if a["name"].upper().startswith("SHA256SUMS")), None)
    if sums:
        rel.sums_url = sums["browser_download_url"]
    return rel


def download_stats(app):
    """[(tag, date, downloads)] for the app's published releases, newest first.
    Counts the .deb files (the .zip for extensions). GitHub counts every download,
    including the owner's own installs and updates."""
    try:
        data = _get_json(f"{API}/repos/{app.repo}/releases?per_page=100")
    except HubError:
        raise
    except Exception as e:  # e.g. a stale cache entry that is not a list
        raise HubError(str(e)) from None
    kind = ".zip" if app.extension else ".deb"
    out = []
    for r in data if isinstance(data, list) else []:
        if r.get("draft"):
            continue
        n = sum(a.get("download_count", 0) for a in r.get("assets", [])
                if a.get("name", "").lower().endswith(kind))
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
        with urllib.request.urlopen(_request(rel.sums_url, "*/*"), timeout=20) as resp:
            text = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*").split("/")[-1] == rel.asset_name:
            return parts[0].lower()
    return ""


def download(app, progress=None, cancel: threading.Event | None = None):
    """Download the app's release .deb into the cache and verify it. Returns the path."""
    rel = app.release
    if not rel or not rel.asset_url:
        raise HubError(f"{app.name}: no {'extension .zip' if app.extension else '.deb'} to download")
    folder = os.path.join(CACHE_DIR, app.id)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, rel.asset_name)
    expected = _expected_sha256(rel)

    if os.path.exists(path) and expected and _sha256(path) == expected:
        if progress:
            progress(rel.size, rel.size)
        return path

    part = path + ".part"
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(_request(rel.asset_url, "application/octet-stream"),
                                    timeout=30) as resp, open(part, "wb") as out:
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
    if app.extension:
        check_extension_zip(path, app.extension)
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
    shutil.rmtree(os.path.join(CACHE_DIR, app.id), ignore_errors=True)


def clear_staging():
    """Delete the .debs fetched for installs from the Library, AI Install and updates of
    other apps (they are fetched again when needed)."""
    shutil.rmtree(os.path.join(CACHE_DIR, "favorites"), ignore_errors=True)


DOWNLOAD_KEEP_DAYS = 2      # a download not installed within this long is deleted
_MAGIC = {".deb": b"!<arch>\n", ".zip": b"PK\x03\x04"}


def _looks_broken(path):
    magic = _MAGIC.get(os.path.splitext(path)[1].lower())
    if not magic:
        return False
    try:
        with open(path, "rb") as f:
            return f.read(len(magic)) != magic
    except OSError:
        return True


def prune_downloads(keep_days=DOWNLOAD_KEEP_DAYS):
    """Delete leftover downloads: unfinished (.part, older than an hour), broken (not a real
    .deb/.zip) or older than keep_days. Installed downloads are already deleted by
    finish_install. Returns the bytes freed."""
    now, freed = time.time(), 0
    try:
        folders = [e.path for e in os.scandir(CACHE_DIR) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return 0
    for folder in folders:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                path = os.path.join(root, name)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                age = now - st.st_mtime
                if (age > keep_days * 86400 or (name.endswith(".part") and age > 3600)
                        or (not name.endswith(".part") and _looks_broken(path))):
                    _unlink(path)
                    freed += st.st_size
        for root, _dirs, _files in os.walk(folder, topdown=False):
            try:
                os.rmdir(root)          # only succeeds when empty
            except OSError:
                pass
    return freed


def release_memory():
    """Give freed memory back to the system. glibc keeps freed blocks for reuse, so after a big
    temporary allocation (an apt cache is ~75 MB) the process would otherwise stay that large."""
    import gc
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def limit_malloc_arenas(n=2):
    """glibc gives each worker thread its own heap ("arena") and rarely returns their memory;
    with many short-lived threads that is 100+ MB. A couple of shared arenas are plenty here."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").mallopt(-8, n)     # M_ARENA_MAX
    except (OSError, AttributeError):
        pass


def finish_install(app, log=None):
    """After a successful install: drop the download; turn extensions on for this user; put the
    app's browser extension (if it has one) in Documents."""
    clear_cache(app)
    if app.extension:
        for note in enable_extension(app.extension):
            if log:
                log(f"{app.name}: {note}")
    sync_browser_extensions(app, log, force=True)


# --- browser extensions shipped as release .zip files ----------------------

def documents_dir():
    try:
        out = subprocess.run(["xdg-user-dir", "DOCUMENTS"], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    home = os.path.expanduser("~")
    if not out or os.path.normpath(out) == os.path.normpath(home):    # "$HOME/" means not set
        return os.path.join(home, "Documents")
    return out


def browser_extension_dir(folder):
    return os.path.join(documents_dir(), folder)


def _browser_extension_version(path):
    try:
        with open(os.path.join(path, "manifest.json"), encoding="utf-8") as f:
            return str(json.load(f).get("version", ""))
    except (OSError, ValueError):
        return ""


def browser_extensions_outdated(app):
    """Browser-extension folders that are missing or older than the latest release."""
    rel = app.release
    if not (app.installed and rel and rel.extras):
        return []
    return [x for x in rel.extras
            if is_newer(rel.version, _browser_extension_version(browser_extension_dir(x["folder"])) or "0")]


def browser_extension_root(app):
    """Documents folder that holds the app's unpacked browser extensions (e.g. "Draco Browser Extension")."""
    tops = {x["folder"].split("/")[0] for x in app.browser_extensions}
    return browser_extension_dir(tops.pop()) if len(tops) == 1 else documents_dir()


def save_browser_extension_zips(app, dest=None, log=None):
    """Save the release's browser-extension .zip files into Downloads; returns their paths."""
    rel = app.release
    if not rel or not rel.extras:
        raise HubError(f"{app.name}: this release has no browser-extension .zip")
    dest = dest or os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(dest, exist_ok=True)
    out = []
    for x in rel.extras:
        try:
            with urllib.request.urlopen(_request(x["url"], "application/octet-stream"), timeout=30) as resp:
                data = resp.read()
        except (urllib.error.URLError, OSError) as e:
            raise HubError(f"{x['name']}: download failed ({getattr(e, 'reason', e)})") from None
        if x.get("sha256") and hashlib.sha256(data).hexdigest() != x["sha256"]:
            raise HubError(f"{x['name']}: checksum mismatch, not saved")
        path = os.path.join(dest, x["name"])
        with open(path, "wb") as f:
            f.write(data)
        out.append(path)
        if log:
            log(f"{app.name}: saved {path}")
    return out


def sync_browser_extensions(app, log=None, force=False):
    """Download the release's browser-extension zips and unpack them into Documents
    (replacing an older copy). Without force only missing or outdated copies are fetched."""
    rel = app.release
    if not rel or not rel.extras or not (app.installed or force):
        return []
    todo = rel.extras if force else browser_extensions_outdated(app)
    done = []
    for x in todo:
        target = browser_extension_dir(x["folder"])
        try:
            folder = os.path.join(CACHE_DIR, app.id)
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, x["name"])
            with urllib.request.urlopen(_request(x["url"], "application/octet-stream"), timeout=30) as resp:
                data = resp.read()
            if x.get("sha256") and hashlib.sha256(data).hexdigest() != x["sha256"]:
                raise HubError(f"{x['name']}: checksum mismatch")
            with open(path, "wb") as f:
                f.write(data)
            import zipfile
            tmp = target + ".app-hub-new"
            shutil.rmtree(tmp, ignore_errors=True)
            with zipfile.ZipFile(path) as z:
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
            _unlink(path)
            done.append(target)
            if log:
                log(f"{app.name}: browser extension {rel.version} → {target} "
                    "(click Reload on its card in the browser's extensions page)")
        except (HubError, OSError, urllib.error.URLError, ValueError) as e:
            if log:
                log(f"{app.name}: browser extension not updated ({getattr(e, 'reason', e)})")
    return done


# --- GNOME Shell extensions -----------------------------------------------

USER_EXT_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                            "gnome-shell", "extensions")
SYSTEM_EXT_DIR = "/usr/share/gnome-shell/extensions"


def extension_dir(app):
    return os.path.join(SYSTEM_EXT_DIR if app.system else USER_EXT_DIR, app.extension)


def extension_version(app, raw=False):
    """Installed version from metadata.json (version-name, else version), or None.
    raw=True gives the integer "version" field instead (GNOME's own counter)."""
    try:
        with open(os.path.join(extension_dir(app), "metadata.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    version = meta.get("version") if raw else meta.get("version-name") or meta.get("version")
    if raw and version is None:
        return None
    return str(version) if version is not None else "0"


def check_extension_zip(path, uuid):
    """Refuse a .zip that is not the expected extension."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            meta = json.loads(z.read("metadata.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        _unlink(path)
        raise HubError(f"{os.path.basename(path)} is not a GNOME Shell extension") from None
    if meta.get("uuid") != uuid:
        _unlink(path)
        raise HubError(f"{os.path.basename(path)} contains {meta.get('uuid')!r}, expected {uuid!r}")


def _shell_list(key):
    out = _run(["gsettings", "get", "org.gnome.shell", key]).stdout.strip()
    if out.startswith("@as "):
        out = out[4:]
    try:
        import ast
        value = ast.literal_eval(out)
        return list(value) if isinstance(value, (list, tuple)) else []
    except (ValueError, SyntaxError):
        return []


def _shell_set_list(key, values):
    return _run(["gsettings", "set", "org.gnome.shell", key,
                 "[" + ", ".join(repr(v) for v in values) + "]"]).returncode == 0


def enable_extension(uuid):
    """Turn an installed extension on for this user (takes effect at the next login)."""
    if not shutil.which("gsettings"):
        return ["turn it on in the Extensions app"]
    enabled, disabled = _shell_list("enabled-extensions"), _shell_list("disabled-extensions")
    ok = uuid in enabled or _shell_set_list("enabled-extensions", enabled + [uuid])
    if uuid in disabled:
        _shell_set_list("disabled-extensions", [u for u in disabled if u != uuid])
    if not ok:
        return ["could not turn it on; use the Extensions app"]
    notes = ["turned on; log out and back in to load it"]
    if _run(["gsettings", "get", "org.gnome.shell", "disable-user-extensions"]).stdout.strip() == "true":
        notes.append("extensions are switched off in the Extensions app; switch them on")
    return notes


def disable_extension(uuid):
    if shutil.which("gsettings"):
        enabled = _shell_list("enabled-extensions")
        if uuid in enabled:
            _shell_set_list("enabled-extensions", [u for u in enabled if u != uuid])


def install_user_extension(app, path, log=None):
    """Install an extension .zip for this user only (no password)."""
    if not shutil.which("gnome-extensions"):
        raise HubError("gnome-extensions is missing (it comes with gnome-shell)")
    rc, out = run_command(["gnome-extensions", "install", "--force", path], log)
    if rc != 0:
        raise HubError(f"{app.name}: gnome-extensions install failed: {out.strip()[-200:]}")


def root_steps(staged):
    """Helper plan steps for downloaded apps that need root (.deb files, system extensions)."""
    debs = [(a.package, p) for a, p in staged if not a.extension]
    steps = install_plan(debs) if debs else []
    steps += [["install-extension", a.extension, p, "gdm" if a.gdm else ""]
              for a, p in staged if a.extension and a.system]
    return steps


def install_staged(staged, log=None):
    """Install downloaded apps: personal extensions directly, the rest with one password
    prompt. Returns (rc, output)."""
    failed, out = 0, ""
    for app, path in staged:
        if app.extension and not app.system:
            try:
                install_user_extension(app, path, log)
                finish_install(app, log)
            except HubError as e:
                failed = 1
                if log:
                    log(str(e))
    steps = root_steps(staged)
    rc = 0
    if steps:
        rc, out = run_helper(steps, log)
        if rc == 0:
            for app, _ in staged:
                if not app.extension or app.system:
                    finish_install(app, log)
    return (rc or failed), out


def uninstall_extension(app, log=None):
    """Remove an extension (system ones through the helper). Returns (rc, output)."""
    disable_extension(app.extension)
    if app.system:
        return run_helper([["remove-extension", app.extension]], log)
    path = extension_dir(app)
    shutil.rmtree(path, ignore_errors=True)
    if log:
        log(f"removed {path}; log out and back in to unload it")
    return 0, ""


def open_extension(app):
    """Open an extension's settings, or the Extensions app."""
    for cmd in (["gnome-extensions", "prefs", app.extension], ["gtk-launch", "org.gnome.Extensions"],
                ["gtk-launch", "com.mattjakeman.ExtensionManager"]):
        if shutil.which(cmd[0]) and subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                                   stderr=subprocess.DEVNULL).returncode == 0:
            return
    raise HubError(f"log out and back in to load {app.name}, then open it in the Extensions app")


# --- privileged helper ----------------------------------------------------

AUTH_CANCELLED = (126, 127)


def install_plan(items):
    """items: [(package, path)] -> helper plan installing those .deb files."""
    return [["install-deb", *[x for pkg, path in items for x in (pkg, path)]]]


def helper_command(plan):
    cmd = [HELPER, json.dumps(plan)]
    return cmd if os.geteuid() == 0 else ["pkexec", *cmd]


def run_command(cmd, on_line=None):
    """Run cmd, streaming merged output lines. Returns (returncode, full_output)."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1)
    except OSError as e:
        return 1, str(e)
    lines = []
    for line in proc.stdout:
        lines.append(line)
        if on_line:
            on_line(line.rstrip("\n"))
    return proc.wait(), "".join(lines)


def run_helper(plan, on_line=None):
    """Run a root plan through pkexec (one password prompt for the whole plan)."""
    return run_command(helper_command(plan), on_line)


def launch(app):
    if app.extension:
        return open_extension(app)
    names = [app.desktop] if app.desktop else []
    names += [os.path.basename(p) for p in _package_desktop_files(app.installed_as or app.package)]
    for name in names:
        if name and shutil.which("gtk-launch"):
            if subprocess.run(["gtk-launch", name.removesuffix(".desktop")],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return
    raise HubError(f"could not find a launcher for {app.name}")


def _package_desktop_files(package):
    out = _run(["dpkg", "-L", package]).stdout.splitlines()
    return [p for p in out if p.startswith("/usr/share/applications/") and p.endswith(".desktop")]
