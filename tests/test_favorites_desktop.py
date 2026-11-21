"""AppImage .desktop files must not let name/summary text inject keys."""
import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from app_hub import favorites  # noqa: E402


def _parse(text):
    kf = GLib.KeyFile()
    kf.load_from_data(text, len(text.encode()), GLib.KeyFileFlags.NONE)
    return kf


def test_newline_in_name_cannot_inject_exec():
    dest = "/home/u/Applications/Evil.AppImage"
    text = favorites.appimage_desktop_entry(
        "Evil\nExec=rm -rf ~\n[Desktop Action x]", "line1\r\nExec=sh -c bad\tend\\n", dest)
    assert text.count("\nExec=") == 1
    kf = _parse(text)
    assert kf.get_groups()[0] == ["Desktop Entry"]
    assert kf.get_keys("Desktop Entry")[0].count("Exec") == 1
    assert kf.get_string("Desktop Entry", "Exec") == f'"{dest}" %U'
    assert "\n" not in kf.get_string("Desktop Entry", "Name")
    assert kf.get_string("Desktop Entry", "Comment") == "line1 Exec=sh -c bad end\\n"


def test_exec_path_with_special_chars_round_trips():
    dest = '/home/a b/$x`"\\.AppImage'
    kf = _parse(favorites.appimage_desktop_entry("Ok", "", dest))
    ok, argv = GLib.shell_parse_argv(kf.get_string("Desktop Entry", "Exec"))
    assert argv[0] == dest and argv[1] == "%U"
