"""Changed-path tests for the protected APS blank-DWG feasibility producer."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
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


# A payload that clears the recipe's plausible-DWG floor. The real engine writes
# ~31 KB from acad.dwt, so anything smaller than the floor is not a drawing.
def _dwg_bytes(marker: bytes = b"leaf") -> bytes:
    return b"AC1032" + marker + b"\x00" * 8192


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Da:
    ENGINE = "Autodesk.AutoCAD+24_3"
    DA = "https://example.invalid/da/us-east/v3"
    ALIAS = "prod"
    _HTTP_TIMEOUT = 5
    json = json

    def __init__(self, *, create_status="success", payload=None, counts=None,
                 activity_exists=False, live_activity=None, aliased_version=1):
        self.create_status = create_status
        self.payload = _dwg_bytes() if payload is None else payload
        self.counts = counts
        self.activity_exists = activity_exists
        self.live_activity = live_activity
        self.aliased_version = aliased_version
        self.published_versions = []
        self.alias_patches = []
        self.calls = []
        self.requests = SimpleNamespace(
            post=self._post, get=self._get, patch=self._patch
        )

    # -- APS provisioning surface -------------------------------------------
    def _auth_headers(self):
        return {"Authorization": "Bearer redacted"}

    def _post(self, url, **kwargs):
        body = json.loads(kwargs["data"])
        if url.endswith("/activities"):
            self.calls.append(("provision", url, kwargs["data"]))
            return _Response(409 if self.activity_exists else 200)
        if url.endswith("/versions"):
            self.calls.append(("publish-version", body))
            self.published_versions.append(body)
            return _Response(200, {"version": self.aliased_version + 1})
        if url.endswith("/aliases"):
            self.calls.append(("alias", body))
            return _Response(409 if self.activity_exists else 201)
        raise AssertionError(f"unexpected POST {url}")

    def _get(self, url, **_kwargs):
        if "/aliases/" in url:
            return _Response(200, {"version": self.aliased_version})
        if "/versions/" in url:
            return _Response(200, self.live_activity or {})
        raise AssertionError(f"unexpected GET {url}")

    def _patch(self, url, **kwargs):
        self.alias_patches.append(json.loads(kwargs["data"]))
        return _Response(200)

    def activity_qualified(self, activity):
        return f"leaf.{activity}+prod"

    # -- scratch object surface ---------------------------------------------
    def upload_scratch_object(self, local_path, key):
        self.calls.append(("upload-script", key, Path(local_path).read_text(
            encoding="utf-8")))

    def scratch_signed_download_url(self, key):
        self.calls.append(("signed-script", key))
        return "https://signed.invalid/script"

    def scratch_signed_upload_url(self, key):
        self.calls.append(("signed-output", key))
        return "upload-key", "https://signed.invalid/output"

    def finalize_scratch_upload(self, key, upload_key):
        self.calls.append(("finalize", key, upload_key))

    def download_scratch_object(self, key):
        self.calls.append(("download", key))
        return self.payload

    def delete_scratch_object(self, key):
        self.calls.append(("delete", key))

    # -- work surface --------------------------------------------------------
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

    def run_tool(self, path, tool, params, **kwargs):
        self.calls.append(("read", Path(path).read_bytes(), tool["name"], params))
        counts = self.counts
        if counts is None:
            counts = {self.marker_from_script(): 1}
        return {
            "ok": True,
            "result": {"counts": counts},
            "cost": {"engine_seconds": 3.0, "usd_est": 0.01},
        }

    def marker_from_script(self) -> str:
        """The marker this run actually put in the uploaded .scr."""
        script = next(call[2] for call in self.calls if call[0] == "upload-script")
        return re.search(r"LEAF-BLANK-MARKER=(\S+)\)", script).group(1).rstrip('"')

    @staticmethod
    def _engine_seconds(status):
        return 2.0


def _publish_ok(da):
    def publish(payload, digest):
        da.calls.append(("publish", payload, digest))
        return {
            "tenant_id": "tenant",
            "project_id": "project",
            "drawing_id": "drawing",
            "version_id": "version",
            "version": 1,
            "object_key": "object",
            "sha256": digest,
        }
    return publish


def _never_publish(*_args):
    raise AssertionError("an unsupported result must never publish")


def test_supported_probe_reads_exact_bytes_before_project_publication(monkeypatch):
    monkeypatch.setenv("APS_USD_PER_HR", "18")
    da = _Da()

    result = _producer().run(
        da,
        tenant_id="tenant",
        source_sha="a" * 40,
        read_tool={"name": "count-by-layer"},
        publish=_publish_ok(da),
    )

    digest = hashlib.sha256(da.payload).hexdigest()
    assert result["status"] == "supported"
    assert result["output"] == {"sha256": digest, "bytes": len(da.payload), "version": 1}
    assert result["read"]["tool"] == "count-by-layer"
    assert result["drawing"]["sha256"] == digest
    assert result["cost"] == {"engine_seconds": 5.0, "usd_est": 0.02}
    assert next(i for i, call in enumerate(da.calls) if call[0] == "read") \
        < next(i for i, call in enumerate(da.calls) if call[0] == "publish")


def test_marker_layer_is_unique_per_run_and_carried_on_the_receipt():
    producer = _producer()
    markers = set()
    for _ in range(3):
        da = _Da()
        result = producer.run(
            da,
            tenant_id="tenant",
            source_sha="a" * 40,
            read_tool={"name": "count-by-layer"},
            publish=_publish_ok(da),
        )
        assert result["marker_layer"] == da.marker_from_script()
        assert re.fullmatch(r"LEAF_BLANK_[0-9A-F]{12}", result["marker_layer"])
        markers.add(result["marker_layer"])
    assert len(markers) == 3, "the provenance marker must not repeat across runs"


def test_script_is_a_per_run_argument_and_no_input_drawing_is_referenced():
    da = _Da()
    _producer().run(
        da,
        tenant_id="tenant",
        source_sha="a" * 40,
        read_tool={"name": "count-by-layer"},
        publish=_publish_ok(da),
    )
    spec = json.loads(next(call[2] for call in da.calls if call[0] == "provision"))
    assert "settings" not in spec, "a baked-in script cannot carry a per-run marker"
    assert set(spec["parameters"]) == {"Script", "Result"}
    assert "HostDwg" not in spec["parameters"]
    assert "/i" not in spec["commandLine"][0]
    assert "$(args[Script].path)" in spec["commandLine"][0]

    create = next(call for call in da.calls if call[0] == "create")
    assert create[2] == {
        "Script": {"url": "https://signed.invalid/script", "verb": "get"},
        "Result": {"url": "https://signed.invalid/output", "verb": "put"},
    }


def test_script_uses_the_proven_command_recipe_and_never_activex():
    da = _Da()
    _producer().run(
        da,
        tenant_id="tenant",
        source_sha="a" * 40,
        read_tool={"name": "count-by-layer"},
        publish=_publish_ok(da),
    )
    script = next(call[2] for call in da.calls if call[0] == "upload-script")
    marker = da.marker_from_script()
    # (vlax-get-acad-object) is nil under accoreconsole - there is no COM
    # application object - so any ActiveX SaveAs produces no drawing at all.
    for forbidden in ("vlax-get-acad-object", "vla-SaveAs", "vla-get-ActiveDocument"):
        assert forbidden not in script
    assert f'(command "_.-LAYER" "_Make" "{marker}" "")' in script
    # The witness entity is what makes the marker visible to count-by-layer,
    # which counts model-space ENTITIES per layer, not layer-table names.
    assert '(command "_.POINT"' in script
    assert script.index('"_Make"') < script.index('"_.POINT"') < script.index('SAVEAS')
    assert '(command "_.SAVEAS" "2018" "blank.dwg")' in script


def test_scratch_script_and_output_are_both_discarded_after_the_run():
    da = _Da()
    _producer().run(
        da,
        tenant_id="tenant",
        source_sha="a" * 40,
        read_tool={"name": "count-by-layer"},
        publish=_publish_ok(da),
    )
    deleted = {call[1] for call in da.calls if call[0] == "delete"}
    assert any(key.endswith("/run.scr") for key in deleted)
    assert any(key.endswith("/blank.dwg") for key in deleted)


def test_scratch_objects_are_discarded_even_when_the_create_leg_fails():
    da = _Da(create_status="failed")
    _producer().run(
        da,
        tenant_id="tenant",
        source_sha="a" * 40,
        read_tool={"name": "count-by-layer"},
        publish=_never_publish,
    )
    assert len([call for call in da.calls if call[0] == "delete"]) == 2


def test_failed_or_invalid_no_input_output_freezes_upload_only_fallback():
    producer = _producer()
    cases = (
        (_Da(create_status="failed"), "no_input_activity_rejected"),
        (_Da(payload=b"not-a-dwg"), "invalid_dwg_output"),
        # A real DWG header but far below the floor a drawing written from
        # acad.dwt can plausibly have: a truncated download, not a drawing.
        (_Da(payload=b"AC1032" + b"\x00" * 16), "invalid_dwg_output"),
    )
    for da, reason in cases:
        result = producer.run(
            da,
            tenant_id="tenant",
            source_sha="b" * 40,
            read_tool={"name": "count-by-layer"},
            publish=_never_publish,
        )
        assert result["status"] == "unsupported"
        assert result["fallback"] == "upload_only"
        assert result["reason"] == reason
        assert result["drawing"] is None
        assert result["marker_layer"] == da.marker_from_script()
        assert result["cost"] == {"engine_seconds": 2.0, "usd_est": 0.0056}


def test_read_failure_is_unsupported_and_never_publishes():
    da = _Da()
    da.run_tool = lambda *_args, **_kwargs: {"ok": False, "error": "re-extract failed"}
    result = _producer().run(
        da,
        tenant_id="tenant",
        source_sha="c" * 40,
        read_tool={"name": "count-by-layer"},
        publish=_never_publish,
    )
    assert result["status"] == "unsupported"
    assert result["reason"] == "read_tool_failed"
    assert result["fallback"] == "upload_only"


def test_read_without_this_runs_marker_is_unsupported_and_never_publishes():
    """A paid 200 that cannot show our own marker has not proven whose bytes it read."""
    producer = _producer()
    for counts in ({}, {"0": 3}, {"LEAF_BLANK_AAAAAAAAAAAA": 1}, "not-an-object"):
        da = _Da(counts=counts)
        result = producer.run(
            da,
            tenant_id="tenant",
            source_sha="e" * 40,
            read_tool={"name": "count-by-layer"},
            publish=_never_publish,
        )
        assert result["status"] == "unsupported"
        assert result["reason"] == "provenance_mismatch"
        assert result["fallback"] == "upload_only"
        assert result["drawing"] is None


def test_marker_reported_rejects_every_shape_that_is_not_our_own_marker():
    marker_reported = _producer().marker_reported
    assert marker_reported({"counts": {"LEAF_BLANK_ABCDEF123456": 1}},
                           "LEAF_BLANK_ABCDEF123456") is True
    for result in (None, {}, {"counts": None}, {"counts": []}, {"counts": {}},
                   {"counts": {"OTHER": 1}}, "counts"):
        assert marker_reported(result, "LEAF_BLANK_ABCDEF123456") is False


def test_existing_activity_with_a_stale_body_is_republished_and_realiased():
    """409 does not mean 'already correct'. The first body shipped here could not
    produce a drawing at all, so keeping it would burn a WorkItem on known-broken
    code."""
    producer = _producer()
    stale = {
        "engine": "Autodesk.AutoCAD+24_3",
        "commandLine": [r'$(engine.path)\accoreconsole.exe /s "$(settings[script].path)"'],
        "parameters": {"Result": {"verb": "put", "required": True,
                                  "localName": "blank.dwg"}},
    }
    da = _Da(activity_exists=True, live_activity=stale, aliased_version=1)
    producer.run(
        da,
        tenant_id="tenant",
        source_sha="f" * 40,
        read_tool={"name": "count-by-layer"},
        publish=_publish_ok(da),
    )
    assert len(da.published_versions) == 1
    assert "id" not in da.published_versions[0]
    assert set(da.published_versions[0]["parameters"]) == {"Script", "Result"}
    assert da.alias_patches == [{"version": 2}]


def test_existing_activity_that_already_matches_is_not_republished():
    producer = _producer()
    current = producer.activity_spec("Autodesk.AutoCAD+24_3")
    da = _Da(activity_exists=True, live_activity=current, aliased_version=7)
    producer.run(
        da,
        tenant_id="tenant",
        source_sha="f" * 40,
        read_tool={"name": "count-by-layer"},
        publish=_publish_ok(da),
    )
    assert da.published_versions == []
    assert da.alias_patches == [{"version": 7}]


def test_producer_and_spike_share_one_recipe_module():
    """The consolidation invariant: there is exactly one blank-DWG recipe."""
    producer = _producer()
    recipe = producer._blank_lisp()
    assert recipe.__file__ == str(
        (SERVER_DIR.parent / "da" / "blank_lisp.py").resolve()
    )
    source = (SERVER_DIR / "da" / "blank_dwg.py").read_text(encoding="utf-8")
    body = source[source.index("def activity_spec"):]
    for forbidden in ("_.SAVEAS", "_.-LAYER", "vlax-get-acad-object"):
        assert forbidden not in body, "the recipe must live only in da/blank_lisp.py"


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
            payload=_dwg_bytes(),
            digest=hashlib.sha256(_dwg_bytes()).hexdigest(),
        )
    except ValueError as exc:
        assert str(exc) == "project is unavailable for this tenant"
    else:
        raise AssertionError("cross-tenant project unexpectedly published")


def test_publish_partial_matches_the_producer_positional_contract():
    """da/blank_dwg.py calls publish(payload, digest) POSITIONALLY on the
    functools.partial broker builds with three identity kwargs. The all-
    keyword-only signature this pins against shipped to staging and killed
    every rung-2 acceptance with a TypeError inside the one-off broker task
    (named live 2026-08-26); the bind below reproduces that call shape
    hermetically."""
    import functools
    import inspect

    publish = functools.partial(
        broker._publish_blank_dwg,
        tenant_id="00000000-0000-0000-0000-000000000000",
        project_id="00000000-0000-0000-0000-000000000001",
        drawing_name="APS blank drawing acceptance",
    )
    inspect.signature(publish).bind(b"AC1032", "0" * 64)
