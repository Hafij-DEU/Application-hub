"""Helper: missing arguments die cleanly; package-owned extensions are never removed."""
import os

import pytest

from conftest import load_helper

helper = load_helper()


@pytest.mark.parametrize("op", ["snap-refresh", "flatpak-update"])
def test_missing_argument_dies_not_traceback(op, capsys):
    r = helper.Runner(simulate=True)
    with pytest.raises(SystemExit) as e:
        r.step(op, [])
    assert e.value.code == 2
    assert f"{op} needs 1 argument" in capsys.readouterr().err


@pytest.mark.parametrize("op", ["snap-refresh", "flatpak-update"])
def test_main_missing_argument_exit_code(op, capsys):
    with pytest.raises(SystemExit) as e:
        helper.main(["--simulate", f'[["{op}"]]'])
    assert e.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_single_argument_still_works(capsys):
    r = helper.Runner(simulate=True)
    assert r.step("snap-refresh", ["firefox"]) == 0
    assert "snap refresh firefox" in capsys.readouterr().out


def _ext_dir(tmp_path, monkeypatch, owner):
    monkeypatch.setattr(helper, "SYSTEM_EXT_DIR", str(tmp_path))
    monkeypatch.setattr(helper, "dpkg_owner", lambda path: owner)
    gdm = []
    monkeypatch.setattr(helper.Runner, "gdm_extension", lambda self, u, on: gdm.append((u, on)))
    (tmp_path / "x@y.org").mkdir()
    (tmp_path / "x@y.org" / "metadata.json").write_text("{}")
    return gdm


def test_remove_extension_refuses_package_owned(tmp_path, monkeypatch, capsys):
    gdm = _ext_dir(tmp_path, monkeypatch, "gnome-shell-ubuntu-extensions")
    with pytest.raises(SystemExit):
        helper.Runner(simulate=False).remove_extension("x@y.org")
    assert os.path.isdir(tmp_path / "x@y.org")
    assert gdm == []
    assert "belongs to package" in capsys.readouterr().err


def test_remove_extension_still_removes_own(tmp_path, monkeypatch):
    gdm = _ext_dir(tmp_path, monkeypatch, None)
    assert helper.Runner(simulate=False).remove_extension("x@y.org") == 0
    assert not os.path.exists(tmp_path / "x@y.org")
    assert gdm == [("x@y.org", False)]


@pytest.mark.skipif(not os.path.isdir("/usr/share/gnome-shell/extensions/ubuntu-dock@ubuntu.com"),
                    reason="no packaged ubuntu-dock here")
def test_dpkg_owner_real_system_extension():
    assert helper.dpkg_owner("/usr/share/gnome-shell/extensions/ubuntu-dock@ubuntu.com")
    assert helper.dpkg_owner("/usr/share/gnome-shell/extensions/nope@example.invalid") is None
