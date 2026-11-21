"""Config folders of installed software must not be offered as leftovers."""
import os

from app_hub import cleaner


def _fake_system(tmp_path, monkeypatch, removed_names):
    home = tmp_path / "home"
    cfg = home / ".config"
    for d in ("gridcraft", "oldapp", "tool"):
        (cfg / d).mkdir(parents=True)
        os.utime(cfg / d, (1, 1))
    monkeypatch.setattr(cleaner, "HOME", str(home))
    monkeypatch.setattr(cleaner, "DATA_ROOTS", [str(cfg)])
    monkeypatch.setattr(cleaner, "installed_keys", lambda apps: set())
    monkeypatch.setattr(cleaner, "residual_packages", lambda: list(removed_names))
    monkeypatch.setattr(cleaner.core, "load_catalog", lambda: [])
    monkeypatch.setattr(cleaner.inventory, "snapd", lambda path: [])
    return home


def test_installed_names_from_fake_inventory(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gridcraft-cli").write_text("")
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "foo.desktop").write_text("[Desktop Entry]\nExec=env A=1 /opt/Bar/BarApp %U\n")
    info = tmp_path / "info"
    info.mkdir()
    (info / "pkg.list").write_text("/usr/share/mime/packages/ai.storyteller.wordcraft.xml\n/opt/Zed\n")
    names = cleaner.installed_names([str(bin_dir)], [str(apps)], str(info))
    assert {"gridcraft-cli", "foo", "barapp", "ai.storyteller.wordcraft", "zed"} <= names
    assert cleaner.ownership("gridcraft", names) == "owned"      # prefix gridcraft-cli
    assert cleaner.ownership("GridCraft", names) == "owned"      # case-insensitive
    assert cleaner.ownership("wordcraft", names) == "owned"      # reverse-DNS word
    assert cleaner.ownership("barap", names) == "maybe"          # loose prefix
    assert cleaner.ownership("oldapp", names) is None


def test_scan_skips_owned_and_unselects_unsure(tmp_path, monkeypatch):
    home = _fake_system(tmp_path, monkeypatch, ["gridcraft", "oldapp", "tool"])
    names = {"gridcraft-cli", "toolbox"}
    items = cleaner.scan_leftovers([], [], names=names)
    by_path = {i.path: i for i in items}
    cfg = home / ".config"
    assert str(cfg / "gridcraft") not in by_path            # installed: never flagged
    assert by_path[str(cfg / "oldapp")].selected is True    # real leftover still offered
    assert by_path[str(cfg / "tool")].selected is False     # uncertain: listed, not preselected


def test_scan_without_matches_keeps_old_behaviour(tmp_path, monkeypatch):
    home = _fake_system(tmp_path, monkeypatch, ["gridcraft", "oldapp"])
    items = cleaner.scan_leftovers([], [], names=set())
    paths = {i.path for i in items if i.selected}
    assert paths == {str(home / ".config" / "gridcraft"), str(home / ".config" / "oldapp")}
