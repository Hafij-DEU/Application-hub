import pytest

from app_hub import core


@pytest.mark.parametrize("latest,installed,newer", [
    ("v1.10", "1.9", True),
    ("1.0.0", "1.0.0-rc1", True),
    ("1.0.0-rc1", "1.0.0", False),
    ("v1.0.0", "1.0.0~rc1", True),            # installed pre-release gets the final
    ("1.0.0-rc2", "1.0.0~rc1", True),
    ("v2.0.0-beta.1", "1.9", True),
    ("v2.0.0-beta.1", "2.0.0", False),
    ("1.0.0-alpha", "1.0.0-beta", False),
    ("1.0.0-pre", "1.0.0", False),
    ("nightly", "1.0", False),
    ("", "1.0", False),
    ("latest", "", False),
    ("files-v0.5.10", "0.5.9", True),
    ("settings-v0.5.32", "0.5.31", True),
    ("taskmgr-v0.3.4", "0.3.4", False),
    ("v1.0.0", "1.0.0~ubuntu24.04", False),
    ("v1.0.1", "1.0.0~ubuntu24.04", True),
    ("1.0.0-10", "1.0.0-9", True),
    ("1.0.0-9", "1.0.0-10", False),
    ("v1.0.0", "1.0.0-1", False),
    ("v1.0.0-2", "1.0.0", False),
    ("release-2024.05", "2024.04", True),
    ("V3", "2", True),
])
def test_is_newer(latest, installed, newer):
    assert core.is_newer(latest, installed) is newer


@pytest.mark.parametrize("tag,clean", [
    ("files-v0.5.1", "0.5.1"), ("v1.2.3", "1.2.3"), ("1.0.0-1", "1.0.0"),
    ("1.0~ubuntu24.04", "1.0"), ("1.0.0-rc1", "1.0.0~rc1"), ("1.0.0~RC1", "1.0.0~rc1"),
    ("2.0.0-beta.2", "2.0.0~beta.2"),
])
def test_clean_version(tag, clean):
    assert core.clean_version(tag) == clean


def test_update_available_uses_tag_revision():
    app = core.App(id="a", name="A", summary="", repo="o/r", package="a")
    app.installed = "1.0.0-9"
    app.release = core.Release(tag="v1.0.0-10", version=core.clean_version("v1.0.0-10"), url="u",
                               asset_url="http://x/a.deb")
    assert app.update_available
