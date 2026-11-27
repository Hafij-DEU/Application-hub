"""Per-browser deep clean: find each installed browser's caches and data,
grouped by kind, so the user sees exactly what goes before choosing.

  python3 browser_clean.py scan            -> JSON list of cleaner groups
  python3 browser_clean.py delete JSON     -> delete [{"paths": [...], "procs": [...]}]

Shared by Harbor Settings (embedded) and App Hub (imported). Only paths
inside $HOME are ever deleted, symlinks are never followed, and a browser
that is running is skipped (deleting under a live browser corrupts it).
"""
import glob
import json
import os
import shutil
import subprocess
import sys

HOME = os.path.realpath(os.path.expanduser("~"))

# (kind id, label, what it is, selected by default, chromium names, firefox names)
# Names are relative to a profile dir ("p:"), the browser config root ("r:")
# or the browser cache root ("c:" profile-relative, "cr:" root-relative).
KINDS = [
    ("cache", "Web cache", "pages, images and scripts saved for faster loading",
     True,
     ["c:Cache", "c:Code Cache", "p:Cache", "p:Code Cache"],
     ["c:cache2", "c:startupCache", "c:jumpListCache", "c:thumbnails", "p:startupCache"]),
    ("gpu", "GPU and shader cache", "compiled graphics shaders, rebuilt on demand",
     True,
     ["p:GPUCache", "p:DawnGraphiteCache", "p:DawnWebGPUCache", "p:DawnCache",
      "r:GrShaderCache", "r:ShaderCache", "r:GraphiteDawnCache", "cr:GrShaderCache",
      "cr:ShaderCache"],
     ["p:shader-cache", "c:shader-cache"]),
    ("sw", "Offline site cache", "service-worker caches of websites; sites refill them",
     True,
     ["p:Service Worker/CacheStorage", "p:Service Worker/ScriptCache"],
     []),
    ("crash", "Crash reports and logs", "old crash dumps and telemetry pings",
     True,
     ["r:Crash Reports", "r:Crashpad", "r:BrowserMetrics", "p:BrowserMetrics"],
     ["r:Crash Reports", "p:crashes", "p:minidumps", "p:datareporting/archived",
      "p:saved-telemetry-pings"]),
    ("components", "Downloaded update files", "component and extension installers already applied",
     True,
     ["r:component_crx_cache", "r:extensions_crx_cache", "p:Extensions/Temp"],
     []),
    ("history", "Browsing history", "visited pages and typed addresses (bookmarks are kept)",
     False,
     ["p:History", "p:History-journal", "p:Visited Links", "p:Top Sites",
      "p:Top Sites-journal", "p:Shortcuts", "p:Shortcuts-journal"],
     ["p:formhistory.sqlite", "p:sessionstore-backups"]),
    ("cookies", "Cookies and site storage", "signs you out of every website",
     False,
     ["p:Cookies", "p:Cookies-journal", "p:Network/Cookies", "p:Network/Cookies-journal",
      "p:Local Storage", "p:Session Storage", "p:IndexedDB", "p:WebStorage",
      "p:Service Worker/Database"],
     ["p:cookies.sqlite", "p:cookies.sqlite-wal", "p:webappsstore.sqlite", "p:storage/default"]),
]

# (name, family, process names, [(config root, cache root), ...])
S = "~/snap"
F = "~/.var/app"
BROWSERS = [
    ("Firefox", "firefox", ["firefox", "firefox-bin"], [
        ("~/.mozilla/firefox", "~/.cache/mozilla/firefox"),
        (S + "/firefox/common/.mozilla/firefox", S + "/firefox/common/.cache/mozilla/firefox"),
        (F + "/org.mozilla.firefox/.mozilla/firefox", F + "/org.mozilla.firefox/cache/mozilla/firefox"),
    ]),
    ("LibreWolf", "firefox", ["librewolf"], [
        ("~/.librewolf", "~/.cache/librewolf"),
        (F + "/io.gitlab.librewolf-community/.librewolf", F + "/io.gitlab.librewolf-community/cache/librewolf"),
    ]),
    ("Google Chrome", "chromium", ["chrome"], [
        ("~/.config/google-chrome", "~/.cache/google-chrome"),
        (F + "/com.google.Chrome/config/google-chrome", F + "/com.google.Chrome/cache/google-chrome"),
    ]),
    ("Chromium", "chromium", ["chromium", "chromium-browse", "chromium-browser"], [
        ("~/.config/chromium", "~/.cache/chromium"),
        (S + "/chromium/common/chromium", S + "/chromium/common/.cache/chromium"),
        (F + "/org.chromium.Chromium/config/chromium", F + "/org.chromium.Chromium/cache/chromium"),
    ]),
    ("Brave", "chromium", ["brave"], [
        ("~/.config/BraveSoftware/Brave-Browser", "~/.cache/BraveSoftware/Brave-Browser"),
        (S + "/brave/current/.config/BraveSoftware/Brave-Browser",
         S + "/brave/common/.cache/BraveSoftware/Brave-Browser"),
        (F + "/com.brave.Browser/config/BraveSoftware/Brave-Browser",
         F + "/com.brave.Browser/cache/BraveSoftware/Brave-Browser"),
    ]),
    ("Microsoft Edge", "chromium", ["msedge"], [
        ("~/.config/microsoft-edge", "~/.cache/microsoft-edge"),
        (F + "/com.microsoft.Edge/config/microsoft-edge", F + "/com.microsoft.Edge/cache/microsoft-edge"),
    ]),
    ("Opera", "chromium", ["opera"], [
        ("~/.config/opera", "~/.cache/opera"),
        (S + "/opera/current/.config/opera", S + "/opera/common/.cache/opera"),
        (F + "/com.opera.Opera/config/opera", F + "/com.opera.Opera/cache/opera"),
    ]),
    ("Vivaldi", "chromium", ["vivaldi-bin", "vivaldi"], [
        ("~/.config/vivaldi", "~/.cache/vivaldi"),
        (F + "/com.vivaldi.Vivaldi/config/vivaldi", F + "/com.vivaldi.Vivaldi/cache/vivaldi"),
    ]),
    ("Yandex Browser", "chromium", ["yandex_browser"], [
        ("~/.config/yandex-browser", "~/.cache/yandex-browser"),
    ]),
]


