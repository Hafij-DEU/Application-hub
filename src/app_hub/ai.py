"""Ask Gemini (with Google Search) how to install an app, then check its answer.

Gemini may only propose structured recipes (apt, snap, flatpak, github,
deb-url, appimage-url). Each one is verified locally (apt cache, snapd,
flatpak remote, GitHub API, HTTPS HEAD) before App Hub offers to install it.
Shell commands from the model are shown as notes and never run.
"""

import json
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import core, favorites, inventory

CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(inventory.HOME, ".config"),
                      "app-hub", "settings.json")
DEFAULT_MODEL = "gemini-2.5-flash"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CHANNELS = ("apt", "flatpak", "snap", "github", "deb-url", "appimage-url")


# --- settings -------------------------------------------------------------

def load_settings():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    os.makedirs(os.path.dirname(CONFIG), mode=0o700, exist_ok=True)
    tmp = CONFIG + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, CONFIG)


def api_key():
    return os.environ.get("GEMINI_API_KEY") or load_settings().get("gemini_api_key", "")


def model():
    return load_settings().get("gemini_model") or DEFAULT_MODEL


# --- result model ---------------------------------------------------------

@dataclass
class Option:
    recipe: favorites.Recipe
    verified: bool = False
    status: str = ""          # what the local check found
    notes: str = ""           # model's notes (may include commands; shown, never run)


@dataclass
class Found:
    name: str
    summary: str = ""
    homepage: str = ""
    options: list = field(default_factory=list)


@dataclass
class Answer:
    apps: list
    sources: list             # [(title, uri)] from Google Search grounding
    raw: str = ""


# --- Gemini ---------------------------------------------------------------

def _system_prompt():
    distro, version = core.os_release()
    return f"""You help install desktop apps on {distro} {version} ({core.dpkg_arch()}).
Search the web for the app the user asks about (or the best apps matching their description,
at most 3). For each app list the official ways to install it on this system, best first:
an Ubuntu/Debian archive package, Flathub, the Snap Store, the developer's GitHub releases
.deb, a direct .deb download, an AppImage download.

Reply with ONLY this JSON, no prose, no code fences:
{{"apps": [{{"name": "...", "summary": "one line", "homepage": "https://...",
  "options": [
    {{"channel": "apt", "id": "<debian package name>"}},
    {{"channel": "flatpak", "id": "<app id like org.example.App>", "remote": "flathub"}},
    {{"channel": "snap", "id": "<snap name>", "snap_channel": "stable", "classic": false}},
    {{"channel": "github", "id": "<owner>/<repo>", "asset_pattern": "<optional regex for the .deb>"}},
    {{"channel": "deb-url", "url": "https://... .deb (a stable 'latest' link if one exists)"}},
    {{"channel": "appimage-url", "url": "https://... .AppImage"}}
  ],
  "notes": "anything else, e.g. if it needs a third-party apt repository"}}]}}
Only include options you found evidence for. Never invent ids or URLs. Use https URLs only."""


def _extract_json(text):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise core.HubError("Gemini did not return JSON")
    return json.loads(text[start:end + 1])


