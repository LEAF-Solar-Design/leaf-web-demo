"""Construct a first Solar graph and locate its graphless intake parent."""
import hashlib
import math
import re

import json
import write_loop
import store
from solar_design_graph import GraphValidationError, validate_graph
from tenant_id_validator import validate_tenant_id


SEED_SCHEMA_VERSION = 1
UNIT_SCALE = {"m": 1, "mm": .001, "cm": .01, "km": 1000,
              "in": .0254, "ft": .3048, "yd": .9144}


def seed_entity_id(tenant_id, drawing_id, kind):
    if kind not in ("project", "settings"):
        raise GraphValidationError("UNKNOWN_ENTITY_KIND")
    try:
        validate_tenant_id(tenant_id)
        validate_tenant_id(drawing_id, kind="drawing id")
    except ValueError:
        raise GraphValidationError("INVALID_SEED_REQUEST") from None
    value = list(hashlib.sha256(
        ("leaf-solar-seed\0" + tenant_id + "\0" + drawing_id + "\0" + kind)
        .encode("utf-8")).hexdigest()[:32])
    value[12] = "4"
    value[16] = "89ab"[int(value[16], 16) % 4]
    value = "".join(value)
    return "leaf:" + kind + ":" + "-".join(
        (value[:8], value[8:12], value[12:16], value[16:20], value[20:]))


def seed_units(units):
    if (not isinstance(units, dict) or set(units) != {
            "drawing_units", "wcs_to_ucs", "elevation_datum", "crs"}):
        raise GraphValidationError("INVALID_SEED_REQUEST")
    name = units["drawing_units"]
    matrix = units["wcs_to_ucs"]
    datum = units["elevation_datum"]
    crs = units["crs"]
    if (not isinstance(name, str) or name not in UNIT_SCALE
            or not isinstance(matrix, list) or len(matrix) != 16
            or any(type(v) not in (int, float) or
                   (type(v) is int and v.bit_length() > 64) or
                   (type(v) is float and not math.isfinite(v)) for v in matrix)
            or not isinstance(datum, str) or not 1 <= len(datum) <= 4096
            or not (crs is None or isinstance(crs, str) and len(crs) <= 4096)):
        raise GraphValidationError("INVALID_SEED_REQUEST")
    try:
        datum.encode("utf-8")
        if isinstance(crs, str):
            crs.encode("utf-8")
    except UnicodeError:
        raise GraphValidationError("INVALID_SEED_REQUEST") from None
    return {"drawing_units": name, "meters_per_unit": UNIT_SCALE[name],
            "source": "explicit", "compute_units": "m", "wcs_to_ucs": matrix[:],
            "elevation_datum": datum, "crs": crs,
            "drawing_unit_is_feet": name == "ft", "warnings": []}


def new_empty_graph(*, tenant_id, drawing_id, source_hash, units, created_at):
    try:
        validate_tenant_id(tenant_id)
        validate_tenant_id(drawing_id, kind="drawing id")
    except ValueError:
        raise GraphValidationError("INVALID_SEED_REQUEST") from None
    if (not isinstance(source_hash, str) or not re.fullmatch("[0-9a-f]{64}", source_hash)
            or not isinstance(created_at, str) or not 1 <= len(created_at) <= 4096):
        raise GraphValidationError("INVALID_SEED_REQUEST")
    resolved_units = seed_units(units)

    def entity(kind, state, reasons):
        return {"id": seed_entity_id(tenant_id, drawing_id, kind), "kind": kind,
                "rev": 0, "extra": {}, "validity": {"state": state, "reasons": reasons},
                "provenance": {"created_by": "solar-seed", "created_at": created_at,
                               "last_writer": "solar-seed", "source_rev": 0,
                               "source_hash": source_hash, "tool_id": "solar-settings"}}

    project = entity("project", "unknown", ["seed_defaults"])
    project.update(name="", zip_code="", latitude=None, longitude=None,
                   installation_design="Roof", units=resolved_units,
                   graph_schema_version=1, site_revision="seed-" + source_hash[:16])
    settings = entity("settings", "valid", [])
    settings.update(panel_layer_contains="Panel", panel_group_layer="Panel Group",
                    string_layer="String", home_run_layer="HomeRun", panels_in_sequence=0,
                    num_mppt=0, strings_per_mppt=0, optimizer_ratio=1,
                    use_l2_collectors=False, panel_group_number=1, string_number=1,
                    inverter_number=1, mppt_letter="A", global_string_sizing_confirmed=False,
                    voc_cold={"passes": None, "override_accepted": False,
                              "suggested_string_length": None, "per_module": None,
                              "string_voltage": None, "max_dc_voltage": None})
    return validate_graph({
        "graph_schema_version": 1, "rev": 0, "parent_rev": None,
        "source_hash": source_hash, "catalog_versions": {},
        "project": project, "settings": settings,
        "electrical_zones": [], "frames": [], "panels": [], "strings": [],
        "inverters": [], "routes": [], "schedules": [], "opaque_stores": {},
        "orphaned_xdata": [], "extra": {"seed": {
            "schema_version": SEED_SCHEMA_VERSION, "source_intake_sha256": source_hash}}})


def resolve_seed_context(backend, tenant_id, drawing_id, version, *, source_intake_sha256):
    try:
        validate_tenant_id(tenant_id)
        validate_tenant_id(drawing_id, kind="drawing id")
    except ValueError:
        raise GraphValidationError("GRAPH_CONTEXT_UNAVAILABLE") from None
    if type(version) is not int or version < 1:
        raise GraphValidationError("INVALID_PARENT_VERSION")
    if (not isinstance(source_intake_sha256, str)
            or not re.fullmatch("[0-9a-f]{64}", source_intake_sha256)):
        raise GraphValidationError("INVALID_SEED_REQUEST")
    try:
        resolved, key, entry = store.resolve_version_entry(backend, tenant_id, drawing_id, version)
        manifest = store.load_manifest(
            backend, store.sanitize_id(tenant_id), store.sanitize_id(drawing_id))
        head = manifest["head"]
        note = entry.get("note") or ""
        if type(head) is not int or head < 1 or type(note) is not str:
            raise ValueError("invalid manifest")
    except (KeyError, ValueError, TypeError, OSError):
        raise GraphValidationError("GRAPH_CONTEXT_UNAVAILABLE") from None
    if note.startswith("solar-bundle:"):
        raise GraphValidationError("INVALID_SEED_PARENT")
    try:
        data = backend.get(key)
    except (KeyError, ValueError, TypeError, OSError):
        raise GraphValidationError("GRAPH_CONTEXT_UNAVAILABLE") from None
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != entry.get("sha256"):
        raise GraphValidationError("INVALID_SEED_PARENT")
    try:
        intake = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise GraphValidationError("INVALID_SEED_PARENT") from None
    if not isinstance(intake, dict):
        raise GraphValidationError("INVALID_SEED_PARENT")
    companions = sum(key in intake for key in ("solar_design_graph", "solar_design_graph_sha256"))
    if companions == 2:
        raise GraphValidationError("GRAPH_ALREADY_EMBEDDED")
    if companions == 1:
        raise GraphValidationError("INVALID_SEED_PARENT")
    if actual_hash != source_intake_sha256:
        raise GraphValidationError("SOURCE_HASH_MISMATCH")
    return {"resolved_version": resolved, "current_head": head, "intake": intake,
            "intake_sha256": actual_hash, "created": entry["created"],
            "seed_ready": resolved == head,
            "refusal_reason": None if resolved == head else "not_current_head"}
