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
