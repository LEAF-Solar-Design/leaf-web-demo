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


def test_missing_env_returns_200_empty(monkeypatch):
    monkeypatch.delenv("LEAF_SKILLS_BUNDLE_PATH", raising=False)
    monkeypatch.delenv("LEAF_SKILLS_OPERATOR_BUNDLE_PATH", raising=False)

    response = _client().get("/api/skills")

    assert response.status_code == 200
    assert response.json() == {"skills": []}
