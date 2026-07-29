"""Contract tests for the disk-backed chat skill catalog."""
from __future__ import annotations

import os
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import skills
import skills_catalog


@pytest.fixture(autouse=True)
def _auth_off(monkeypatch):
    """Keep route tests on the existing local tenant-principal path."""
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)


def _bundle(root: Path, tier: str, skills: dict[str, str]) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        '{"leafTier": ' + repr(tier).replace("'", '"') + "}", encoding="utf-8")
    for name, description in skills.items():
        skill = root / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\nbody\n",
            encoding="utf-8")
    return root


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(skills.router)
    return TestClient(app, raise_server_exceptions=True)


def test_tenant_sees_only_tenant_safe_and_operator_sees_both(monkeypatch, tmp_path):
    tenant = _bundle(tmp_path / "tenant", "tenant-safe", {"tenant-card": "Tenant card"})
    operator = _bundle(tmp_path / "operator", "operator", {"ops-card": "Ops card"})
    monkeypatch.setenv("LEAF_SKILLS_BUNDLE_PATH", str(tenant))
    monkeypatch.setenv("LEAF_SKILLS_OPERATOR_BUNDLE_PATH", str(operator))
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")

    tenant_response = _client().get("/api/skills")
    operator_response = _client().get("/api/skills", headers={"X-Ops-Secret": "ops-secret"})

    assert tenant_response.status_code == 200
    assert tenant_response.json() == {"skills": [{
        "id": "tenant-card", "name": "tenant-card", "description": "Tenant card",
        "tier": "tenant-safe",
    }]}
    assert operator_response.status_code == 200
    assert {skill["id"] for skill in operator_response.json()["skills"]} == {
        "tenant-card", "ops-card"}


def test_tenant_bundle_with_wrong_manifest_tier_is_empty(monkeypatch, tmp_path):
    wrong = _bundle(tmp_path / "wrong", "operator", {"ops-card": "Ops card"})
    monkeypatch.setenv("LEAF_SKILLS_BUNDLE_PATH", str(wrong))

    assert _client().get("/api/skills").json() == {"skills": []}


def test_hostile_names_and_case_duplicates_are_not_cataloged(tmp_path):
    bundle = _bundle(tmp_path / "bundle", "tenant-safe", {"safe": "Safe"})
    for name in ("CON", "trailing."):
        skill = bundle / "skills" / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Hostile\n---\n", encoding="utf-8")
    duplicate = bundle / "skills" / "SAFE"
    try:
        duplicate.mkdir()
        (duplicate / "SKILL.md").write_text("---\nname: SAFE\n---\n", encoding="utf-8")
    except FileExistsError:
        # Case-insensitive filesystem (Windows/macOS): `safe` and `SAFE` are ONE
        # directory, so the on-disk collision cannot even be constructed here —
        # which is itself why the case-fold dedupe exists. The dedupe rule is
        # asserted portably by test_duplicate_listing_entries_are_deduped.
        pass

    output = skills_catalog.discover_bundle(str(bundle), "tenant-safe")

    assert output == [{"id": "safe", "name": "safe", "description": "Safe", "tier": "tenant-safe"}]
    assert skills_catalog.is_valid_skill_name("../../x") is False


def test_symlinked_skill_directory_is_skipped(tmp_path):
    bundle = _bundle(tmp_path / "bundle", "tenant-safe", {})
    target = bundle / "skills" / "target"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "---\nname: linked\ndescription: Linked\n---\n", encoding="utf-8")
    link = bundle / "skills" / "linked"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is not permitted in this sandbox: {exc}")

    assert skills_catalog.discover_bundle(str(bundle), "tenant-safe") == []


def test_duplicate_listing_entries_are_deduped(monkeypatch, tmp_path):
    """First-wins case-fold dedupe, portable across filesystems.

    A case-insensitive FS cannot HOLD `Probe` and `probe` at once, so the
    collision is reproduced at the catalog's actual seam: the scandir listing.
    Yielding every entry twice is exactly what a case-folded collision looks
    like to the `seen` set, and the catalog must emit each skill ONCE."""
    bundle = _bundle(tmp_path / "bundle", "tenant-safe", {"safe": "Safe"})
    real_scandir = os.scandir

    class _TwiceListing:
        def __init__(self, path):
            self._path = path

        def __enter__(self):
            with real_scandir(self._path) as it:
                entries = list(it)
            return iter(entries + entries)

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(skills_catalog.os, "scandir", _TwiceListing)
    output = skills_catalog.discover_bundle(str(bundle), "tenant-safe")
    assert output == [
        {"id": "safe", "name": "safe", "description": "Safe", "tier": "tenant-safe"}
    ], "a duplicated listing entry produced a duplicated catalog entry"


