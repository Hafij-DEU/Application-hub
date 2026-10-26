# PyInstaller spec for App Hub on Windows 11 (run from an MSYS2 UCRT64 shell, see build.ps1)
#   python -m PyInstaller --noconfirm packaging/windows/app-hub.spec
# Produces dist/App Hub/ with AppHub.exe (the window, no console) and app-hub.exe (command line).

import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent.parent      # noqa: F821  (SPECPATH is set by PyInstaller)
SRC = ROOT / "src"

# Bundle only what App Hub uses from GTK: GTK 4 + libadwaita, the Adwaita icons, English.
GI_CONFIG = {"gi": {"module-versions": {"Gtk": "4.0", "Gdk": "4.0", "Gsk": "4.0", "Adw": "1"},
                    "icons": ["Adwaita", "hicolor"], "themes": ["Adwaita"], "languages": ["en_US"]}}

a = Analysis(  # noqa: F821
    [str(ROOT / "bin" / "app-hub.pyw")],
    pathex=[str(SRC)],
    datas=[(str(SRC / "catalog.json"), "."), (str(SRC / "library-default.json"), "."),
           (str(SRC / "icons"), "icons")],
    hiddenimports=["gi.repository.Adw", "gi.repository.Gtk", "gi.repository.Gdk", "gi.repository.Gsk",
                   "gi.repository.Graphene", "gi.repository.GdkPixbuf", "gi.repository.Pango", "winreg",
                   *[f"app_hub.{m}" for m in ("ai", "cleaner", "cli", "core", "favorites", "gui", "helper",
                                              "history", "installer", "inventory", "ipc", "library",
                                              "updates", "winget", "winsys")]],
    hooksconfig=GI_CONFIG,
    excludes=["tkinter", "unittest", "pydoc"],
)
pyz = PYZ(a.pure)  # noqa: F821

gui = EXE(  # noqa: F821
    pyz, a.scripts, [], exclude_binaries=True,
    name="AppHub", icon=str(ROOT / "data" / "app-hub.ico"),
    # APPHUB_CONSOLE=1 builds the window with a console, for --selftest in CI.
    console=os.environ.get("APPHUB_CONSOLE") == "1",
)
cli = EXE(  # noqa: F821
    pyz, a.scripts, [], exclude_binaries=True,
    name="app-hub", icon=str(ROOT / "data" / "app-hub.ico"),
    console=True,
)

COLLECT(gui, cli, a.binaries, a.datas, name="App Hub")  # noqa: F821
