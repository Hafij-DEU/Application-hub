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
    icon: str = ""                                 # icon file (Library apps)
    owner: str = ""                                # Library group; "" = My Apps
    section: str = ""                              # "self" = App Hub itself, "mine" = added to My Apps
    release: Release | None = None
    error: str = ""

    @property
    def repo_url(self):
        return f"https://github.com/{self.repo}"

    @property
    def update_available(self):
        if not (self.installed and self.release and self.release.asset_url):
            return False
        return self.renamed or is_newer(self.release.version, self.installed)

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
            parts.append(f"{self.release.tag} has no .deb for this system")
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
        app.installed, app.installed_as = None, ""
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
            data = json.load(resp)
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


def fetch_release(app):
    data = _get_json(f"{API}/repos/{app.repo}/releases/latest")
    skipped = ""
    if not pick_asset(data.get("assets", []), app.asset_pattern):
        # The newest release may ship only other platforms (e.g. a Windows-only fix):
        # use the newest stable release that has a .deb for this system.
        try:
            recent = _get_json(f"{API}/repos/{app.repo}/releases?per_page=10")
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
        rel.size = asset.get("size", 0)
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            rel.sha256 = digest[7:]
    sums = next((a for a in assets if a["name"].upper().startswith("SHA256SUMS")), None)
    if sums:
        rel.sums_url = sums["browser_download_url"]
    return rel


def check_all(apps, on_done=None):
    """Fetch latest releases in parallel; on_done(app) runs in a worker thread."""
    def one(app):
        try:
            app.release, app.error = fetch_release(app), ""
        except HubError as e:
            app.release, app.error = None, str(e)
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
        raise HubError(f"{app.name}: no .deb to download")
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
