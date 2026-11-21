import hashlib
import shutil
import subprocess

import pytest

from conftest import load_helper
from app_hub import core

helper = load_helper()
needs_dpkg = pytest.mark.skipif(not shutil.which("dpkg-deb"), reason="dpkg-deb missing")


def make_deb(tmp_path, name="demo"):
    root = tmp_path / "root"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "DEBIAN" / "control").write_text(
        f"Package: {name}\nVersion: 1.0\nArchitecture: all\nMaintainer: t <t@t>\nDescription: t\n")
    deb = tmp_path / f"{name}.deb"
    subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(root), str(deb)],
                   check=True, capture_output=True)
    return deb


@needs_dpkg
def test_stage_accepts_matching_sha(tmp_path):
    deb = make_deb(tmp_path)
    r = helper.Runner(simulate=True)
    try:
        dest = r.stage("demo", str(deb), hashlib.sha256(deb.read_bytes()).hexdigest())
        assert open(dest, "rb").read() == deb.read_bytes()
    finally:
        r.cleanup()


@needs_dpkg
def test_stage_refuses_swapped_file(tmp_path):
    deb = make_deb(tmp_path)
    good = hashlib.sha256(deb.read_bytes()).hexdigest()
    other = make_deb(tmp_path / "x", "demo")  # same package name, different bytes
    shutil.copy(other, deb)
    deb.write_bytes(deb.read_bytes() + b"\0")
    r = helper.Runner(simulate=True)
    try:
        with pytest.raises(SystemExit):
            r.stage("demo", str(deb), good)
    finally:
        r.cleanup()


@pytest.mark.parametrize("sha", ["", "ABC", "g" * 64, "A" * 64, "0" * 63, 5])
def test_verified_step_rejects_bad_sha(tmp_path, sha):
    r = helper.Runner(simulate=True)
    with pytest.raises(SystemExit):
        r.step("install-deb-verified", ["demo", str(tmp_path / "a.deb"), sha])


def test_verified_step_needs_triples():
    r = helper.Runner(simulate=True)
    with pytest.raises(SystemExit):
        r.step("install-deb-verified", ["demo", "/tmp/a.deb"])


def test_install_plan_passes_sha(tmp_path, monkeypatch):
    f = tmp_path / "a.deb"
    f.write_bytes(b"hello")
    plan = core.install_plan([("demo", str(f))])
    assert plan == [["install-deb-verified", "demo", str(f), hashlib.sha256(b"hello").hexdigest()]]
    monkeypatch.setitem(core._verified, str(f), "ab" * 32)
    assert core.install_plan([("demo", str(f))])[0][3] == "ab" * 32
