"""Static and schema checks for the dormant blank-DWG feasibility workflow."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_original_path = list(sys.path)
sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != ROOT]
sys.modules.pop("platform", None)
import platform as _stdlib_platform  # noqa: E402,F401
import jsonschema  # noqa: E402
import yaml  # noqa: E402
sys.path = _original_path


WORKFLOW = ROOT / ".github" / "workflows" / "prove-aps-blank-dwg.yml"
SCHEMA = ROOT / "contract" / "aps-blank-dwg-feasibility.v1.schema.json"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_is_manual_source_only_and_has_no_credential_or_live_surface():
    doc = _workflow()
    triggers = doc.get(True, doc.get("on"))
    assert triggers == {"workflow_dispatch": None}
    assert doc["permissions"] == {"contents": "read"}
    assert doc["concurrency"]["cancel-in-progress"] is False
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "LEAF_BLANK_DWG_LIVE_EXECUTION: UNCONFIGURED" in text
    assert "server/tests/test_broker_blank_dwg.py" in text
    assert "scripts/test_aps_blank_dwg_workflow.py" in text
    # The broker producer now runs the SAME recipe module as the proven T3-02
    # spike, so a regression in that recipe is a regression in this contract.
    assert "da/test_blank_spike.py" in text
    for forbidden in (
        "aws-actions/", "aws ", "APS_CRED", "APS_CLIENT", "APS_SECRET",
        "curl ", "/broker/blank-dwg/feasibility", "workflow_run:", "push:",
    ):
        assert forbidden not in text


def test_receipt_schema_accepts_supported_and_upload_only_results():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    digest = hashlib.sha256(b"AC1032-blank").hexdigest()
    supported = {
        "contract": "leaf.aps-blank-dwg-feasibility.v1",
        "status": "supported",
        "source_sha": "a" * 40,
        "activity": "LeafBlankDwgFeasibility",
        "marker_layer": "LEAF_BLANK_ABCDEF123456",
        "workitem_id": "wi-1",
        "output": {"sha256": digest, "bytes": 8192, "version": 1},
        "drawing": {
            "tenant_id": "11111111-1111-4111-8111-111111111111",
            "project_id": "22222222-2222-4222-8222-222222222222",
            "drawing_id": "33333333-3333-4333-8333-333333333333",
            "version_id": "44444444-4444-4444-8444-444444444444",
            "version": 1,
            "object_key": "tenants/t/drawings/d/v/00000001.dwg",
            "sha256": digest,
        },
        "read": {"tool": "count-by-layer", "ok": True, "result": {"counts": {}}},
        "cost": {"engine_seconds": 5.0, "usd_est": 0.02},
        "fallback": None,
        "reason": None,
        "degraded_mode": False,
    }
    unsupported = {
        "contract": "leaf.aps-blank-dwg-feasibility.v1",
        "status": "unsupported",
        "source_sha": "b" * 40,
        "activity": "LeafBlankDwgFeasibility",
        "marker_layer": "LEAF_BLANK_ABCDEF123456",
        "workitem_id": "wi-2",
        "output": None,
        "drawing": None,
        "read": None,
        "cost": {"engine_seconds": 2.0, "usd_est": 0.01},
        "fallback": "upload_only",
        "reason": "no_input_activity_rejected",
        "degraded_mode": False,
    }
    jsonschema.validate(supported, schema)
    jsonschema.validate(unsupported, schema)


def test_schema_rejects_false_support_without_project_version_or_read_witness():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    false_support = {
        "contract": "leaf.aps-blank-dwg-feasibility.v1",
        "status": "supported",
        "source_sha": "c" * 40,
        "activity": "LeafBlankDwgFeasibility",
        "marker_layer": "LEAF_BLANK_ABCDEF123456",
        "workitem_id": "wi-3",
        "output": None,
        "drawing": None,
        "read": None,
        "cost": None,
        "fallback": None,
        "reason": None,
        "degraded_mode": False,
    }
    try:
        jsonschema.validate(false_support, schema)
    except jsonschema.ValidationError:
        return
    raise AssertionError("false supported receipt unexpectedly validated")


def test_schema_marker_layer_matches_what_the_recipe_actually_mints():
    """The receipt pattern and the recipe must not drift apart.

    The marker is the only thing standing between "the read returned 200" and
    "the read returned OUR bytes", so a schema that no longer matches a freshly
    minted marker would reject every real receipt.
    """
    sys.path.insert(0, str(ROOT / "da"))
    try:
        import blank_lisp
    finally:
        sys.path.pop(0)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    pattern = schema["properties"]["marker_layer"]["pattern"]
    for _ in range(50):
        assert re.fullmatch(pattern, blank_lisp.new_marker_layer())


def test_schema_rejects_a_receipt_with_no_provenance_marker():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    unmarked = {
        "contract": "leaf.aps-blank-dwg-feasibility.v1",
        "status": "unsupported",
        "source_sha": "d" * 40,
        "activity": "LeafBlankDwgFeasibility",
        "workitem_id": "wi-4",
        "output": None,
        "drawing": None,
        "read": None,
        "cost": None,
        "fallback": "upload_only",
        "reason": "provenance_mismatch",
        "degraded_mode": False,
    }
    try:
        jsonschema.validate(unmarked, schema)
    except jsonschema.ValidationError:
        return
    raise AssertionError("a receipt with no marker_layer unexpectedly validated")


def test_schema_rejects_an_implausibly_small_drawing():
    """A DWG the engine wrote from acad.dwt is ~30 KB. A few bytes with an AC10
    header is a truncated download, not drawing version 1."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    digest = hashlib.sha256(b"AC1032-blank").hexdigest()
    tiny = {
        "contract": "leaf.aps-blank-dwg-feasibility.v1",
        "status": "supported",
        "source_sha": "e" * 40,
        "activity": "LeafBlankDwgFeasibility",
        "marker_layer": "LEAF_BLANK_ABCDEF123456",
        "workitem_id": "wi-5",
        "output": {"sha256": digest, "bytes": 12, "version": 1},
        "drawing": {
            "tenant_id": "11111111-1111-4111-8111-111111111111",
            "project_id": "22222222-2222-4222-8222-222222222222",
            "drawing_id": "33333333-3333-4333-8333-333333333333",
            "version_id": "44444444-4444-4444-8444-444444444444",
            "version": 1,
            "object_key": "tenants/t/drawings/d/v/00000001.dwg",
            "sha256": digest,
        },
        "read": {"tool": "count-by-layer", "ok": True,
                 "result": {"counts": {"LEAF_BLANK_ABCDEF123456": 1}}},
        "cost": {"engine_seconds": 5.0, "usd_est": 0.02},
        "fallback": None,
        "reason": None,
        "degraded_mode": False,
    }
    try:
        jsonschema.validate(tiny, schema)
    except jsonschema.ValidationError:
        return
    raise AssertionError("an implausibly small drawing unexpectedly validated")


