"""The cutover gate must be binary and must never read unknown as safe."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


_SCRIPT = (Path(__file__).resolve().parents[2] / "scripts"
           / "identity_cutover_preflight.py")


def _load():
    spec = importlib.util.spec_from_file_location(
        "identity_cutover_preflight", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _fake_jobs(rows, terminal=("complete", "failed")):
    return SimpleNamespace(list_jobs=lambda limit=200: rows, TERMINAL=terminal)


def test_a_live_job_blocks_the_cutover(monkeypatch):
    preflight = _load()
    monkeypatch.setitem(
        __import__("sys").modules, "jobs",
        _fake_jobs([{"job_id": "j1", "tenant_id": "claim-a", "status": "running"}]))

    result = preflight.check_jobs_drained()

    assert result["ready"] is False
    assert result["live_jobs"][0]["job_id"] == "j1"


def test_an_empty_queue_is_ready(monkeypatch):
    preflight = _load()
    monkeypatch.setitem(
        __import__("sys").modules, "jobs",
        _fake_jobs([{"job_id": "j1", "tenant_id": "t", "status": "complete"}]))

    assert preflight.check_jobs_drained()["ready"] is True


def test_an_unknown_state_counts_as_live(monkeypatch):
    """Liveness is 'not terminal'. A state this script has never heard of must
    block, or a newly added non-terminal state would read as drained."""
    preflight = _load()
    monkeypatch.setitem(
        __import__("sys").modules, "jobs",
        _fake_jobs([{"job_id": "j1", "tenant_id": "t", "status": "quarantined"}]))

    assert preflight.check_jobs_drained()["ready"] is False


def test_a_truncated_listing_is_not_proof_of_drained(monkeypatch):
    """200 terminal rows do not rule out an older live one."""
    preflight = _load()
    rows = [{"job_id": f"j{i}", "tenant_id": "t", "status": "complete"}
            for i in range(200)]
    monkeypatch.setitem(__import__("sys").modules, "jobs", _fake_jobs(rows))

    result = preflight.check_jobs_drained()

    assert result["ready"] is False
    assert "truncated" in result["detail"]


def test_an_unreadable_job_store_blocks(monkeypatch):
    preflight = _load()

    def _explode(limit=200):
        raise RuntimeError("store down")

    monkeypatch.setitem(
        __import__("sys").modules, "jobs",
        SimpleNamespace(list_jobs=_explode, TERMINAL=("complete",)))

    assert preflight.check_jobs_drained()["ready"] is False


def test_a_claim_keyed_broker_record_blocks(tmp_path, monkeypatch):
    preflight = _load()
    path = tmp_path / "broker_tenants.json"
    path.write_text(json.dumps({"acceptance-tenant-a-20260728": {"tier": "restricted"}}),
                    encoding="utf-8")
    monkeypatch.setenv("BROKER_TENANTS", str(path))

    result = preflight.check_broker_records_rekeyed()

    assert result["ready"] is False
    assert result["stale_keys"] == ["acceptance-tenant-a-20260728"]


def test_platform_keyed_records_pass(tmp_path, monkeypatch):
    preflight = _load()
    path = tmp_path / "broker_tenants.json"
    path.write_text(
        json.dumps({"bccb0d64-04c9-4108-bcc1-f27b8bb3924d": {"tier": "hosted_pro"}}),
        encoding="utf-8")
    monkeypatch.setenv("BROKER_TENANTS", str(path))

    assert preflight.check_broker_records_rekeyed()["ready"] is True


def test_an_unreadable_broker_file_blocks(tmp_path, monkeypatch):
    preflight = _load()
    path = tmp_path / "broker_tenants.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("BROKER_TENANTS", str(path))

    assert preflight.check_broker_records_rekeyed()["ready"] is False


# --------------------------------------------------------------------------- #
# the gate must check the authority the broker ACTUALLY reads
# --------------------------------------------------------------------------- #
def test_postgres_mode_is_not_ready_just_because_the_json_is_absent(monkeypatch):
    """The regression that made this gate worse than useless.

    With LEAF_BROKER_STORE=postgres the broker reads disables and tiers from the
    broker_tenants TABLE. An earlier version reported READY for that
    configuration purely because the JSON file was missing, so a claim-keyed
    disable or restricted tier would have survived the cutover behind a green
    gate.
    """
    preflight = _load()
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.delenv("BROKER_TENANTS", raising=False)

    result = preflight.check_broker_records_rekeyed()

    assert result["ready"] is False
    assert "postgres" in result["name"]


def test_an_unrecognised_broker_store_blocks(monkeypatch):
    preflight = _load()
    monkeypatch.setenv("LEAF_BROKER_STORE", "something-new")

    assert preflight.check_broker_records_rekeyed()["ready"] is False


def test_legacy_mode_with_no_file_is_still_ready(monkeypatch, tmp_path):
    preflight = _load()
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setenv("BROKER_TENANTS", str(tmp_path / "absent.json"))

    assert preflight.check_broker_records_rekeyed()["ready"] is True


# --------------------------------------------------------------------------- #
# a drained snapshot is not a drained queue
# --------------------------------------------------------------------------- #
def test_producers_must_be_stopped_before_a_drain_means_anything(monkeypatch):
    """One listing proves the queue was empty at an instant. /api/run can commit
    a new raw-claim job immediately after, and an active turn can submit one
    too, so the gate refuses to imply a guarantee it never checked."""
    preflight = _load()
    monkeypatch.delenv("LEAF_CUTOVER_PRODUCERS_STOPPED", raising=False)

    assert preflight.check_producers_stopped()["ready"] is False

    monkeypatch.setenv("LEAF_CUTOVER_PRODUCERS_STOPPED", "1")
    assert preflight.check_producers_stopped()["ready"] is True


def test_the_verdict_is_blocked_by_any_single_check(monkeypatch):
    preflight = _load()
    monkeypatch.delenv("LEAF_CUTOVER_PRODUCERS_STOPPED", raising=False)
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setattr("sys.argv", ["preflight"])

    assert preflight.main() == 1
