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
