"""Bounded immutable platform reads against a real isolated Git repository."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deps
import platform_customize as lane
from routers import platform_customize as routes

TENANT = "org_range_admin"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE).decode().strip()


@pytest.fixture
def source(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Range test")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "web").mkdir()
    (repo / "web/small.jsx").write_bytes(b"original\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")
    large = "".join(f"{i:04d}: " + "\u00e9\U0001f33f" * 17 + "\r\n" for i in range(3500))
    (repo / "web/big.jsx").write_bytes(large.encode())
    (repo / "web/empty.jsx").write_bytes(b"")
    (repo / "web/binary.dat").write_bytes(b"\x00" + b"x" * 300_000)
    (repo / "web/long.jsx").write_bytes(b"x" * 32_769 + b"\n")
    (repo / "web/huge.jsx").write_bytes(b"x" * (4_194_304 + 1))
    (repo / "web/replacement.jsx").write_bytes(b"\xff" * 11_000 + b"\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "authoring source")
    tip = git(repo, "rev-parse", "HEAD")
    ref = "refs/leaf/deployed/" + tip
    git(repo, "update-ref", ref, tip)
    git(repo, "update-ref", "refs/heads/main", baseline)
    monkeypatch.setenv("LEAF_PLATFORM_REPO_DIR", str(repo))
    monkeypatch.setenv("LEAF_PLATFORM_REPO_SOURCE_REF", ref)
    monkeypatch.setenv("LEAF_PLATFORM_REPO_BASE_REF", "refs/heads/main")
    return {"repo": repo, "tip": tip, "ref": ref, "large": large, "baseline": baseline}


def page(source, path="web/big.jsx", **overrides):
    args = {"path": path, "start_line": 1, "max_lines": 400,
            "expected_commit": source["tip"]}
    args.update(overrides)
    return lane.read_source(**args)


def test_large_source_reassembles_exactly_with_commit_and_byte_bounds(source):
    assert len(source["large"].encode()) > lane.MAX_SOURCE_BYTES
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        lane.read_source(path="web/big.jsx")
    assert (exc.value.code, exc.value.status_code) == ("source_too_large", 413)
    chunks, start = [], 1
    while start is not None:
        result = page(source, start_line=start)
        assert result["commit"] == source["tip"] and result["ref"] == source["ref"]
        assert result["path"] == "web/big.jsx" and result["binary"] is False
        assert len(result["content"].encode()) <= 32_768
        assert result["range"]["end_line"] - start + 1 <= 400
        assert result["content"].endswith("\r\n")
        chunks.append(result["content"])
        start = result["range"]["next_line"]
    assert result["range"]["eof"] is True
    assert result["range"]["total_lines"] == 3500
    assert "".join(chunks) == source["large"]
    assert git(source["repo"], "rev-parse", "refs/heads/main") == source["baseline"]


def test_line_limit_and_legacy_small_file_are_preserved(source):
    result = page(source, max_lines=2)
    assert result["range"] == {"start_line": 1, "end_line": 2, "next_line": 3,
                               "total_lines": 3500, "eof": False}
    small = lane.read_source(path="web/small.jsx")
    assert small["content"] == "original\n" and "range" not in small


@pytest.mark.parametrize("field", ["start_line", "max_lines", "expected_commit"])
def test_partial_ranges_refuse_before_git(source, monkeypatch, field):
    values = {"start_line": 1, "max_lines": 2, "expected_commit": source["tip"]}
    monkeypatch.setattr(lane, "repo_dir", lambda: pytest.fail("partial range reached Git"))
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        lane.read_source(path="web/big.jsx", **{field: values[field]})
    assert exc.value.code == "source_range_invalid"


@pytest.mark.parametrize("args", [
    {"start_line": 0}, {"start_line": True}, {"max_lines": 0}, {"max_lines": 401},
    {"expected_commit": "A" * 40}, {"expected_commit": "short"},
])
def test_invalid_complete_ranges_refuse_before_git(source, monkeypatch, args):
    complete = {"start_line": 1, "max_lines": 2, "expected_commit": source["tip"]}
    complete.update(args)
    monkeypatch.setattr(lane, "repo_dir", lambda: pytest.fail("invalid range reached Git"))
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        lane.read_source(path="web/big.jsx", **complete)
    assert exc.value.code == "source_range_invalid"


def test_source_change_refuses_before_blob_read(source, monkeypatch):
    monkeypatch.setattr(lane, "_git_out", lambda *args: pytest.fail("stale read loaded content"))
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        page(source, expected_commit=source["baseline"])
    assert (exc.value.code, exc.value.status_code) == ("source_changed", 409)


def test_oversize_range_input_refuses_before_loading_blob(source, monkeypatch):
    monkeypatch.setattr(lane, "_git_out", lambda *args: pytest.fail("oversize blob loaded"))
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        page(source, path="web/huge.jsx")
    assert exc.value.code == "source_too_large" and exc.value.status_code == 413


@pytest.mark.parametrize("path", ["web/long.jsx", "web/replacement.jsx"])
def test_overbudget_first_line_refuses_after_utf8_decoding(source, path):
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        page(source, path=path)
    assert (exc.value.code, exc.value.status_code) == ("source_line_too_large", 413)


def test_empty_binary_and_out_of_bounds_are_explicit(source):
    empty = page(source, path="web/empty.jsx")
    assert empty["content"] == "" and empty["range"] == {
        "start_line": 1, "end_line": 0, "next_line": None, "total_lines": 0, "eof": True}
    binary = page(source, path="web/binary.dat")
    assert binary["binary"] is True and "content" not in binary
    for path, start in [("web/big.jsx", 3501), ("web/empty.jsx", 2)]:
        with pytest.raises(lane.PlatformCustomizeError) as exc:
            page(source, path=path, start_line=start)
        assert exc.value.code == "source_range_out_of_bounds"


@pytest.fixture
def client(source, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    context = {"tenant": deps.TenantContext(TENANT, tier="admin", subject="auth0|test")}
    app.dependency_overrides[deps.require_tenant] = lambda: context["tenant"]
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R7_MODE", "internal")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", TENANT)
    with TestClient(app) as http:
        yield http, context


def test_actual_http_range_and_refusals(client, source):
    http, context = client
    params = {"path": "web/big.jsx", "start_line": 1, "max_lines": 2,
              "expected_commit": source["tip"]}
    result = http.get("/api/platform/source", params=params)
    assert result.status_code == 200 and result.json()["range"]["next_line"] == 3
    assert http.get("/api/platform/source", params={"path": "web/big.jsx"}).status_code == 413
    assert http.get("/api/platform/source", params={"path": "web/big.jsx", "start_line": 1}).status_code == 422
    stale = http.get("/api/platform/source", params=params | {"expected_commit": source["baseline"]})
    assert stale.status_code == 409 and stale.json()["reason_code"] == "source_changed"
    context["tenant"] = deps.TenantContext(TENANT, tier="hosted_pro", subject="auth0|test")
    assert http.get("/api/platform/source", params=params).status_code == 403
    context["tenant"] = deps.TenantContext("org_not_allowed", tier="admin", subject="auth0|test")
    assert http.get("/api/platform/source", params=params).status_code == 404


def test_range_keeps_path_and_live_auth_guards(client, source, monkeypatch):
    http, _ = client
    params = {"path": "../outside", "start_line": 1, "max_lines": 2,
              "expected_commit": source["tip"]}
    assert http.get("/api/platform/source", params=params).status_code == 422
    monkeypatch.setattr(deps, "auth_live", lambda: False)
    assert http.get("/api/platform/source", params=params).status_code == 503
