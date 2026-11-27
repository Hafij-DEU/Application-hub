"""Browser deep clean: finds the right folders, never leaves $HOME, skips running browsers."""
import os

from app_hub import browser_clean as bc


def make(path, size=10):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)


def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(bc, "HOME", os.path.realpath(home))
    monkeypatch.setattr(bc, "running", lambda procs: False)
    return home


def test_scan_groups_chromium_and_firefox(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    cfg = home / ".config/google-chrome"
    make(str(cfg / "Default/Preferences"))
    make(str(cfg / "Default/GPUCache/data_0"))
    make(str(cfg / "Default/History"))
    make(str(cfg / "Default/Bookmarks"))
    make(str(home / ".cache/google-chrome/Default/Cache/Cache_Data/f_1"), 100)
    ff = home / ".mozilla/firefox/abc.default"
    make(str(ff / "prefs.js"))
    make(str(ff / "cookies.sqlite"))
    make(str(home / ".cache/mozilla/firefox/abc.default/cache2/entries/1"), 50)

    groups = {g["browser"]: g for g in bc.scan()}
    chrome = {i["kind"]: i for i in groups["Google Chrome"]["items"]}
    assert chrome["cache"]["selected"] and chrome["cache"]["size"] == 100
    assert chrome["gpu"]["selected"]
    assert not chrome["history"]["selected"]          # privacy data is opt-in
    every = [p for i in chrome.values() for p in i["paths"]]
    assert not any(p.endswith("Bookmarks") or p.endswith("Preferences") for p in every)
    fox = {i["kind"]: i for i in groups["Firefox"]["items"]}
    assert fox["cache"]["paths"][0].endswith("abc.default/cache2")
    assert not fox["cookies"]["selected"]


def test_delete_refuses_outside_home_and_symlinks(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    make(str(outside / "keep"))
    os.symlink(outside, home / "link")
    inside = home / ".cache/x/Cache"
    make(str(inside / "a"))
    log = []
    failed = bc.delete([{"label": "t", "procs": [],
                         "paths": [str(outside), str(home / "link"), str(inside)]}], log.append)
    assert failed == 2
    assert (outside / "keep").exists() and os.path.islink(home / "link")
    assert not inside.exists()


def test_delete_skips_running_browser(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    monkeypatch.setattr(bc, "running", lambda procs: True)
    target = home / ".cache/google-chrome/Default/Cache"
    make(str(target / "a"))
    log = []
    assert bc.delete([{"label": "Chrome", "procs": ["chrome"], "paths": [str(target)]}], log.append) == 1
    assert target.exists() and "running" in log[0]
