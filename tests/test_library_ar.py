import io

import pytest

from app_hub import core, library


def ar_member(name, size_field, body=b""):
    return name.ljust(16).encode() + b"0".ljust(12) + b"0".ljust(6) + b"0".ljust(6) + \
        b"100644".ljust(8) + size_field.ljust(10).encode() + b"`\n" + body


@pytest.mark.parametrize("size", ["-60", "-1", "abc", ""])
def test_bad_ar_size_is_rejected(monkeypatch, size):
    head = b"!<arch>\n" + ar_member("debian-binary", size) + b"x" * 200
    monkeypatch.setattr(library.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(head))
    with pytest.raises(core.HubError):
        library.remote_deb_control("http://example.invalid/a.deb")