def ask_gemini(query, system=None):
    key = api_key()
    if not key:
        raise core.HubError("Add your Gemini API key in Preferences first")
    body = {
        "systemInstruction": {"parts": [{"text": system or _system_prompt()}]},
        "contents": [{"role": "user", "parts": [{"text": query}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2},
    }
    req = urllib.request.Request(
        ENDPOINT.format(model=urllib.parse.quote(model())), data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key,
                 "User-Agent": core.USER_AGENT}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise core.HubError(f"Gemini error {e.code}: {msg or e.reason}") from None
    except (urllib.error.URLError, OSError) as e:
        raise core.HubError(f"could not reach Gemini ({getattr(e, 'reason', e)})") from None

    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    sources = []
    for chunk in cand.get("groundingMetadata", {}).get("groundingChunks", []):
        web = chunk.get("web") or {}
        if web.get("uri"):
            sources.append((web.get("title") or web["uri"], web["uri"]))
    if not text:
        raise core.HubError("Gemini returned no answer (" + cand.get("finishReason", "unknown") + ")")
    try:
        parsed = _extract_json(text)
    except ValueError:
        raise core.HubError("could not read Gemini's answer") from None
    return parsed, sources, text


# --- verification ---------------------------------------------------------

def _head_ok(url):
    try:
        favorites._check_https(url)
    except core.HubError as e:
        return False, str(e)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": core.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            size = int(resp.headers.get("Content-Length") or 0)
            host = urllib.parse.urlsplit(resp.geturl()).netloc
            return True, f"download from {host}" + (f" · {core.human_size(size)}" if size else "")
    except urllib.error.HTTPError as e:
        return False, f"link returns HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"link unreachable ({getattr(e, 'reason', e)})"


def verify(recipe, cache):
    ch = recipe.channel
    if ch == "apt":
        if cache is not None and recipe.id in cache and cache[recipe.id].candidate:
            cand = cache[recipe.id].candidate
            if [o for o in cand.origins if o.archive != "now" and o.site]:
                recipe.summary = recipe.summary or cand.summary
                return True, f"in your software sources · {cand.version}"
        return False, "not in your software sources"
    if ch == "snap":
        found = inventory.snapd(f"/v2/find?name={urllib.parse.quote(recipe.id)}")
        if not found:
            return False, "not found in the Snap Store"
        s = found[0]
        recipe.classic = s.get("confinement") == "classic"
        return True, f"Snap Store · {s.get('version', '')}" + (" · classic" if recipe.classic else "")
    if ch == "flatpak":
        r = inventory._run(["flatpak", "remote-info", recipe.remote or "flathub", recipe.id], 40)
        if r.returncode == 0:
            ver = re.search(r"Version:\s*(\S+)", r.stdout)
            return True, f"{recipe.remote or 'flathub'}" + (f" · {ver.group(1)}" if ver else "")
        return False, f"not found on {recipe.remote or 'flathub'}"
    if ch == "github":
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", recipe.id):
            return False, "not a GitHub owner/repo"
        app = core.App(id="x", name=recipe.name, summary="", repo=recipe.id, package="",
                       asset_pattern=recipe.asset_pattern)
        try:
            rel = core.fetch_release(app)
        except core.HubError as e:
            return False, f"GitHub: {e}"
        if not rel.asset_url:
            return False, f"{rel.tag} has no .deb for this system"
        return True, f"{rel.asset_name} · {core.human_size(rel.size)}"
    if ch in ("deb-url", "appimage-url"):
        return _head_ok(recipe.url)
    return False, "unsupported channel"


def find(query, log=lambda s: None):
    """Ask Gemini, verify each option. Returns an Answer."""
    log(f"asking {model()} …")
    parsed, sources, raw = ask_gemini(query)
    cache = inventory.open_apt_cache()
    apps = []
    for entry in parsed.get("apps", [])[:3]:
        name = str(entry.get("name") or query).strip()
        found = Found(name, str(entry.get("summary", "")), str(entry.get("homepage", "")))
        notes = str(entry.get("notes", ""))
        for opt in entry.get("options", [])[:8]:
            ch = opt.get("channel")
            if ch not in CHANNELS:
                continue
            recipe = favorites.Recipe(
                ch, str(opt.get("id") or opt.get("url") or name), name, summary=found.summary,
                remote=str(opt.get("snap_channel") or opt.get("remote") or ""),
                classic=bool(opt.get("classic")), url=str(opt.get("url", "")),
                asset_pattern=str(opt.get("asset_pattern", "")))
            if ch == "snap" and recipe.remote in ("stable", "latest/stable"):
                recipe.remote = ""
            if ch in ("deb-url", "appimage-url"):
                recipe.id = re.sub(r"[^A-Za-z0-9._-]+", "-", name).lower()
            if ch == "apt":
                recipe.package = recipe.id
            log(f"checking {favorites.CHANNEL_LABELS[ch]}: {recipe.describe()}")
            ok, status = verify(recipe, cache)
            found.options.append(Option(recipe, ok, status, notes))
        found.options.sort(key=lambda o: not o.verified)
        apps.append(found)
    cache = None
    core.release_memory()
    return Answer(apps, sources, raw)


def suggest_repos(query):
    """GitHub repositories (owner/repo) that publish Linux .deb releases for `query`."""
    system = f"""You find GitHub repositories of desktop apps for Linux ({core.dpkg_arch()}).
Search the web for the app the user names or describes (at most 6 matches). Only list
repositories whose GitHub Releases include a .deb package. Never invent repositories.
Reply with ONLY this JSON, no prose, no code fences:
{{"apps": [{{"name": "App name", "summary": "what it does, under 60 characters",
  "repo": "owner/repo"}}]}}"""
    parsed, _sources, _raw = ask_gemini(query, system)
    out = []
    for e in parsed.get("apps", [])[:6]:
        repo = str(e.get("repo", "")).strip().removeprefix("https://github.com/").strip("/")
        if re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            out.append({"repo": repo, "name": str(e.get("name", ""))[:40],
                        "summary": str(e.get("summary", ""))[:90]})
    if not out:
        raise core.HubError("Gemini found no GitHub project with .deb releases")
    return out
