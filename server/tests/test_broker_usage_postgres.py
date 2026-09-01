from __future__ import annotations

import json
import platform as _stdlib_platform
import sys
from pathlib import Path

_stdlib_platform.python_implementation()

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker_pg_store  # noqa: E402
from routers import ops as ops_router  # noqa: E402
from routers import usage as usage_router  # noqa: E402


class _Store:
    def __init__(self):
        self.calls = []

    def aggregate_usage(self, tenant_id):
        self.calls.append(("aggregate", tenant_id))
        values = {
            "tenant-a": {
                "today": {"runs": 2, "usd_est": 0.123457},
                "total": {"runs": 3, "usd_est": 0.234568},
            },
            "tenant-b": {
                "today": {"runs": 0, "usd_est": 0.0},
                "total": {"runs": 1, "usd_est": 1.0},
            },
        }
        return values[tenant_id]

    def usage_tenant_ids(self):
        self.calls.append(("tenants",))
        return ["tenant-a", "tenant-b"]

    def disabled_tenant_ids(self):
        self.calls.append(("disabled",))
        return ["tenant-b"]


class _UsageModule:
    @staticmethod
    def cap_for(_tenant_id):
        return 1.0


def _stale_file_guard(*_args, **_kwargs):
    raise AssertionError("PostgreSQL usage path touched stale JSONL")


def test_tenant_usage_postgres_never_reads_stale_jsonl(monkeypatch):
    store = _Store()
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.setattr(broker_pg_store, "get_store", lambda: store)
    monkeypatch.setattr(usage_router, "_usage_mod", lambda: _UsageModule())
    monkeypatch.setattr(usage_router, "_ledger_path", _stale_file_guard)
    monkeypatch.setattr(
        usage_router.agent_ledger, "aggregate",
        lambda _tenant: {"today": {}, "cycle": {}},
    )

    body = usage_router.usage("tenant-a")
    assert body["today"] == {"runs": 2, "usd_est": 0.123457}
    assert body["total"] == {"runs": 3, "usd_est": 0.234568}
    assert body["cap"] == {
        "usd_cap": 1.0, "remaining": 0.765432, "enabled": True,
    }
    assert store.calls == [("aggregate", "tenant-a")]


def test_ops_usage_postgres_never_reads_stale_jsonl(monkeypatch):
    store = _Store()
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.delenv("LEAF_OPS_SECRET", raising=False)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.setattr(broker_pg_store, "get_store", lambda: store)
    monkeypatch.setattr(ops_router, "_ledger_path", _stale_file_guard)
    monkeypatch.setattr(ops_router, "_distinct_tenants", _stale_file_guard)
    monkeypatch.setattr(
        ops_router.requests, "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ConnectionError("broker unavailable")),
    )
    body = ops_router.ops_tenants()
    assert body["tenants"] == [
        {"tenant_id": "tenant-a", "runs": 3, "usd_est": 0.234568,
         "disabled": False,
         "llm_turns": 0, "llm_cost_tokens": 0, "llm_usd_est": 0.0},
        {"tenant_id": "tenant-b", "runs": 1, "usd_est": 1.0,
         "disabled": True,
         "llm_turns": 0, "llm_cost_tokens": 0, "llm_usd_est": 0.0},
    ]
    # The postgres authority still owns the AutoCAD half of the platform block.
    assert body["platform"]["autocad_backend"] == {"runs": 4, "usd_est": 1.234568}
    assert ("tenants",) in store.calls
    assert ("disabled",) in store.calls


def test_legacy_tenant_usage_shape_is_unchanged(monkeypatch, tmp_path):
    ledger = tmp_path / "legacy.jsonl"
    ledger.write_text("\n".join([
        json.dumps({
            "ts": 1, "tenant_id": "tenant-a", "tool": "x",
            "engine_op": "x", "aps_endpoint": "x", "aps_live": False,
            "engine_seconds": None, "usd_est": 0.1, "status": "ok",
        }),
        json.dumps({
            "ts": 2, "tenant_id": "tenant-a", "tool": "x",
            "engine_op": "x", "aps_endpoint": "x", "aps_live": False,
            "engine_seconds": None, "usd_est": None,
            "status": "quota_exceeded",
        }),
    ]) + "\n", encoding="utf-8")
    monkeypatch.delenv("LEAF_BROKER_STORE", raising=False)
    monkeypatch.setenv("LEAF_USAGE_LEDGER", str(ledger))
    monkeypatch.delenv("LEAF_TENANT_CAP_USD", raising=False)
    monkeypatch.setattr(
        usage_router.agent_ledger, "aggregate",
        lambda _tenant: {"today": {}, "cycle": {}},
    )
    body = usage_router.usage("tenant-a")
    assert body["total"] == {"runs": 1, "usd_est": 0.1}
    assert body["cap"] == {
        "usd_cap": None, "remaining": None, "enabled": False,
    }


def test_legacy_ops_usage_still_reads_jsonl(monkeypatch, tmp_path):
    ledger = tmp_path / "legacy-ops.jsonl"
    ledger.write_text(json.dumps({
        "ts": 1, "tenant_id": "tenant-a", "tool": "x",
        "engine_op": "x", "aps_endpoint": "x", "aps_live": False,
        "engine_seconds": None, "usd_est": 0.25, "status": "ok",
    }) + "\n", encoding="utf-8")
    monkeypatch.delenv("LEAF_BROKER_STORE", raising=False)
    monkeypatch.setenv("LEAF_USAGE_LEDGER", str(ledger))
    monkeypatch.delenv("LEAF_OPS_SECRET", raising=False)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.setattr(
        broker_pg_store, "get_store",
        lambda: (_ for _ in ()).throw(
            AssertionError("legacy ops path touched PostgreSQL")),
    )

    class _Health:
        @staticmethod
        def json():
            return {"tenants_disabled": []}

    monkeypatch.setattr(ops_router.requests, "get", lambda *_a, **_k: _Health())
    body = ops_router.ops_tenants()
    assert body["tenants"] == [{
        "tenant_id": "tenant-a", "runs": 1, "usd_est": 0.25,
        "disabled": False,
        "llm_turns": 0, "llm_cost_tokens": 0, "llm_usd_est": 0.0,
    }]