def _producer():
    """Load the broker producer the same way the broker itself does: by path."""
    import importlib.util
    path = ROOT / "server" / "da" / "blank_dwg.py"
    spec = importlib.util.spec_from_file_location("contract_blank_dwg", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeDa:
    """The smallest APS surface the producer touches. Deliberately in THIS file:
    it must be the real producer's output that meets the real schema, and this
    module is the one that can import jsonschema past the repo's `platform`
    package shadow (see the sys.path dance at the top)."""

    ENGINE = "Autodesk.AutoCAD+24_3"
    DA = "https://example.invalid/da/us-east/v3"
    ALIAS = "prod"
    _HTTP_TIMEOUT = 5
    json = json

    def __init__(self, *, create_status="success", payload=None, counts=None,
                 read_ok=True):
        from types import SimpleNamespace
        self.create_status = create_status
        self.payload = payload if payload is not None else b"AC1032" + b"\x00" * 8192
        self.counts = counts
        self.read_ok = read_ok
        self.script = ""
        self.requests = SimpleNamespace(
            post=lambda url, **kw: _Ok(200),
            get=lambda url, **kw: _Ok(200, {}),
            patch=lambda url, **kw: _Ok(200),
        )

    def _auth_headers(self):
        return {}

    def activity_qualified(self, activity):
        return f"leaf.{activity}+prod"

    def upload_scratch_object(self, local_path, _key):
        self.script = Path(local_path).read_text(encoding="utf-8")

    def scratch_signed_download_url(self, _key):
        return "https://signed.invalid/script"

    def scratch_signed_upload_url(self, _key):
        return "upload-key", "https://signed.invalid/output"

    def finalize_scratch_upload(self, _key, _upload_key):
        pass

    def download_scratch_object(self, _key):
        return self.payload

    def delete_scratch_object(self, _key):
        pass

    def submit_workitem(self, _activity, _arguments, **_kwargs):
        return {"id": "wi-1", "status": self.create_status}

    def marker(self):
        return re.search(r"LEAF-BLANK-MARKER=(LEAF_BLANK_[0-9A-F]+)", self.script).group(1)

    def run_tool(self, _path, tool, _params, **_kwargs):
        if not self.read_ok:
            return {"ok": False, "error": "re-extract failed"}
        counts = self.counts if self.counts is not None else {self.marker(): 1}
        return {"ok": True, "result": {"counts": counts},
                "cost": {"engine_seconds": 3.0, "usd_est": 0.01}}

    @staticmethod
    def _engine_seconds(_status):
        return 2.0


class _Ok:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_every_receipt_the_producer_can_emit_validates_against_the_contract():
    """The producer and the contract must not drift apart.

    Checking them separately lets the producer emit a shape the schema rejects,
    which would only surface on the one paid run this route ever makes. So walk
    every terminal branch of run() and validate what actually comes out of it.
    """
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    producer = _producer()

    def publish(_payload, digest):
        return {
            "tenant_id": "11111111-1111-4111-8111-111111111111",
            "project_id": "22222222-2222-4222-8222-222222222222",
            "drawing_id": "33333333-3333-4333-8333-333333333333",
            "version_id": "44444444-4444-4444-8444-444444444444",
            "version": 1,
            "object_key": "tenants/t/drawings/d/v/00000001.dwg",
            "sha256": digest,
        }

    cases = [
        (_FakeDa(), "supported"),
        (_FakeDa(create_status="failed"), "no_input_activity_rejected"),
        (_FakeDa(payload=b"AC1032" + b"\x00" * 16), "invalid_dwg_output"),
        (_FakeDa(read_ok=False), "read_tool_failed"),
        (_FakeDa(counts={}), "provenance_mismatch"),
    ]
    seen = set()
    for da, expected in cases:
        receipt = producer.run(
            da,
            tenant_id="11111111-1111-4111-8111-111111111111",
            source_sha="a" * 40,
            read_tool={"name": "count-by-layer"},
            publish=publish,
        )
        jsonschema.validate(receipt, schema)
        assert (receipt["reason"] or receipt["status"]) == expected
        assert receipt["marker_layer"] == da.marker()
        seen.add(expected)
    assert len(seen) == len(cases)


def test_the_closed_contract_is_a_credential_guard():
    """`additionalProperties: false` here is load-bearing SECURITY, not tidiness.

    leaf-web-demo went public 2026-08-24. APS returns presigned reportUrls whose
    query string carries a temporary AWS credential (X-Amz-Signature and
    friends), and GitHub secret scanning opened six alerts the same day on other
    committed receipts that had them. This receipt cannot acquire that defect,
    for two reasons that must BOTH keep holding:

      1. server/da/blank_dwg.py never reads reportUrl at all, and
      2. the contract is CLOSED, so a future edit that started smuggling one in
         would fail validation instead of shipping.

    Anyone loosening these to "just add one field" is removing a credential
    guard. This test is here so they find that out from a red test rather than
    from a secret-scanning alert.
    """
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    for field in ("output", "drawing", "read"):
        obj = next(branch for branch in schema["properties"][field]["oneOf"]
                   if branch.get("type") == "object")
        assert obj["additionalProperties"] is False, field

    base = {
        "contract": "leaf.aps-blank-dwg-feasibility.v1",
        "status": "unsupported",
        "source_sha": "a" * 40,
        "activity": "LeafBlankDwgFeasibility",
        "marker_layer": "LEAF_BLANK_ABCDEF123456",
        "workitem_id": "wi-1",
        "output": None, "drawing": None, "read": None,
        "cost": None, "fallback": "upload_only",
        "reason": "no_input_activity_rejected", "degraded_mode": False,
    }
    jsonschema.validate(base, schema)  # the clean receipt is valid

    leaky = dict(base, reportUrl=(
        "https://dasprod-store.s3.amazonaws.com/report.txt"
        "?X-Amz-Signature=deadbeef&X-Amz-Security-Token=abc"))
    try:
        jsonschema.validate(leaky, schema)
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError(
            "the contract accepted a receipt carrying a presigned reportUrl - "
            "the credential guard is gone")


def test_the_producer_never_reads_report_url():
    """Belt to the schema's braces: the leak cannot start upstream either."""
    source = (ROOT / "server" / "da" / "blank_dwg.py").read_text(encoding="utf-8")
    assert "reportUrl" not in source
