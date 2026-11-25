"""Internal-only dev transport, bounded reads, and checker-gated staging."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID
import zipfile

from fastapi.testclient import TestClient
import pytest

from branch_tools import dev_catalog
import deps
import tool_envelopes

BASE = "/api/tool-envelopes/dev"
TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
REPO = Path(__file__).resolve().parents[2]
REFERENCE = REPO / "server/tests/fixtures/branch_tools/reference-package/panel-gap-check"
DIGEST = "079bc8c277c9cba64b38d881a58fdaede524d19636d7400887fc99273bcd1f72"
ZIP = b"PK\x05\x06" + b"\x00" * 18


def stage(root, name="panel-gap-check", version="1.2.0"):
    directory = root / name / version
    directory.mkdir(parents=True)
    (directory / "package.zip").write_bytes(ZIP)
    (directory / "digest.txt").write_text(DIGEST + "\n")
    return directory


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import app

    monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS", "1")
    monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS_DIR", str(tmp_path))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", TENANT)
    monkeypatch.setattr(app, "dependency_overrides", {
        deps.require_active_tenant: lambda: UUID(TENANT),
    })
    return TestClient(app)


def assert_hidden(client):
    for suffix in ("", "/panel-gap-check/1.2.0/package"):
        response = client.get(BASE + suffix)
        assert response.status_code == 404
        assert response.json() == {"detail": "tool_not_published"}
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("flag", [None, "0", "true"])
def test_flag_off(client, tmp_path, monkeypatch, flag):
    stage(tmp_path)
    if flag is None:
        monkeypatch.delenv("LEAF_BRANCH_DEV_TOOLS")
    else:
        monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS", flag)
    assert_hidden(client)


def test_external_tenant(client, tmp_path, monkeypatch):
    stage(tmp_path)
    monkeypatch.setitem(client.app.dependency_overrides, deps.require_active_tenant, lambda: UUID(OTHER))
    assert_hidden(client)


def test_internal_lists_and_downloads_without_cache(client, tmp_path, monkeypatch):
    directory = stage(tmp_path)
    timestamp = 1795000000
    os.utime(directory / "digest.txt", (timestamp, timestamp))
    response = client.get(BASE)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    expected = {"name": "panel-gap-check", "version": "1.2.0", "package_digest": DIGEST,
                "review": "unreviewed",
                "staged_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")}
    assert response.json() == {"tools": [expected]}
    response = client.get(BASE + "/panel-gap-check/1.2.0/package")
    assert response.status_code == 200
    assert response.content == ZIP
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    (directory / "review.json").write_text(json.dumps({"status": "approved"}))
    assert client.get(BASE).json()["tools"][0]["review"] == "approved"
    other_root = tmp_path / "other"
    other_root.mkdir()
    monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS_DIR", str(other_root))
    assert client.get(BASE).json() == {"tools": []}


def test_dev_route_never_reaches_signed_catalog(client, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("dev route reached signed catalog")

    monkeypatch.setattr(tool_envelopes, "published_catalog", forbidden)
    for identity in ("x/1.0.0", "unknown-tool/1.0.0", "Bad_Name/1.0.0"):
        response = client.get(BASE + "/" + identity + "/package")
        assert response.status_code == 404
        assert response.json() == {"detail": "tool_not_published"}
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("root", [None, "missing"])
def test_missing_root(client, tmp_path, monkeypatch, root):
    if root is None:
        monkeypatch.delenv("LEAF_BRANCH_DEV_TOOLS_DIR")
    else:
        monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS_DIR", str(tmp_path / root))
    assert_hidden(client)


def test_invalid_entries_are_skipped(client, tmp_path):
    stage(tmp_path)
    stage(tmp_path, "Bad_Name")
    stage(tmp_path, "bad-version", "1.0")
    for name, filename, raw in [
        ("bad-digest", "digest.txt", b"A" * 64),
        ("bad-review", "review.json", b"{"),
        ("big-package", "package.zip", b" " * (8 * 1024 * 1024 + 1)),
    ]:
        (stage(tmp_path, name) / filename).write_bytes(raw)
    missing = stage(tmp_path, "missing-package")
    (missing / "package.zip").unlink()
    assert [item["name"] for item in client.get(BASE).json()["tools"]] == ["panel-gap-check"]


def test_symlinks_are_skipped(client, tmp_path, monkeypatch):
    directory = stage(tmp_path)
    (tmp_path / "linked-name").symlink_to(directory.parent, target_is_directory=True)
    (directory.parent / "2.0.0").symlink_to(directory, target_is_directory=True)
    linked_file = stage(tmp_path, "linked-file") / "digest.txt"
    linked_file.unlink()
    linked_file.symlink_to(directory / "digest.txt")
    assert len(client.get(BASE).json()["tools"]) == 1
    link = tmp_path / "root-link"
    link.symlink_to(directory.parent, target_is_directory=True)
    monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS_DIR", str(link))
    assert_hidden(client)


def test_walk_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS_DIR", str(tmp_path))
    for index in range(201):
        stage(tmp_path, f"tool-{index}")
    assert len(dev_catalog.read_catalog()) == 200
    root = tmp_path / "versions"
    for index in range(51):
        stage(root, version=f"1.0.{index}")
    monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS_DIR", str(root))
    assert len(dev_catalog.read_catalog()) == 50


def run_stage(package, out, cwd):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, "-B", "-P",
                           str(REPO / "scripts/branch_tools/stage_dev_package.py"),
                           "--package", str(package), "--out", str(out)],
                          cwd=cwd, env=env, capture_output=True, text=True)


def test_stage_deterministic_and_refuses_duplicate(tmp_path):
    out = tmp_path / "dev"
    result = run_stage(REFERENCE, out, tmp_path)
    assert result.returncode == 0, result.stderr
    directory = out / "panel-gap-check/1.2.0"
    assert (directory / "digest.txt").read_text().strip() == DIGEST
    original = (directory / "package.zip").read_bytes()
    with zipfile.ZipFile(directory / "package.zip") as archive:
        assert archive.namelist() == sorted(archive.namelist())
        for entry in archive.infolist():
            assert entry.filename.startswith("panel-gap-check/")
            assert entry.date_time == (1980, 1, 1, 0, 0, 0)
            assert entry.compress_type == zipfile.ZIP_DEFLATED
            assert archive.read(entry) == (REFERENCE / entry.filename.split("/", 1)[1]).read_bytes()
    assert run_stage(REFERENCE, out, tmp_path).returncode == 1
    assert (directory / "package.zip").read_bytes() == original
    second = tmp_path / "second"
    assert run_stage(REFERENCE, second, tmp_path).returncode == 0
    assert (second / "panel-gap-check/1.2.0/package.zip").read_bytes() == original


def test_stage_refuses_invalid_package(tmp_path):
    package = tmp_path / "invalid-tool"
    package.mkdir()
    (package / "main.csx").write_text("throw new Exception();")
    out = tmp_path / "dev"
    result = run_stage(package, out, tmp_path)
    assert result.returncode == 1
    assert "V1" in result.stderr
    assert not out.exists()