def inside_home(path):
    """True for a real (non-symlink) path strictly inside $HOME."""
    try:
        if os.path.islink(path):
            return False
        real = os.path.realpath(path)
    except OSError:
        return False
    return real.startswith(HOME + os.sep) and real != HOME


def size_of(path):
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not os.path.isdir(path) or os.path.islink(path):
        return st.st_size
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def profiles(family, root):
    """Profile dirs of one browser root."""
    if not os.path.isdir(root):
        return []
    if family == "firefox":
        found = [d for d in glob.glob(os.path.join(root, "*"))
                 if os.path.isfile(os.path.join(d, "prefs.js")) or os.path.isfile(os.path.join(d, "times.json"))]
        return sorted(found)
    found = []
    for d in glob.glob(os.path.join(root, "*")):
        name = os.path.basename(d)
        if name in ("Default", "System Profile", "Guest Profile") or name.startswith("Profile "):
            found.append(d)
    if not found and os.path.isfile(os.path.join(root, "Preferences")):
        found.append(root)  # Opera keeps its single profile in the root
    return sorted(found)


def paths_for(kind, family, root, cache_root):
    names = kind[4] if family == "chromium" else kind[5]
    out = []
    profs = profiles(family, root)
    for n in names:
        where, rel = n.split(":", 1)
        if where == "r":
            out.append(os.path.join(root, rel))
        elif where == "cr":
            out.append(os.path.join(cache_root, rel))
        else:
            for p in profs:
                if where == "p":
                    out.append(os.path.join(p, rel))
                else:  # c: the profile's twin under the cache root
                    twin = os.path.join(cache_root, os.path.relpath(p, root)) if p != root else cache_root
                    out.append(os.path.join(twin, rel))
    seen, keep = set(), []
    for p in out:
        if p not in seen and os.path.lexists(p) and inside_home(p):
            seen.add(p)
            keep.append(p)
    return keep


def running(procs):
    try:
        r = subprocess.run(["ps", "-eo", "comm="], capture_output=True, text=True, timeout=10)
    except Exception:
        return False
    names = {ln.strip() for ln in r.stdout.splitlines()}
    return any(p in names for p in procs)


def short(path):
    return "~" + path[len(HOME):] if path.startswith(HOME) else path


def scan():
    groups = []
    for name, family, procs, places in BROWSERS:
        roots = []
        for cfg, cache in places:
            cfg, cache = os.path.expanduser(cfg), os.path.expanduser(cache)
            if os.path.isdir(cfg) or os.path.isdir(cache):
                roots.append((cfg, cache))
        if not roots:
            continue
        items = []
        nprof = sum(len(profiles(family, r)) for r, _c in roots)
        for kind in KINDS:
            paths = []
            for cfg, cache in roots:
                paths += paths_for(kind, family, cfg, cache)
            if not paths:
                continue
            size = sum(size_of(p) for p in paths)
            if size <= 0:
                continue
            items.append({
                "kind": kind[0],
                "label": kind[1],
                "detail": kind[2],
                "size": size,
                "selected": kind[3],
                "paths": paths,
                "path": "\n".join(short(p) for p in paths),
                "procs": procs,
                "step": [],
            })
        if not items:
            continue
        is_running = running(procs)
        if is_running:  # nothing pre-ticked: it would be skipped anyway
            for it in items:
                it["selected"] = False
        where = ", ".join(sorted({"Snap" if "/snap/" in c else "Flatpak" if "/.var/app/" in c else "deb"
                                  for c, _x in roots}))
        groups.append({
            "title": f"{name} deep clean",
            "description": (f"{nprof} profile{'s' if nprof != 1 else ''} ({where}). "
                            "Each row says what it deletes and lists the exact folders. "
                            "Bookmarks, passwords and extensions are never touched."),
            "note": (f"{name} is running: close it first, or it is skipped." if is_running else ""),
            "granular": True,
            "action": "delete",
            "browser": name,
            "running": is_running,
            "items": items,
            "root_plan": "",
            "user_cmds": [],
        })
    return groups


def delete(entries, say=print):
    """entries: [{"paths": [...], "procs": [...], "label": str}]. Returns failures."""
    failed = 0
    for e in entries:
        if running(e.get("procs", [])):
            say(f"skipped {e.get('label', '')}: the browser is running, close it and try again")
            failed += 1
            continue
        for p in e.get("paths", []):
            p = os.path.expanduser(p)
            if not inside_home(p):
                say(f"refused {p}: not inside your home folder")
                failed += 1
                continue
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.unlink(p)
                say(f"deleted {short(p)}")
            except FileNotFoundError:
                pass
            except OSError as err:
                say(f"could not delete {short(p)}: {err.strerror}")
                failed += 1
    return failed


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "scan":
        print(json.dumps(scan()))
    elif len(sys.argv) >= 3 and sys.argv[1] == "delete":
        sys.exit(1 if delete(json.loads(sys.argv[2]), lambda m: print(m, flush=True)) else 0)
    else:
        print(__doc__)
        sys.exit(2)
