"""Standardization slice 7b: the surface-config overlay fold.

  deps.effective_surface_config   no file, bad file, unknown slot, a valid
                                   overlay, tenant isolation, the cache bound.
  GET /api/surface-config          tenant-scoped through the same binding
                                   /api/capabilities uses; never 500.

Run:  cd server && python -m pytest tests/test_surface_config.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


def _client():
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def _seed(base: Path, tenant_id: str, surface_config: dict | None) -> Path:
    root = base / tenant_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "registry.json").write_text(json.dumps({"tools": []}), encoding="utf-8")
    if surface_config is not None:
        (root / "surface-config.json").write_text(
            json.dumps(surface_config), encoding="utf-8"
        )
    return root


def _reset_cache(monkeypatch):
    import deps
    monkeypatch.setattr(deps, "_surface_config_cache", {})
    monkeypatch.setattr(deps, "_surface_config_warned_tenants", set())


def _post_setup(monkeypatch, tmp_path, overlay=None, *, admit=True):
    from routers import platform_customize
    _reset_cache(monkeypatch)
    base = tmp_path / "tenants"
    root = _seed(base, "t1", overlay)
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)
    if admit:
        monkeypatch.setattr(platform_customize, "_gate", lambda tenant: (str(tenant), "admin"))
    return root / "surface-config.json"


def test_post_invalid_schema_preserves_bytes(monkeypatch, tmp_path):
    path = _post_setup(monkeypatch, tmp_path, {"cad": {"authoring": False}})
    before = path.read_bytes()
    response = _client().post("/api/surface-config", headers=_h("t1"),
                              json={"overlay": {"cad": {"unknown": True}}})
    assert response.status_code == 400
    assert "surface-config.json failed schema validation" in response.text
    assert path.read_bytes() == before


def test_post_commits_exact_bytes_fresh_receipt_and_evicts_cache(monkeypatch, tmp_path):
    import deps
    path = _post_setup(monkeypatch, tmp_path, {"cad": {"authoring": False}})
    assert deps.effective_surface_config("t1") == {"cad": {"authoring": False}}
    overlay = {"cad": {"authoring": True}, "sheets": {"chrome": {"tab": "My sheets"}}}
    response = _client().post("/api/surface-config", headers=_h("t1"), json={"overlay": overlay})
    assert response.status_code == 200, response.text
    assert path.read_bytes() == (json.dumps(overlay, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    assert response.json() == deps.surface_config_source("t1")
    assert deps.effective_surface_config("t1") == overlay
    assert _client().get("/api/surface-config", headers=_h("t1")).json()["surfaces"] == overlay


def test_post_oversize_is_refused_before_json_parse(monkeypatch, tmp_path):
    path = _post_setup(monkeypatch, tmp_path)
    response = _client().post("/api/surface-config", headers=_h("t1"), content=b"!" * (256 * 1024 + 1))
    assert response.status_code == 413
    assert not path.exists()


def test_post_respects_vendored_file_size_limit(monkeypatch, tmp_path):
    path = _post_setup(monkeypatch, tmp_path)
    response = _client().post("/api/surface-config", headers=_h("t1"),
                              json={"overlay": {"cad": {"chrome": {"tab": "x" * (64 * 1024)}}}})
    assert response.status_code == 413
    assert not path.exists()


def test_post_symlink_target_refused(monkeypatch, tmp_path):
    path = _post_setup(monkeypatch, tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}\n")
    _symlink_or_skip(outside, path)
    response = _client().post("/api/surface-config", headers=_h("t1"), json={"overlay": {}})
    assert response.status_code == 400
    assert path.is_symlink()
    assert outside.read_bytes() == b"{}\n"


def test_post_entitlement_gate_refuses_without_write(monkeypatch, tmp_path):
    import deps
    from app import app
    from routers import platform_customize
    path = _post_setup(monkeypatch, tmp_path, {}, admit=False)
    before = path.read_bytes()
    # Run the neighbour's real gate, with only its identity/policy inputs stubbed.
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(platform_customize.entitlements, "resolve_tier", lambda tenant: "free")
    monkeypatch.setattr(platform_customize.entitlements, "resolve_roles", lambda tenant: ((), False))
    monkeypatch.setattr(platform_customize.entitlements, "entitlements_for", lambda *args: {"platform_customize": False})
    monkeypatch.setitem(app.dependency_overrides, deps.require_tenant, lambda: "t1")
    response = _client().post("/api/surface-config", headers=_h("t1"), json={"overlay": {}})
    assert response.status_code == 403, response.text
    assert response.json()["required"] == "platform_customize"
    assert path.read_bytes() == before


def test_submit_refuses_invalid_tenant_identity(monkeypatch, tmp_path):
    import deps
    import pytest
    from fastapi import HTTPException
    path = _post_setup(monkeypatch, tmp_path)
    for tenant_id in ("../t1", "t1\n"):
        with pytest.raises(HTTPException) as error:
            deps.submit_surface_config(tenant_id, {})
        assert error.value.status_code == 400
        assert not path.exists()


def test_post_waits_for_old_reader_before_replace_and_evict(monkeypatch, tmp_path):
    import deps
    import threading
    from concurrent.futures import ThreadPoolExecutor
    old = {"cad": {"authoring": False}}
    new = {"cad": {"authoring": True}}
    path = _post_setup(monkeypatch, tmp_path, old)
    loaded = threading.Event()
    release = threading.Event()
    writer_waiting = threading.Event()
    lock = threading.Lock()
    loader = deps.load_repo_surface_config

    class ObservedLock:
        def __enter__(self):
            if loaded.is_set():
                writer_waiting.set()
            lock.acquire()

        def __exit__(self, *args):
            lock.release()

    def paused_loader(root, *, on_error):
        overlay = loader(root, on_error=on_error)
        if root == path.parent and not loaded.is_set():
            loaded.set()
            assert release.wait(5), "reader was never released"
        return overlay

    def release_reader():
        try:
            assert writer_waiting.wait(5), "POST never reached the held lock"
            assert lock.locked()
            assert json.loads(path.read_bytes()) == old
        finally:
            release.set()

    monkeypatch.setattr(deps, "_surface_config_lock", ObservedLock())
    monkeypatch.setattr(deps, "load_repo_surface_config", paused_loader)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(lambda: _client().get("/api/surface-config", headers=_h("t1")))
        assert loaded.wait(5), "GET never entered the loader"
        releaser = pool.submit(release_reader)
        response = _client().post("/api/surface-config", headers=_h("t1"), json={"overlay": new})
        releaser.result(timeout=5)
        assert reader.result(timeout=5).json()["surfaces"] == old
    assert response.status_code == 200, response.text
    assert response.json() == deps.surface_config_source("t1")
    assert deps.effective_surface_config("t1") == new


def test_submit_preserves_target_mode_and_defaults_new_file(monkeypatch, tmp_path):
    import deps
    import os
    import stat
    path = _post_setup(monkeypatch, tmp_path)
    chmod = os.chmod
    modes = []

    def record_chmod(target, mode):
        modes.append(mode)
        chmod(target, mode)

    monkeypatch.setattr(os, "chmod", record_chmod)
    deps.submit_surface_config("t1", {})
    assert modes[-1] == 0o644
    chmod(path, 0o640)
    existing_mode = path.stat().st_mode
    deps.submit_surface_config("t1", {"cad": {"authoring": True}})
    assert modes[-1] == existing_mode
    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(existing_mode)


# =========================================================================== #
# deps.effective_surface_config — the fold itself
# =========================================================================== #
def test_no_file_returns_empty_dict_and_no_warning(monkeypatch, tmp_path, capsys):
    import deps
    _reset_cache(monkeypatch)
    base = tmp_path / "tenants"
    _seed(base, "t1", surface_config=None)
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)

    assert deps.effective_surface_config("t1") == {}
    assert "bad tenant surface-config.json" not in capsys.readouterr().err


def test_bad_json_fails_closed_to_empty_with_one_warning(monkeypatch, tmp_path, capsys):
    import deps
    _reset_cache(monkeypatch)
    base = tmp_path / "tenants"
    root = _seed(base, "t1", surface_config=None)
    (root / "surface-config.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)

    assert deps.effective_surface_config("t1") == {}
    assert "bad tenant surface-config.json" in capsys.readouterr().err

    # cache TTL still applies (bounded, see below), so bypass it here to prove
    # the warning fires only ONCE per tenant per process, not once per read.
    monkeypatch.setattr(deps, "_surface_config_cache", {})
    deps.effective_surface_config("t1")
    assert capsys.readouterr().err == ""


def test_unknown_slot_fails_closed_to_empty(monkeypatch, tmp_path):
    import deps
    _reset_cache(monkeypatch)
    base = tmp_path / "tenants"
    _seed(base, "t1", {"cad": {"not-a-real-slot": True}})
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)

    assert deps.effective_surface_config("t1") == {}


def test_valid_overlay_is_returned_verbatim(monkeypatch, tmp_path):
    import deps
    _reset_cache(monkeypatch)
    base = tmp_path / "tenants"
    overlay = {"sheets": {"chrome": {"tab": "sheets"}}}
    _seed(base, "t1", overlay)
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)

    assert deps.effective_surface_config("t1") == overlay


def test_tenant_isolation_two_tenants_two_files(monkeypatch, tmp_path):
    import deps
    _reset_cache(monkeypatch)
    base = tmp_path / "tenants"
    o1 = {"cad": {"authoring": False}}
    o2 = {"solar": {"chrome": {"tab": "solar-alt"}}}
    _seed(base, "t1", o1)
    _seed(base, "t2", o2)
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)

    assert deps.effective_surface_config("t1") == o1
    assert deps.effective_surface_config("t2") == o2
    # neither tenant's overlay leaks into the other's.
    assert deps.effective_surface_config("t1") != o2
    assert deps.effective_surface_config("t2") != o1


def test_cache_is_bounded_by_ttl(monkeypatch, tmp_path):
    import deps
    _reset_cache(monkeypatch)
    base = tmp_path / "tenants"
    root = _seed(base, "t1", {"cad": {"authoring": False}})
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)

    first = deps.effective_surface_config("t1")
    assert first == {"cad": {"authoring": False}}

    # rewrite the file with a DIFFERENT overlay; within the TTL window the
    # cached read must still win (proves the cache is consulted, not bypassed).
    (root / "surface-config.json").write_text(
        json.dumps({"cad": {"authoring": True}}), encoding="utf-8"
    )
    assert deps.effective_surface_config("t1") == first

    # simulate TTL expiry by back-dating the cache entry past the bound.
    cache = deps._surface_config_cache
    stamp, cached_overlay = cache["t1"]
    cache["t1"] = (stamp - deps.SURFACE_CONFIG_CACHE_TTL_SECONDS - 1.0, cached_overlay)
    assert deps.effective_surface_config("t1") == {"cad": {"authoring": True}}


# =========================================================================== #
# GET /api/surface-config
# =========================================================================== #
def test_route_no_file_returns_empty_surfaces_never_500(monkeypatch, tmp_path):
    import deps
    _reset_cache(monkeypatch)
    base = tmp_path / "tenants"
    _seed(base, "t1", surface_config=None)
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)

    r = _client().get("/api/surface-config", headers=_h("t1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["surfaces"] == {}
    assert "source" not in body


def test_route_bad_file_returns_empty_surfaces_never_500(monkeypatch, tmp_path):
    import deps
    _reset_cache(monkeypatch)
    base = tmp_path / "tenants"
    root = _seed(base, "t1", surface_config=None)
    (root / "surface-config.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)

    r = _client().get("/api/surface-config", headers=_h("t1"))
    assert r.status_code == 200, r.text
    assert r.json()["surfaces"] == {}


def test_route_valid_overlay_returns_overlay_and_source(monkeypatch, tmp_path):
    import deps
    _reset_cache(monkeypatch)
    base = tmp_path / "tenants"
    overlay = {"sheets": {"chrome": {"tab": "sheets"}}}
    _seed(base, "t1", overlay)
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)

    r = _client().get("/api/surface-config", headers=_h("t1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["surfaces"] == overlay
    assert set(body["source"].keys()) == {"sha256", "authored_at"}
    assert len(body["source"]["sha256"]) == 64


def test_route_tenant_isolation(monkeypatch, tmp_path):
    import deps
    _reset_cache(monkeypatch)
    base = tmp_path / "tenants"
    o1 = {"cad": {"authoring": False}}
    o2 = {"solar": {"chrome": {"tab": "solar-alt"}}}
    _seed(base, "t1", o1)
    _seed(base, "t2", o2)
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)

    c = _client()
    r1 = c.get("/api/surface-config", headers=_h("t1")).json()
    r2 = c.get("/api/surface-config", headers=_h("t2")).json()
    assert r1["surfaces"] == o1
    assert r2["surfaces"] == o2


# =========================================================================== #
# containment of the FILE (round 4): surface-config.json must be a real file at
# the tenant root; a symlink under that name is refused on both read sites
# =========================================================================== #
def _symlink_or_skip(target: Path, link: Path) -> None:
    import os
    import pytest
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError) as exc:  # no symlink privilege (Windows)
        pytest.skip(f"symlink unavailable on this host: {exc}")


def test_symlinked_surface_config_is_refused_by_the_fold(monkeypatch, tmp_path, capsys):
    import deps
    _reset_cache(monkeypatch)
    base = tmp_path / "tenants"
    root = _seed(base, "t1", None)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"sheets": {"chrome": {"tab": "sheets"}}}), encoding="utf-8")
    _symlink_or_skip(outside, root / "surface-config.json")
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)

    assert deps.effective_surface_config("t1") == {}
    assert "resolves outside the tenant repo" in capsys.readouterr().err


def test_symlinked_surface_config_has_no_source(monkeypatch, tmp_path):
    import deps
    _reset_cache(monkeypatch)
    base = tmp_path / "tenants"
    root = _seed(base, "t1", None)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    _symlink_or_skip(outside, root / "surface-config.json")
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)

    assert deps.surface_config_source("t1") is None


def test_contained_surface_config_path_is_the_roots_own_file_or_none(tmp_path):
    import os
    import deps
    root = tmp_path / "repo"
    root.mkdir()
    own = os.path.normpath(os.path.realpath(str(root / "surface-config.json")))
    # absent: the root's own path (the callers fold "absent" themselves)
    assert deps._contained_surface_config_path(str(root)) == own
    (root / "surface-config.json").write_text("{}", encoding="utf-8")
    assert deps._contained_surface_config_path(str(root)) == own
    # the reader is handed the directory the checked file lives in, never the raw root
    assert os.path.dirname(own) == os.path.normpath(os.path.realpath(str(root)))
