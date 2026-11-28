"""Compare cold-voltage evidence from two saved, scope-resolved Solar revisions."""

import math
import re
from dataclasses import asdict, dataclass
from hashlib import sha256

from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError

_DIGEST = re.compile(r"[0-9a-f]{64}")
_REPORT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_CONCLUSIVE = frozenset({"pass", "fail"})


@dataclass(frozen=True)
class VoltageRevisionBinding:
    """Identity supplied by the normal authorized saved revision resolver."""

    organization_id: str
    project_id: str
    drawing_id: str
    input_version_id: str
    input_intake_sha256: str
    graph_sha256: str


def _invalid():
    raise GraphValidationError("SOLAR_VOLTAGE_COMPARISON_EVIDENCE_INVALID")


def _number(value):
    return type(value) in (int, float) and -math.inf < value < math.inf


def _binding_fields(binding):
    fields = asdict(binding)
    for key, value in fields.items():
        if not isinstance(value, str) or not value:
            _invalid()
        if key.endswith("_sha256") and not _DIGEST.fullmatch(value):
            _invalid()
    return fields


def _matches(fields, record):
    return isinstance(record, dict) and all(record.get(key) == value for key, value in fields.items())


def _string_rows(output):
    rows = {}
    if not isinstance(output.get("rows"), list):
        _invalid()
    for row in output["rows"]:
        ref = row.get("string_ref") if isinstance(row, dict) else None
        if not isinstance(ref, str) or not ref or ref in rows:
            _invalid()
        cold = row.get("cold_dc_limit")
        if not isinstance(cold, dict):
            _invalid()
        status = cold.get("status")
        if status == "unverified":
            reason = cold.get("reason")
            if not isinstance(reason, str) or not reason:
                _invalid()
            rows[ref] = {"status": status, "reason": reason}
        elif status in ("pass", "fail"):
            voltage, limit = cold.get("voc_v"), cold.get("saved_inverter_max_dc_v")
            if not _number(voltage) or not _number(limit) or voltage <= 0 or limit <= 0:
                _invalid()
            if status != ("pass" if voltage <= limit else "fail"):
                _invalid()
            rows[ref] = {"status": status, "voc_v": voltage, "saved_inverter_max_dc_v": limit}
        else:
            _invalid()
    return rows


def _read_side(binding, report, artifact):
    fields = _binding_fields(binding)
    side = dict(fields, report_status="unavailable", design_min_temp_c=None,
                output_sha256=None, downloadable_report_id=None)
    if report is None:
        if artifact is not None:
            _invalid()
        return side, None
    if (not _matches(fields, report)
            or report.get("schema") != "leaf.solar-project-read.v1"
            or report.get("tool") != "solar-guardrails-read"
            or report.get("drawing_changed") is not False):
        _invalid()
    output = report.get("output")
    if not isinstance(output, dict) or output.get("schema") != "leaf.solar-string-voltage.v1":
        _invalid()
    temperature = output.get("design_min_temp_c")
    if "design_min_temp_c" not in output or (temperature is not None and not _number(temperature)):
        _invalid()
    rows = _string_rows(output)
    if temperature is None and any(row["status"] in _CONCLUSIVE for row in rows.values()):
        _invalid()
    try:
        digest = sha256(canonical_bytes(output)).hexdigest()
    except (TypeError, ValueError, OverflowError):
        _invalid()
    if digest != report.get("output_sha256"):
        _invalid()
    side.update(report_status="verified", design_min_temp_c=temperature, output_sha256=digest)
    if artifact is not None:
        if not _matches(dict(fields, output_sha256=digest), artifact):
            _invalid()
        report_id = artifact.get("report_id")
        if not isinstance(report_id, str) or not _REPORT_ID.fullmatch(report_id):
            _invalid()
        side["downloadable_report_id"] = report_id
    return side, rows


def compare_saved_string_voltage(
    baseline, current, *, baseline_report=None, current_report=None,
    baseline_artifact=None, current_artifact=None,
):
    """Compare saved evidence without issuing reads, generating results or deciding approval.

    Report data must come from an authorized saved project read. An optional
    artifact must come independently from the authorized downloadable-report
    resolver for that side; caller-supplied JSON cannot attest artifact custody.
    This model checks the bindings again and returns report IDs, never URLs.
    """
    if (baseline.organization_id, baseline.project_id, baseline.drawing_id) != (
        current.organization_id, current.project_id, current.drawing_id
    ):
        _invalid()
    before, old_rows = _read_side(baseline, baseline_report, baseline_artifact)
    after, new_rows = _read_side(current, current_report, current_artifact)
    incomplete = old_rows is None or new_rows is None
    old_rows, new_rows = old_rows or {}, new_rows or {}
    rows = []
    for ref in sorted(old_rows.keys() | new_rows.keys()):
        old, new = old_rows.get(ref), new_rows.get(ref)
        if incomplete:
            change = "unknown"
        elif old is None:
            change = "added"
        elif new is None:
            change = "removed"
        elif old["status"] in _CONCLUSIVE and new["status"] == "unverified":
            change = "lost_coverage"
        elif old["status"] == "unverified" and new["status"] in _CONCLUSIVE:
            change = "gained_coverage"
        elif old == new and before["design_min_temp_c"] == after["design_min_temp_c"]:
            change = "unchanged"
        else:
            change = "changed"
        rows.append({"string_ref": ref, "change": change, "baseline": old, "current": new})

    return {
        "schema": "leaf.solar-string-voltage-comparison.v1",
        "baseline_kind": "saved_revision",
        "design_approval": "unverified",
        "baseline": before,
        "current": after,
        "rows": rows,
    }