def test_dedupe_key_folds_case(monkeypatch):
    """The fold itself, unit-level and platform-independent: `Probe` and
    `probe` MUST collide. Falsifiable — an exact-case key makes this fail."""
    assert skills_catalog._dedupe_key("Probe") == skills_catalog._dedupe_key("probe")
    assert skills_catalog._dedupe_key("safe") == skills_catalog._dedupe_key("SAFE")


def test_hostile_manifest_never_500s(tmp_path):
    """Review round 1 MAJOR: the never-500 contract must survive a manifest
    that parses into a RecursionError (deeply nested but under the byte cap)
    and a filesystem that raises on the symlink probe."""
    bundle = _bundle(tmp_path / "bundle", "tenant-safe", {"safe": "Safe"})
    manifest = bundle / ".claude-plugin" / "plugin.json"
    depth = 30000
    manifest.write_text("[" * depth + "]" * depth, encoding="utf-8")
    assert skills_catalog.discover_bundle(str(bundle), "tenant-safe") == []

    # a route-level probe too: hostile manifest -> 200 []
    os.environ["LEAF_SKILLS_BUNDLE_PATH"] = str(bundle)
    try:
        response = _client().get("/api/skills")
        assert response.status_code == 200
        assert response.json() == {"skills": []}
    finally:
        os.environ.pop("LEAF_SKILLS_BUNDLE_PATH", None)


def test_hardlinked_skill_file_is_skipped(tmp_path):
    """Review round 1 MEDIUM: mirror the harness's nlink refusal — a
    hardlinked SKILL.md resolves INSIDE the bundle, so only the link count
    can see it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "SKILL.md"
    secret.write_text(
        "---\nname: linked\ndescription: Op\n---\n", encoding="utf-8")
    bundle = _bundle(tmp_path / "bundle", "tenant-safe", {"safe": "Safe"})
    linked_dir = bundle / "skills" / "linked"
    linked_dir.mkdir()
    try:
        os.link(secret, linked_dir / "SKILL.md")
    except OSError as exc:
        pytest.skip(f"hardlink creation not permitted here: {exc}")
    names = [s_["name"] for s_ in skills_catalog.discover_bundle(str(bundle), "tenant-safe")]
    assert names == ["safe"], f"hardlinked skill leaked into the catalog: {names}"


def test_missing_env_returns_200_empty(monkeypatch):
    monkeypatch.delenv("LEAF_SKILLS_BUNDLE_PATH", raising=False)
    monkeypatch.delenv("LEAF_SKILLS_OPERATOR_BUNDLE_PATH", raising=False)

    response = _client().get("/api/skills")

    assert response.status_code == 200
    assert response.json() == {"skills": []}


def test_non_ascii_ops_secret_header_is_denied_not_500(monkeypatch, tmp_path):
    """Review round 2 MAJOR: hmac.compare_digest raises TypeError on non-ASCII
    str, and the header is caller-controlled latin-1 — a stray accent was an
    unauthenticated 500. Bytes comparison makes it a plain deny."""
    tenant = _bundle(tmp_path / "tenant", "tenant-safe", {"tenant-card": "T"})
    operator = _bundle(tmp_path / "operator", "operator", {"ops-card": "O"})
    monkeypatch.setenv("LEAF_SKILLS_BUNDLE_PATH", str(tenant))
    monkeypatch.setenv("LEAF_SKILLS_OPERATOR_BUNDLE_PATH", str(operator))
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")

    # httpx refuses non-ASCII str header values client-side, but a raw ASGI
    # caller delivers latin-1 BYTES — which decode into the non-ASCII str that
    # reached compare_digest. Send bytes to reproduce the real wire shape.
    response = _client().get(
        "/api/skills",
        headers={b"X-Ops-Secret": "sécrét".encode("latin-1")})

    assert response.status_code == 200
    assert {s_["id"] for s_ in response.json()["skills"]} == {"tenant-card"}, (
        "a non-ASCII header either 500'd or authorized operator access")
