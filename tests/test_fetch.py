import http.server
import json
import os
import socketserver
import threading
import time

import pytest

from app_hub import core

ROUTES = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        code, body, length = ROUTES.get(self.path, (404, b"{}", None))
        self.send_response(code)
        self.send_header("Content-Length", str(length if length is not None else len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def hub(server, tmp_path, monkeypatch):
    ROUTES.clear()
    monkeypatch.setattr(core, "API", server)
    monkeypatch.setattr(core, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(core, "API_CACHE", str(tmp_path / "cache" / "api-cache.json"))
    monkeypatch.setattr(core, "_api_mem", None)
    monkeypatch.setattr(core, "_token", "")
    return server


def app(repo="o/r"):
    return core.App(id=repo.replace("/", "_"), name="X", summary="", repo=repo, package="x")


def route(repo, body, length=None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    ROUTES[f"/repos/{repo}/releases/latest"] = (200, raw, length)


GOOD = {"tag_name": "v1.0", "assets": [{"name": "x_1.0_all.deb", "browser_download_url": "http://x/a.deb"}]}


@pytest.mark.parametrize("raw,length", [
    (b'{"tag_name": "v1.0", "assets": [', None),     # truncated JSON
    (b"<html>captive portal login</html>", None),    # HTML with 200
    (b'{"tag_name":"v1"}', 500),                     # IncompleteRead
    (b"[1, 2]", None),                               # latest returns a list
])
def test_bad_answer_is_fetch_failure(hub, raw, length):
    route("o/r", raw, length)
    with pytest.raises(core.HubError):
        core.fetch_release(app())


@pytest.mark.parametrize("raw", [b"<html>portal</html>", b"[1, 2]", b'{"tag_name": "v'])
def test_bad_answer_uses_stale_cache(hub, raw):
    url = f"{hub}/repos/o/r/releases/latest"
    core._api_mem = {url: {"etag": "", "data": core._slim(GOOD), "time": time.time()}}
    route("o/r", raw)
    rel = core.fetch_release(app())
    assert rel.tag == "v1.0" and rel.stale and rel.asset_name == "x_1.0_all.deb"


def test_asset_without_name_is_skipped(hub):
    route("o/r", {"tag_name": "v1.0", "assets": [{"browser_download_url": "u"}, 5, None,
                                                 GOOD["assets"][0]]})
    assert core.fetch_release(app()).asset_name == "x_1.0_all.deb"


def test_assets_null(hub):
    route("o/r", {"tag_name": "v1.0", "assets": None})
    ROUTES["/repos/o/r/releases?per_page=30"] = (200, b'[5, {"tag_name": 3, "assets": "zz"}]', None)
    rel = core.fetch_release(app())
    assert rel.tag == "v1.0" and rel.asset_url == ""


@pytest.mark.parametrize("cache", [
    {"u": {"data": {}, "time": "x"}},
    {"u": {"data": {"tag_name": "v", "assets": [1]}, "time": 9e12}},
    {"u": {"data": [{"tag_name": "v", "assets": "zz"}], "time": 9e12}},
    {"u": {"data": "str", "time": 9e12}},
])
def test_wrong_shape_cache_loads(hub, cache):
    os.makedirs(core.CACHE_DIR)
    with open(core.API_CACHE, "w") as f:
        json.dump(cache, f)
    loaded = core._api_cache_load()
    for e in loaded.values():
        data = e["data"] if isinstance(e["data"], list) else [e["data"]]
        for r in data:
            assert all(isinstance(a, dict) and isinstance(a["name"], str) for a in r.get("assets", []))


def test_unwritable_cache_keeps_good_answer(hub, monkeypatch):
    route("o/r", GOOD)
    # cache dir path is a file, so makedirs/open fail with OSError
    blocker = os.path.dirname(core.CACHE_DIR) + "/blocker"
    open(blocker, "w").close()
    monkeypatch.setattr(core, "CACHE_DIR", blocker + "/cache")
    monkeypatch.setattr(core, "API_CACHE", blocker + "/cache/api-cache.json")
    assert core.fetch_release(app()).tag == "v1.0"


def test_check_all_never_leaves_app_unchecked(monkeypatch):
    def boom(a):
        raise KeyError("name")
    monkeypatch.setattr(core, "fetch_release", boom)
    a = app()
    seen = []
    core.check_all([a], seen.append)
    assert seen == [a] and a.release is None and "KeyError" in a.error
