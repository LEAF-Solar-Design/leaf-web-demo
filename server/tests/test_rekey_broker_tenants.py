"""The re-key tool must never invent a mapping or silently drop a record."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rekey_broker_tenants.py"


def _load():
    spec = importlib.util.spec_from_file_location("rekey_broker_tenants", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CLAIM = "acceptance-tenant-a-20260728"
PLATFORM = "bccb0d64-04c9-4108-bcc1-f27b8bb3924d"


def test_a_platform_id_is_recognised_and_a_claim_is_not():
    rekey = _load()
    assert rekey.looks_like_platform_id(PLATFORM) is True
    assert rekey.looks_like_platform_id(CLAIM) is False
    assert rekey.looks_like_platform_id("demo-tenant") is False


def test_an_unmappable_key_is_reported_not_dropped(monkeypatch):
    """Silently dropping it is the dangerous outcome: the record disappears and
    the tenant inherits DEFAULT_TIER, which grants nearly everything."""
    rekey = _load()
    monkeypatch.setattr(rekey, "resolve_platform_id", lambda claim: None)

    report = rekey.rekey({CLAIM: {"tier": "restricted"}})

    assert report["unmapped"] == [CLAIM]
    assert report["mapped"] == {}


def test_a_collision_is_reported_rather_than_overwriting(monkeypatch):
    rekey = _load()
    monkeypatch.setattr(rekey, "resolve_platform_id", lambda claim: PLATFORM)

    report = rekey.rekey({CLAIM: {"tier": "restricted"},
                          PLATFORM: {"tier": "hosted_pro"}})

    assert report["collisions"] == [{"from": CLAIM, "to": PLATFORM}]
    assert report["mapped"] == {}


def test_records_already_keyed_by_platform_id_are_left_alone(monkeypatch):
    rekey = _load()
    monkeypatch.setattr(rekey, "resolve_platform_id",
                        lambda claim: pytest.fail("must not resolve a UUID key"))

    report = rekey.rekey({PLATFORM: {"tier": "hosted_pro"}})

    assert report["already_platform"] == [PLATFORM]


def test_apply_refuses_while_anything_is_unmapped(tmp_path, monkeypatch):
    """A partial re-key is the worst outcome: some tenants keep their tightening
    record and others silently fall back to demo."""
    rekey = _load()
    path = tmp_path / "broker_tenants.json"
    path.write_text(json.dumps({CLAIM: {"tier": "restricted"}}), encoding="utf-8")
    monkeypatch.setattr(rekey, "resolve_platform_id", lambda claim: None)
    monkeypatch.setattr("sys.argv",
                        ["rekey", "--file", str(path), "--apply"])

    assert rekey.main() == 1
    # unchanged on disk
    assert json.loads(path.read_text(encoding="utf-8")) == {
        CLAIM: {"tier": "restricted"}}


def test_a_missing_file_is_not_an_error(tmp_path, monkeypatch):
    rekey = _load()
    monkeypatch.setattr(
        "sys.argv", ["rekey", "--file", str(tmp_path / "absent.json")])
    assert rekey.main() == 0
