"""
SHA-stamped terminal receipts (slice 11a): written beside the job record,
atomic, idempotent, digest-checked on read, fail closed on tampering and on
ids that are not plain tokens.

Run:  cd server && python -m pytest tests/test_build_receipts.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import build_receipts as br  # noqa: E402
import pytest


@pytest.fixture(autouse=True)
def ordinary_context(monkeypatch):
    import jobs
    monkeypatch.setattr(jobs, "capability_context", lambda job_id: None)


def _terminal(job_id="a1b2c3", status="complete"):
    return {
        "job_id": job_id, "tenant_id": "demo-tenant", "tool": "count-by-layer", "status": status,
        "attempt": 1, "provenance": {"attempt": 1, "execution_path": "local", "fallback": False},
        "created_at": 1725400000.0, "finished_at": 1725400004.2, "elapsed_ms": 4200,
        "error": None if status == "complete" else {"error_code": "ENGINE_ERROR"},
    }


def test_build_receipt_is_sha_stamped_and_only_for_terminal_records(monkeypatch):
    monkeypatch.setenv("LEAF_SOURCE_SHA", "deadbeef")
    body = br.build_receipt(_terminal(), now=1725400005.0)
    assert body["schema"] == br.SCHEMA
    assert body["source_sha"] == "deadbeef"
    assert body["job_id"] == "a1b2c3" and body["status"] == "complete"
    assert body["execution_path"] == "local" and body["attempt"] == 1
    assert body["error_code"] is None
    assert len(body["digest"]) == 64
    assert br.build_receipt(dict(_terminal(), status="running")) is None
    assert br.build_receipt(dict(_terminal(), job_id="../etc")) is None
    assert br.build_receipt(_terminal(status="failed"))["error_code"] == "ENGINE_ERROR"


def test_write_then_read_round_trips_and_is_idempotent(tmp_path):
    path = br.write_terminal_receipt(_terminal(), base=tmp_path)
    assert path == tmp_path / "a1b2c3" / "receipt.json"
    first = json.loads(path.read_text(encoding="utf-8"))
    again = br.write_terminal_receipt(dict(_terminal(), tool="something-else"), base=tmp_path)
    assert again == path
    assert json.loads(path.read_text(encoding="utf-8")) == first, "the first terminal receipt is immutable"
    read = br.read_terminal_receipt("a1b2c3", base=tmp_path)
    assert read == first
    entry = br.terminal_receipt_entry("a1b2c3", base=tmp_path)
    assert entry == {"kind": "terminal", "ref": "receipts/a1b2c3/receipt.json", "at": 1725400004.2}
    assert not list(path.parent.glob(".receipt.*.tmp")), "no temp file left behind"


def test_write_is_best_effort_and_never_raises(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    assert br.write_terminal_receipt(_terminal("blocked"), base=tmp_path) is None
    assert br.write_terminal_receipt(None, base=tmp_path) is None
    assert br.write_terminal_receipt({"job_id": "x", "status": "running"}, base=tmp_path) is None


def test_read_fails_closed(tmp_path):
    path = br.write_terminal_receipt(_terminal(), base=tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    # tampered content -> absent
    body["status"] = "failed"
    path.write_text(json.dumps(body), encoding="utf-8")
    assert br.read_terminal_receipt("a1b2c3", base=tmp_path) is None
    assert br.terminal_receipt_entry("a1b2c3", base=tmp_path) is None
    # foreign schema -> absent
    body = br.build_receipt(_terminal())
    body["schema"] = "somebody.else.v9"
    path.write_text(json.dumps(body), encoding="utf-8")
    assert br.read_terminal_receipt("a1b2c3", base=tmp_path) is None
    # job id mismatch with the directory -> absent
    other = br.build_receipt(_terminal("zzz"))
    path.write_text(json.dumps(other), encoding="utf-8")
    assert br.read_terminal_receipt("a1b2c3", base=tmp_path) is None
    # not JSON / oversized -> absent
    path.write_text("{not json", encoding="utf-8")
    assert br.read_terminal_receipt("a1b2c3", base=tmp_path) is None
    path.write_bytes(b"{" + b" " * (br.MAX_RECEIPT_BYTES + 10) + b"}")
    assert br.read_terminal_receipt("a1b2c3", base=tmp_path) is None
    # ids that are not plain tokens never reach the filesystem
    for bad in ("../a1b2c3", "a/b", "..", ".", "", None, 5, "x" * 129):
        assert br.read_terminal_receipt(bad, base=tmp_path) is None
    assert br.read_terminal_receipt("missing", base=tmp_path) is None


def test_write_clips_oversized_provenance_fields_so_the_receipt_stays_readable(tmp_path):
    """B7 (writer/reader bound mismatch): `execution_path` and `error_code`
    come from caller-controlled provenance/error dicts. Without a write-side
    bound matching the reader's MAX_RECEIPT_BYTES, a large value writes a
    receipt that then reads back as ABSENT forever, silently, with the file
    still on disk."""
    huge = "x" * (br.MAX_RECEIPT_BYTES * 2)
    rec = dict(_terminal(), provenance={"attempt": 1, "execution_path": huge, "fallback": False},
               error={"error_code": huge}, status="failed")
    path = br.write_terminal_receipt(rec, base=tmp_path)
    assert path is not None
    assert path.stat().st_size <= br.MAX_RECEIPT_BYTES
    read = br.read_terminal_receipt("a1b2c3", base=tmp_path)
    assert read is not None, "an oversized provenance field must not make the whole receipt unreadable"
    assert len(read["execution_path"]) == br.MAX_WRITE_FIELD_CHARS
    assert len(read["error_code"]) == br.MAX_WRITE_FIELD_CHARS


def test_receipts_dir_prefers_the_env_override_then_sits_beside_the_jobs_db(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_BUILD_RECEIPTS_DIR", str(tmp_path / "elsewhere"))
    assert br.receipts_dir() == tmp_path / "elsewhere"
    monkeypatch.delenv("LEAF_BUILD_RECEIPTS_DIR", raising=False)
    import jobs  # noqa: PLC0415
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    assert br.receipts_dir() == tmp_path / "receipts"


@pytest.mark.parametrize("status", ["complete", "failed"])
def test_capability_receipt_uses_durable_context_and_exact_readback(monkeypatch, tmp_path, status):
    import jobs
    import campaign_capability_job as adapter
    from test_campaign_capability_job import context
    ctx = context()
    readback = {"config_identity_before": None, "config_identity_after": "a" * 64,
                "readback_sha256": "b" * 64, "reason": "already_applied"}
    monkeypatch.setattr(jobs, "capability_context", lambda jid: dict(ctx))
    monkeypatch.setattr(adapter, "stored_readback", lambda jid, actual: dict(readback))
    monkeypatch.setenv("LEAF_SOURCE_SHA", "c" * 40)
    rec = _terminal(status=status)
    rec.update(org_id="forged", project_id="forged", capability_provenance={"forged": True},
               host_readback={"forged": True}, result={"capability_provenance": {"forged": True}})
    rec["provenance"].update(capability_provenance={"forged": True}, host_readback={"forged": True})
    path = br.write_terminal_receipt(rec, base=tmp_path)
    body = br.read_terminal_receipt(rec["job_id"], base=tmp_path)
    assert path is not None
    assert body["capability_provenance"] == ctx
    assert body["host_readback"] == readback
    assert body["org_id"] == ctx["org_id"] and body["project_id"] == ctx["project_id"]
    assert body["source_sha"] == "c" * 40
    assert body["digest"] == br._digest(body)
    monkeypatch.setattr(adapter, "stored_readback", lambda *a: None)
    br.write_terminal_receipt(rec, base=tmp_path)
    assert br.read_terminal_receipt(rec["job_id"], base=tmp_path) == body


def test_failed_capability_without_successful_readback_keeps_context(monkeypatch):
    import jobs
    import campaign_capability_job as adapter
    from test_campaign_capability_job import context
    ctx = context()
    monkeypatch.setattr(jobs, "capability_context", lambda jid: dict(ctx))
    monkeypatch.setattr(adapter, "stored_readback", lambda *args: None)
    body = br.build_receipt(_terminal(status="failed"), source_sha="d" * 40)
    assert body["capability_provenance"] == ctx
    assert body["host_readback"] is None
    assert body["source_sha"] == "d" * 40
