"""Offline canaries for tenant-scoped drawing storage."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402


def _backends(tmp_path):
    return (store.InMemoryBackend(), store.FilesystemBackend(str(tmp_path / "store")))


def _seed(backend):
    version_key = store.drawing_version_key("tenant-a", "d1", 1)
    backend.put(version_key, b"CANARY-A")
    store.save_manifest(backend, "tenant-a", "d1", {
        "head": 1, "latest": 1, "versions": [{"v": 1}],
    })
    return {version_key, store.manifest_key("tenant-a", "d1")}


def test_prefix_listing_is_disjoint_per_tenant(tmp_path):
    for backend in _backends(tmp_path):
        written = _seed(backend)
        owner_prefix = store.drawing_prefix("tenant-a", "d1")
        other_prefix = store.drawing_prefix("tenant-b", "d1")
        if isinstance(backend, store.InMemoryBackend):
            owner_keys = {key for key in backend.keys() if key.startswith(owner_prefix)}
            other_keys = {key for key in backend.keys() if key.startswith(other_prefix)}
        else:
            owner_keys = backend.drawing_object_keys("tenant-a", "d1")
            other_keys = backend.drawing_object_keys("tenant-b", "d1")
        assert owner_keys == written
        assert other_keys == set()
        assert all(not key.startswith(owner_prefix) for key in other_keys)


def test_same_drawing_id_two_tenants_disjoint_keys():
    a = (store.drawing_version_key("tenant-a", "d1", 1),
         store.manifest_key("tenant-a", "d1"))
    b = (store.drawing_version_key("tenant-b", "d1", 1),
         store.manifest_key("tenant-b", "d1"))
    assert a[0] != b[0]
    assert a[1] != b[1]
    assert all(not key.startswith(store.drawing_prefix("tenant-b", "d1")) for key in a)
    assert all(not key.startswith(store.drawing_prefix("tenant-a", "d1")) for key in b)


def test_tenant_id_equal_to_other_drawing_id_is_disjoint():
    pairs = (("d1", "tenant-a"), ("tenant-a", "d1"))
    prefixes = [store.drawing_prefix(*pair) for pair in pairs]
    keys = [key for pair in pairs for key in (
        store.drawing_version_key(*pair, 1), store.manifest_key(*pair))]
    assert len(set(keys)) == 4
    assert not prefixes[0].startswith(prefixes[1])
    assert not prefixes[1].startswith(prefixes[0])
    assert all(not key.startswith(prefixes[1]) for key in keys[:2])
    assert all(not key.startswith(prefixes[0]) for key in keys[2:])


def test_sanitize_rejects_traversal_and_separators():
    for raw in ("../tenant-a", "tenant-a/..", "tenant-a/d1", "tenant-a\\d1",
                "tenant-a%2fd1", "tenant-a\x00", ".", ".."):
        with pytest.raises(ValueError):
            store.sanitize_id(raw)


def test_sanitize_rejects_case_and_length():
    for raw in ("TENANT-A", "Tenant-a", "-tenant", "_tenant", "", "a" * 64):
        with pytest.raises(ValueError):
            store.sanitize_id(raw)
    for raw in ("a" * 63, "a0_-"):
        assert store.sanitize_id(raw) == raw


def test_filesystem_backend_keeps_raw_dotdot_key_inside_root(tmp_path):
    """sanitize_id is the control; _path is the containment backstop."""
    root = tmp_path / "store"
    backend = store.FilesystemBackend(str(root))
    key = "tenants/../escape.dwg"
    assert Path(backend._path(key)).resolve().is_relative_to(root.resolve())
    backend.put(key, b"CANARY-A")
    assert not [path for path in tmp_path.rglob("*")
                if path.is_file() and not path.resolve().is_relative_to(root.resolve())]
    with pytest.raises(ValueError):
        store.sanitize_id("..")


def test_checkout_lock_keys_are_per_tenant_and_outside_drawing_prefix():
    locks = [store.checkout_lock_key(tenant, "d1") for tenant in ("tenant-a", "tenant-b")]
    prefixes = [store.drawing_prefix(tenant, "d1") for tenant in ("tenant-a", "tenant-b")]
    assert locks[0] != locks[1]
    assert all(not key.startswith(prefix) for key in locks for prefix in prefixes)


def test_resolve_version_for_other_tenant_never_returns_the_bytes(tmp_path):
    for backend in _backends(tmp_path):
        _seed(backend)
        _, key = store.resolve_version(backend, "tenant-a", "d1", "head")
        assert backend.get(key) == b"CANARY-A"
        with pytest.raises(KeyError) as exc:
            store.resolve_version(backend, "tenant-b", "d1", "head")
        assert exc.value.args == (store.manifest_key("tenant-b", "d1"),)
