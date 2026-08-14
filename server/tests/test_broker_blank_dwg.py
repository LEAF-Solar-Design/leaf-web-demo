"""Changed-path tests for the protected APS blank-DWG feasibility producer."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi.responses import JSONResponse


SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker  # noqa: E402


def _producer():
    path = SERVER_DIR / "da" / "blank_dwg.py"
    spec = importlib.util.spec_from_file_location("test_blank_dwg", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Da:
    ENGINE = "Autodesk.AutoCAD+24_3"
    DA = "https://example.invalid/da/us-east/v3"
    ALIAS = "prod"
    _HTTP_TIMEOUT = 5
    json = json

    def __init__(self, *, create_status="success", payload=b"AC1032-blank"):
        self.create_status = create_status
        self.payload = payload
        self.calls = []
        self.requests = SimpleNamespace(post=self._post)

    def _auth_headers(self):
        return {"Authorization": "Bearer redacted"}

    def _post(self, url, **kwargs):
        self.calls.append(("provision", url, kwargs["data"]))
        return _Response(409)

    def activity_qualified(self, activity):
        return f"leaf.{activity}+prod"

    def scratch_signed_upload_url(self, key):
        self.calls.append(("signed-output", key))
        return "upload-key", "https://signed.invalid/output"

    def submit_workitem(self, activity, arguments, **kwargs):
        self.calls.append(("create", activity, arguments))
        callback = kwargs.get("on_submitted")
        if callback:
            callback("wi-create")
        return {
            "id": "wi-create",
            "status": self.create_status,
            "stats": {
                "timeInstructionsStarted": "2026-08-13T00:00:00Z",
                "timeInstructionsEnded": "2026-08-13T00:00:02Z",
            },
        }

    def finalize_scratch_upload(self, key, upload_key):
        self.calls.append(("finalize", key, upload_key))

    def download_scratch_object(self, key):
        self.calls.append(("download", key))
        return self.payload

    def delete_scratch_object(self, key):
        self.calls.append(("delete", key))

    def run_tool(self, path, tool, params, **kwargs):
        self.calls.append(("read", Path(path).read_bytes(), tool["name"], params))
        return {
            "ok": True,
            "result": {"counts": {}},
            "cost": {"engine_seconds": 3.0, "usd_est": 0.01},
        }

    @staticmethod
    def _engine_seconds(status):
        return 2.0


def test_supported_probe_reads_exact_bytes_before_project_publication(monkeypatch):
    monkeypatch.setenv("APS_USD_PER_HR", "18")
    da = _Da()
    events = []

    def publish(payload, digest):
        da.calls.append(("publish", payload, digest))
        events.append(("publish", payload, digest))
        return {
            "tenant_id": "tenant",
            "project_id": "project",
            "drawing_id": "drawing",
            "version_id": "version",
            "version": 1,
            "object_key": "object",
            "sha256": digest,
        }

    result = _producer().run(
        da,
        tenant_id="tenant",
        source_sha="a" * 40,
        read_tool={"name": "count-by-layer"},
        publish=publish,
    )

    digest = hashlib.sha256(da.payload).hexdigest()
    assert result["status"] == "supported"
    assert result["output"] == {"sha256": digest, "bytes": len(da.payload), "version": 1}
    assert result["read"] == {
        "tool": "count-by-layer", "ok": True, "result": {"counts": {}}
    }
    assert result["drawing"]["sha256"] == digest
    assert result["cost"] == {"engine_seconds": 5.0, "usd_est": 0.02}
    assert next(i for i, call in enumerate(da.calls) if call[0] == "read") \
        < next(i for i, call in enumerate(da.calls) if call[0] == "publish")
    assert events == [("publish", da.payload, digest)]
    create = next(call for call in da.calls if call[0] == "create")
    assert create[2] == {"Result": {"url": "https://signed.invalid/output", "verb": "put"}}
    assert "HostDwg" not in create[2]


def test_failed_or_invalid_no_input_output_freezes_upload_only_fallback():
    producer = _producer()
    for da, reason in (
        (_Da(create_status="failed"), "no_input_activity_rejected"),
        (_Da(payload=b"not-a-dwg"), "invalid_dwg_output"),
    ):
        result = producer.run(
            da,
            tenant_id="tenant",
            source_sha="b" * 40,
            read_tool={"name": "count-by-layer"},
            publish=lambda *_args: (_ for _ in ()).throw(
                AssertionError("unsupported output must never publish")
            ),
        )
        assert result["status"] == "unsupported"
        assert result["fallback"] == "upload_only"
        assert result["reason"] == reason
        assert result["drawing"] is None
        assert result["cost"] == {"engine_seconds": 2.0, "usd_est": 0.0056}


def test_read_failure_is_unsupported_and_never_publishes():
    da = _Da()
    da.run_tool = lambda *_args, **_kwargs: {"ok": False, "error": "re-extract failed"}
    result = _producer().run(
        da,
        tenant_id="tenant",
        source_sha="c" * 40,
        read_tool={"name": "count-by-layer"},
        publish=lambda *_args: (_ for _ in ()).throw(
            AssertionError("failed read must never publish")
        ),
    )
    assert result["status"] == "unsupported"
    assert result["reason"] == "read_tool_failed"
    assert result["fallback"] == "upload_only"


def test_route_mints_fixed_live_broker_request(monkeypatch):
    captured = []
    monkeypatch.setattr(
        broker,
        "broker_run",
        lambda req: captured.append(req) or JSONResponse({"ok": True}),
    )
    tenant = "11111111-1111-4111-8111-111111111111"
    project = "22222222-2222-4222-8222-222222222222"
    response = broker.blank_dwg_feasibility(broker.BlankDwgFeasibilityRequest(
        tenant_id=tenant,
        project_id=project,
        ledger_event_key="event-1",
        job_id="33333333-3333-4333-8333-333333333333",
        source_sha="d" * 40,
    ))
    assert response.status_code == 200
    assert len(captured) == 1
    req = captured[0]
    assert req.tenant_id == tenant
    assert req.aps_live is True
    assert req.test_source is None
    assert req.tool == broker._blank_dwg_tool()
    assert req.params["project_id"] == project
    assert req.params["source_sha"] == "d" * 40
    assert isinstance(req, broker._BlankDwgBrokerRunRequest)


def test_public_broker_run_model_cannot_select_internal_blank_dwg_path():
    ordinary = broker.BrokerRunRequest(
        tenant_id="11111111-1111-4111-8111-111111111111",
        tool=broker._blank_dwg_tool(),
        params={
            "project_id": "22222222-2222-4222-8222-222222222222",
            "source_sha": "d" * 40,
            "drawing_name": "Blank",
        },
        aps_live=True,
    )
    assert broker._is_blank_dwg_request(ordinary, ordinary.tool) is False
    source = Path(broker.__file__).read_text(encoding="utf-8")
    route = source[source.index('"/broker/blank-dwg/feasibility"'):]
    route = route[:route.index('@app.post("/broker/run"')]
    assert "Depends(require_broker_reconcile_auth)" in route


def test_route_rejects_invalid_scope_before_broker_run(monkeypatch):
    monkeypatch.setattr(
        broker, "broker_run", lambda _req: (_ for _ in ()).throw(
            AssertionError("invalid scope must stop before broker execution")
        )
    )
    response = broker.blank_dwg_feasibility(broker.BlankDwgFeasibilityRequest(
        tenant_id="not-a-uuid",
        project_id="22222222-2222-4222-8222-222222222222",
        ledger_event_key="event-1",
        job_id="job-1",
        source_sha="d" * 40,
    ))
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["reason_code"] == "blank_dwg_scope_invalid"


def test_project_publisher_rejects_cross_tenant_scope_before_storage(monkeypatch):
    tenant = "11111111-1111-4111-8111-111111111111"
    project = "22222222-2222-4222-8222-222222222222"
    monkeypatch.setattr(
        broker.platform_link,
        "platform_store",
        lambda: SimpleNamespace(get_project=lambda _org, _project: None),
    )
    monkeypatch.setattr(
        broker,
        "_get_da",
        lambda: (_ for _ in ()).throw(
            AssertionError("cross-tenant project must stop before storage or APS")
        ),
    )
    try:
        broker._publish_blank_dwg(
            tenant_id=tenant,
            project_id=project,
            drawing_name="Blank",
            payload=b"AC1032-blank",
            digest=hashlib.sha256(b"AC1032-blank").hexdigest(),
        )
    except ValueError as exc:
        assert str(exc) == "project is unavailable for this tenant"
    else:
        raise AssertionError("cross-tenant project unexpectedly published")
