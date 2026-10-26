"""Tests that need no network and no GTK:  python -m unittest discover -s tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from app_hub import core, inventory, winget, winsys  # noqa: E402


class Versions(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(core.is_newer("1.8.6", "1.4.7"))
        self.assertTrue(core.is_newer("1.10", "1.9"))
        self.assertFalse(core.is_newer("1.2", "1.2.0"))
        self.assertFalse(core.is_newer("1.2.0", "1.2.0.0"))
        self.assertTrue(core.is_newer("1.2.0", "1.2.0-beta"))
        self.assertTrue(core.is_newer("v0.1.0-r202610191927", "0.1.0-r202610180743"))
        self.assertFalse(core.is_newer("", "1.0"))

    def test_clean_and_from_name(self):
        self.assertEqual(core.clean_version("v1.8.6"), "1.8.6")
        self.assertEqual(core.version_from_name("AppHubSetup-1.0.2.exe"), "1.0.2")
        self.assertEqual(core.version_from_name("Draco Download Manager version 1.8.6"), "1.8.6")
        self.assertEqual(core.version_from_name("7-Zip 24.08 (x64)"), "24.08")


class Assets(unittest.TestCase):
    MARKD = ["latest.yml", "Markd-0.24.5-android-debug.apk", "markd-linux-amd64-0.24.5.deb",
             "markd-linux-x86_64-0.24.5.AppImage", "markd-win-x64-0.24.5-setup.exe",
             "markd-win-x64-0.24.5-setup.exe.blockmap", "markd-win-x64-0.24.5.zip", "SHA256SUMS.txt"]
    PHOTO = ["photocraft-0.16.0-windows-arm64-portable.zip", "photocraft-0.16.0-windows-arm64.msi",
             "photocraft-0.16.0-windows-x64-portable.zip", "photocraft-0.16.0-windows-x64.msi",
             "photocraft-0.16.0-windows-x86-portable.zip", "photocraft-0.16.0-windows-x86.msi",
             "photocraft-web-0.16.0.zip", "photocraft-cli-0.16.0-macos-universal.zip",
             "photocraft-0.16.0-linux-x86_64.deb"]
    DRACO = ["Draco-1.8.6-x86_64.AppImage", "draco-dm_1.8.6_all.deb", "draco-extension-chromium-1.8.6.zip",
             "draco-extension-firefox-1.8.6.zip", "DracoSetup-1.8.6.exe", "SHA256SUMS.txt"]

    def pick(self, names, arch="x64", pattern=""):
        got = core.pick_asset([{"name": n} for n in names], pattern, arch)
        return got["name"] if got else None

    def test_installers_win(self):
        self.assertEqual(self.pick(self.MARKD), "markd-win-x64-0.24.5-setup.exe")
        self.assertEqual(self.pick(self.DRACO), "DracoSetup-1.8.6.exe")
        self.assertEqual(self.pick(self.PHOTO), "photocraft-0.16.0-windows-x64.msi")

    def test_architectures(self):
        self.assertEqual(self.pick(self.PHOTO, "arm64"), "photocraft-0.16.0-windows-arm64.msi")
        self.assertEqual(self.pick(self.PHOTO, "x86"), "photocraft-0.16.0-windows-x86.msi")
        self.assertEqual(self.pick(self.MARKD, "arm64"), "markd-win-x64-0.24.5-setup.exe")   # x64 runs on arm64

    def test_nothing_for_windows(self):
        self.assertIsNone(self.pick(["app_1.0_amd64.deb", "app-1.0.AppImage", "source.zip"]))
        self.assertEqual(self.pick(["tool-1.0-windows.zip"]), "tool-1.0-windows.zip")

    def test_pattern(self):
        self.assertIsNone(self.pick(self.DRACO, pattern=r"^AppHubSetup-.*\.exe$"))


class Winget(unittest.TestCase):
    @staticmethod
    def row(*cells):
        widths = (38, 30, 12, 12)
        return "".join(c.ljust(w) for c, w in zip(cells, widths)) + (cells[4] if len(cells) > 4 else "")

    OUT = ("   - \r   \\ \r" + row.__func__("Name", "Id", "Version", "Available", "Source") + "\n"
           + "-" * 100 + "\n"
           + row.__func__("7-Zip 24.08 (x64)", "7zip.7zip", "24.08", "26.05", "winget") + "\n"
           + row.__func__("Draco Download Manager version 1.4.7", "ARP\\User\\X64\\{6D1F}_is1", "1.4.7") + "\n"
           + row.__func__("PowerToys (Preview) x64", "XP89DCGQ3K6VLD", "0.99.0", "0.102.29", "msstore") + "\n"
           + "3 upgrades available.\n")

    def test_parse(self):
        rows = [winget._normal(r) for r in winget.parse_tables(self.OUT)]
        self.assertEqual(rows[0]["id"], "7zip.7zip")
        self.assertEqual(rows[0]["available"], "26.05")
        self.assertEqual(rows[2]["source"], "msstore")
        self.assertEqual(len(rows), 3)


class Names(unittest.TestCase):
    def test_base_name(self):
        self.assertEqual(inventory.base_name("Draco Download Manager version 1.8.6"), "Draco Download Manager")
        self.assertEqual(inventory.base_name("7-Zip 24.08 (x64)"), "7-Zip")
        self.assertEqual(inventory.base_name("Markd 0.24.5"), "Markd")

    def test_icon_spec(self):
        self.assertEqual(winsys.parse_icon_spec('"C:\\A B\\app.exe",-101'), ("C:\\A B\\app.exe", -101))
        self.assertEqual(winsys.parse_icon_spec("C:\\x\\app.exe,0"), ("C:\\x\\app.exe", 0))
        self.assertEqual(winsys.parse_icon_spec("C:\\x\\app.ico"), ("C:\\x\\app.ico", 0))

    def test_catalog_match(self):
        apps = {a.id: a for a in core.load_catalog(False)}
        self.assertTrue(core.compile_match(apps["draco"]).search("Draco Download Manager version 1.4.7"))
        self.assertTrue(core.compile_match(apps["markd"]).search("Markd 0.24.5"))
        self.assertFalse(core.compile_match(apps["markd"]).search("Markdown Monster"))
        self.assertTrue(core.compile_match(apps["app-hub"]).search("App Hub version 1.0.0"))
        self.assertFalse(core.compile_match(apps["app-hub"]).search("App Hub Helper Tools"))


class Ignore(unittest.TestCase):
    def setUp(self):
        import tempfile
        from app_hub import updates
        self.updates = updates
        self.tmp = tempfile.mkdtemp()
        self.saved, updates.IGNORED = updates.IGNORED, os.path.join(self.tmp, "ignored.json")

    def tearDown(self):
        self.updates.IGNORED = self.saved

    def pending(self, new):
        from app_hub import inventory
        app = inventory.SysApp("desktop", "HKLM64\\x", "AOMEI Partition Assistant", version="10.4.1", update=new,
                               winget_id="AOMEI.PartitionAssistant", winget_source="winget")
        return app, self.updates.Pending(app.name, "winget", app.version, new, app)

    def test_skip_version_and_app(self):
        u = self.updates
        app, p = self.pending("10.12.1")
        self.assertEqual(len(u.collect([], [app])), 1)
        u.ignore(p, always=False)
        self.assertEqual(u.collect([], [app]), [])
        self.assertTrue(u.app_ignored(app))
        app2, p2 = self.pending("10.13.0")                  # a newer version comes back
        self.assertEqual(len(u.collect([], [app2])), 1)
        u.ignore(p2, always=True)
        app3, _ = self.pending("11.0")
        self.assertEqual(u.collect([], [app3]), [])
        self.assertEqual(len(u.ignored([], [app3])), 1)
        u.allow(p2.key)
        self.assertEqual(len(u.collect([], [app3])), 1)


if __name__ == "__main__":
    unittest.main()
