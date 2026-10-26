"""Ask Gemini (with Google Search) how to install an app on Windows, then check its answer.

Gemini may only propose structured recipes (winget, msstore, github, url). Each one is
verified locally (winget show, the GitHub API, an HTTPS HEAD request) before App Hub
offers to install it. Commands from the model are shown as notes and never run.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import core, favorites, winget, winsys

CONFIG = core.SETTINGS
DEFAULT_MODEL = "gemini-2.5-flash"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CHANNELS = ("winget", "msstore", "github", "url")


# --- settings -------------------------------------------------------------

def load_settings():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    """Saved in %APPDATA%\\App Hub (only this Windows account can read it)."""
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
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
    return f"""You help install desktop apps on {winsys.windows_version()} ({winsys.machine_arch()}).
Search the web for the app the user asks about (or the best apps matching their description,
at most 3). For each app list the official ways to install it on this PC, best first:
the winget community repository, the Microsoft Store, the developer's GitHub releases,
a direct download of the official installer.

Reply with ONLY this JSON, no prose, no code fences:
{{"apps": [{{"name": "...", "summary": "one line", "homepage": "https://...",
  "options": [
    {{"channel": "winget", "id": "<winget package id like Publisher.App>"}},
    {{"channel": "msstore", "id": "<Microsoft Store product id like 9NBLGGH4NNS1 or XP...>"}},
    {{"channel": "github", "id": "<owner>/<repo>", "asset_pattern": "<optional regex for the Windows file>"}},
    {{"channel": "url", "url": "https://... .exe or .msi (a stable 'latest' link if one exists)"}}
  ],
  "notes": "anything else worth knowing"}}]}}
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
        favorites.check_https(url)
    except core.HubError as e:
        return False, str(e)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": core.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            size = int(resp.headers.get("Content-Length") or 0)
            final = resp.geturl()
            host = urllib.parse.urlsplit(final).netloc
            disp = resp.headers.get("Content-Disposition") or ""
    except urllib.error.HTTPError as e:
        return False, f"link returns HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"link unreachable ({getattr(e, 'reason', e)})"
    name = (re.search(r'filename="?([^";]+)', disp).group(1) if "filename" in disp
            else os.path.basename(urllib.parse.urlsplit(final).path))
    if not re.search(r"\.(exe|msi|zip|msix|msixbundle)$", name, re.IGNORECASE):
        return False, f"{host} does not send an installer ({name or 'no file name'})"
    return True, f"{name} from {host}" + (f" · {core.human_size(size)}" if size else "")


def verify(recipe):
    ch = recipe.channel
    if ch in ("winget", "msstore"):
        if not winget.available():
            return False, "winget is not installed (App Installer from the Microsoft Store)"
        info = winget.show(recipe.id, ch)
        if not info:
            return False, f"not found in {'the Microsoft Store' if ch == 'msstore' else 'winget'}"
        recipe.summary = recipe.summary or info.get("Description", "")[:120]
        where = "Microsoft Store" if ch == "msstore" else "winget"
        version = info.get("Version", "")
        publisher = info.get("Publisher", "")
        return True, " · ".join(x for x in (where, version, publisher) if x)
    if ch == "github":
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", recipe.id):
            return False, "not a GitHub owner/repo"
        app = core.App(id="x", name=recipe.name, summary="", repo=recipe.id, asset_pattern=recipe.asset_pattern)
        try:
            rel = core.fetch_release(app)
        except core.HubError as e:
            return False, f"GitHub: {e}"
        if not rel.asset_url:
            return False, f"{rel.tag} has no Windows download"
        return True, f"{rel.asset_name} · {core.human_size(rel.size)}"
    if ch == "url":
        return _head_ok(recipe.url)
    return False, "unsupported channel"


def find(query, log=lambda s: None):
    """Ask Gemini, verify each option. Returns an Answer."""
    log(f"Asking {model()}…")
    parsed, sources, raw = ask_gemini(query)
    apps = []
    for entry in parsed.get("apps", [])[:3]:
        name = str(entry.get("name") or query).strip()
        found = Found(name, str(entry.get("summary", "")), str(entry.get("homepage", "")))
        notes = str(entry.get("notes", ""))
        for opt in entry.get("options", [])[:8]:
            ch = opt.get("channel")
            if ch not in CHANNELS:
                continue
            recipe = favorites.Recipe(ch, str(opt.get("id") or opt.get("url") or name), name,
                                      summary=found.summary, url=str(opt.get("url", "")),
                                      asset_pattern=str(opt.get("asset_pattern", "")))
            if ch == "url":
                recipe.id = re.sub(r"[^A-Za-z0-9._-]+", "-", name).lower()
            log(f"Checking {favorites.CHANNEL_LABELS[ch]}: {recipe.describe()}")
            ok, status = verify(recipe)
            found.options.append(Option(recipe, ok, status, notes))
        found.options.sort(key=lambda o: not o.verified)
        apps.append(found)
    return Answer(apps, sources, raw)


def suggest_repos(query):
    """GitHub repositories (owner/repo) that publish Windows releases for `query`."""
    system = """You find GitHub repositories of desktop apps for Windows.
Search the web for the app the user names or describes (at most 6 matches). Only list
repositories whose GitHub Releases include a Windows installer (.exe, .msi) or a portable
Windows .zip. Never invent repositories.
Reply with ONLY this JSON, no prose, no code fences:
{"apps": [{"name": "App name", "summary": "what it does, under 60 characters",
  "repo": "owner/repo"}]}"""
    parsed, _sources, _raw = ask_gemini(query, system)
    out = []
    for e in parsed.get("apps", [])[:6]:
        repo = str(e.get("repo", "")).strip().removeprefix("https://github.com/").strip("/")
        if re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            out.append({"repo": repo, "name": str(e.get("name", ""))[:40],
                        "summary": str(e.get("summary", ""))[:90]})
    if not out:
        raise core.HubError("Gemini found no GitHub project with Windows releases")
    return out
