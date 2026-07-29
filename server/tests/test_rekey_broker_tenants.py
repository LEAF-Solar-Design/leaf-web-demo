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



# --------------------------------------------------------------------------- #
# the usage ledger, which is cost control and not just history
# --------------------------------------------------------------------------- #
NL = "\n"


def _ledger(path, *entries):
    path.write_text(
        "".join(json.dumps(entry) + NL for entry in entries), encoding="utf-8")
    return path


def test_the_ledger_is_rekeyed_line_by_line(tmp_path):
    rekey = _load()
    path = _ledger(tmp_path / "broker_ledger.jsonl",
                   {"tenant_id": CLAIM, "usd_est": 1.5},
                   {"tenant_id": PLATFORM, "usd_est": 2.0})

    report = rekey.rekey_ledger(path, {CLAIM: PLATFORM}, apply=True)

    assert report["ready"] is True
    assert report["rows_rekeyed"] == 1
    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row["tenant_id"] for row in rows] == [PLATFORM, PLATFORM]
    # Spend is preserved. Understating usage is the direction that costs money,
    # because prior spend feeds the daily quota.
    assert sorted(row["usd_est"] for row in rows) == [1.5, 2.0]


def test_an_unparseable_ledger_line_is_kept_not_dropped(tmp_path):
    """Dropping a spend record understates usage, the expensive way to be wrong."""
    rekey = _load()
    path = tmp_path / "broker_ledger.jsonl"
    path.write_text("{not json" + NL + json.dumps({"tenant_id": CLAIM}) + NL,
                    encoding="utf-8")

    report = rekey.rekey_ledger(path, {CLAIM: PLATFORM}, apply=True)

    assert report["rows_unparseable_kept"] == 1
    assert "{not json" in path.read_text(encoding="utf-8")


def test_an_unmapped_ledger_tenant_blocks_and_writes_nothing(tmp_path):
    rekey = _load()
    path = _ledger(tmp_path / "broker_ledger.jsonl", {"tenant_id": "stranger"})
    original = path.read_text(encoding="utf-8")

    report = rekey.rekey_ledger(path, {CLAIM: PLATFORM}, apply=True)

    assert report["ready"] is False
    assert report["unmapped_tenants"] == ["stranger"]
    assert path.read_text(encoding="utf-8") == original
    # "unchanged content" is not enough on its own: rewriting the file with the
    # unmapped row copied through is byte-identical, so the tell is that it must
    # NOT claim to have applied, and must not leave a backup implying it did.
    assert report.get("applied") is not True
    assert not path.with_suffix(path.suffix + ".pre-rekey").exists()


def test_a_platform_keyed_ledger_row_is_left_alone(tmp_path):
    rekey = _load()
    path = _ledger(tmp_path / "broker_ledger.jsonl",
                   {"tenant_id": PLATFORM, "usd_est": 1.0})

    report = rekey.rekey_ledger(path, {}, apply=True)

    assert report["ready"] is True
    assert report["rows_rekeyed"] == 0


def test_postgres_mode_is_reported_as_out_of_reach(monkeypatch):
    """Omission would read as clean, so the unreachable stores are named."""
    rekey = _load()
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")

    report = rekey.postgres_stores_report()

    assert report["ready"] is False
    assert "broker_usage_ledger" in report["detail"]

    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    assert rekey.postgres_stores_report()["ready"] is True
