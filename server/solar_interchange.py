"""Read-only import boundary for the solar design graph.

Private store meanings come only from the configured adapter. Inspection never
loads an adapter and never executes code from drawing records.

No schema fields are loosened in this slice. Existing nullable fields retain
their schema. String endpoints and module count use ordered membership, so
missing plugin values do not require nullable membership or identity fields.
"""
from __future__ import annotations

import copy
import json
import os
import re
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

try:
    from .solar_design_graph import GraphValidationError, _bounded_json, new_id, validate_graph
except ImportError:
    from solar_design_graph import GraphValidationError, _bounded_json, new_id, validate_graph


SCOPES = {"named-dictionary", "extension-dictionary", "entity-xdata"}
COLLECTIONS = {"zone-el": "electrical_zones", "frame": "frames", "panel": "panels",
               "string": "strings", "inverter": "inverters", "route": "routes",
               "schedule": "schedules"}
UNIT_SCALES = {"m": 1.0, "mm": .001, "cm": .01, "km": 1000.0,
               "in": .0254, "ft": .3048, "yd": .9144}
MAX_ADAPTER_BYTES = 1024 * 1024
MAX_INSPECTION_BYTES = 16 * 1024 * 1024
RESERVED = {"id", "kind", "rev", "provenance", "extra", "validity"}
_NO_INTAKE = object()


def _refuse(code):
    raise GraphValidationError(code)


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _refuse("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError):
        _refuse("INVALID_INTERCHANGE_JSON")
    _bounded_json(value)
    return value


def validate_inspection(value):
    """Validate the closed neutral payload without interpreting opaque stores."""
    _bounded_json(value)
    if type(value) is not dict or set(value) != {"schema", "dwg_sha256", "units", "stores", "entities"}:
        _refuse("INVALID_INSPECTION")
    if value["schema"] != "leaf.solar-inspection.v1" or not re.fullmatch(r"[0-9a-f]{64}", str(value["dwg_sha256"])):
        _refuse("INVALID_INSPECTION_SOURCE")
    units = value["units"]
    if type(units) is not dict or set(units) != {"drawing_units", "meters_per_unit"}:
        _refuse("UNKNOWN_UNITS")
    scale = units["meters_per_unit"]
    if (type(units["drawing_units"]) is not str or units["drawing_units"] not in UNIT_SCALES
            or type(scale) not in (int, float)
            or abs(scale - UNIT_SCALES[units["drawing_units"]]) > 1e-12):
        _refuse("UNKNOWN_UNITS")
    if type(value["entities"]) is not list or type(value["stores"]) is not list:
        _refuse("INVALID_INSPECTION")
    handles = set()
    for entity in value["entities"]:
        if type(entity) is not dict or set(entity) != {"handle", "layer", "kind", "block_name", "geometry"}:
            _refuse("INVALID_INSPECTION_ENTITY")
        handle = entity["handle"]
        if type(handle) is not str or not re.fullmatch(r"[0-9A-Fa-f]{1,32}", handle) or handle.upper() in handles:
            _refuse("INVALID_INSPECTION_HANDLE")
        handles.add(handle.upper())
        if any(type(entity[k]) is not str or len(entity[k]) > 4096 for k in ("layer", "kind")):
            _refuse("INVALID_INSPECTION_ENTITY")
        if entity["block_name"] is not None and (type(entity["block_name"]) is not str or len(entity["block_name"]) > 4096):
            _refuse("INVALID_INSPECTION_ENTITY")
        geometry = entity["geometry"]
        if type(geometry) is not dict or set(geometry) != {"bbox", "points"}:
            _refuse("INVALID_INSPECTION_GEOMETRY")
        bbox = geometry["bbox"]
        if type(bbox) is not list or len(bbox) != 4 or any(type(n) not in (int, float) for n in bbox):
            _refuse("INVALID_INSPECTION_GEOMETRY")
        if bbox[0] > bbox[2] or bbox[1] > bbox[3]:
            _refuse("INVALID_INSPECTION_GEOMETRY")
        points = geometry["points"]
        if points is not None and (type(points) is not list or any(
                type(p) is not list or len(p) != 2 or any(type(n) not in (int, float) for n in p) for p in points)):
            _refuse("INVALID_INSPECTION_GEOMETRY")
    for store in value["stores"]:
        if type(store) is not dict or set(store) != {"key", "scope", "entity_handle", "app", "payload"}:
            _refuse("INVALID_INSPECTION_STORE")
        if type(store["scope"]) is not str or store["scope"] not in SCOPES:
            _refuse("INVALID_INSPECTION_STORE")
        if type(store["key"]) is not str or len(store["key"]) > 4096:
            _refuse("INVALID_INSPECTION_STORE")
        if store["app"] is not None and (type(store["app"]) is not str or len(store["app"]) > 4096):
            _refuse("INVALID_INSPECTION_STORE")
        handle = store["entity_handle"]
        if store["scope"] == "named-dictionary":
            if handle is not None:
                _refuse("INVALID_INSPECTION_STORE")
        elif type(handle) is not str or handle.upper() not in handles:
            _refuse("ORPHAN_INSPECTION_STORE")
        if store["scope"] == "entity-xdata" and not store["app"]:
            _refuse("INVALID_INSPECTION_STORE")
    return copy.deepcopy(value)


def parse_inspection(raw, dwg_sha256):
    """Bind the licensed read result to the source bytes supplied by the client."""
    if type(raw) not in (str, bytes) or len(raw) > MAX_INSPECTION_BYTES:
        _refuse("INSPECTION_LIMIT_EXCEEDED")
    payload = _json(raw)
    if type(payload) is not dict or payload.get("dwg_sha256") not in (None, dwg_sha256):
        _refuse("INSPECTION_SOURCE_MISMATCH")
    payload["dwg_sha256"] = dwg_sha256
    return validate_inspection(payload)


def load_adapter(adapter_path=None):
    path = adapter_path if adapter_path is not None else os.environ.get("LEAF_SOLAR_ADAPTER_FILE")
    if not path:
        _refuse("SOLAR_ADAPTER_REQUIRED")
    if not isinstance(path, (str, os.PathLike)) or len(os.fspath(path)) > 4096:
        _refuse("INVALID_SOLAR_ADAPTER_PATH")
    # A daemon and a deadline also bound slow filesystem mounts. Never read a pipe.
    done = threading.Event()
    result = []

    def read():
        try:
            target = Path(path)
            info = target.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ADAPTER_BYTES:
                raise ValueError()
            with target.open("rb") as source:
                raw = source.read(MAX_ADAPTER_BYTES + 1)
            if len(raw) > MAX_ADAPTER_BYTES:
                raise ValueError()
            result.append(raw)
        except (OSError, ValueError):
            result.append(None)
        finally:
            done.set()

    threading.Thread(target=read, daemon=True).start()
    if not done.wait(5):
        _refuse("SOLAR_ADAPTER_READ_TIMEOUT")
    if not result or result[0] is None:
        _refuse("SOLAR_ADAPTER_UNREADABLE")
    adapter = _json(result[0])
    if (type(adapter) is not dict or set(adapter) != {"schema", "version", "entity_kinds"}
            or adapter["schema"] != "leaf.solar-adapter.v1"
            or type(adapter["version"]) is not int or not 1 <= adapter["version"] <= 1000000
            or type(adapter["entity_kinds"]) is not dict):
        _refuse("INVALID_SOLAR_ADAPTER")
    for kind, rule in adapter["entity_kinds"].items():
        if kind not in {*COLLECTIONS, "project", "settings"} or type(rule) is not dict or set(rule) != {"select", "fields"}:
            _refuse("INVALID_SOLAR_ADAPTER")
        select = rule["select"]
        if (type(select) is not dict or "scope" not in select
                or set(select) - {"scope", "key", "app", "layer", "block_name"}
                or type(select["scope"]) is not str or select["scope"] not in SCOPES | {"entity"}
                or len(select) != 2 or any(type(v) is not str or not v or len(v) > 4096 for v in select.values())):
            _refuse("INVALID_SOLAR_SELECTOR")
        if select["scope"] == "entity" and not ({"layer", "block_name"} & set(select)):
            _refuse("INVALID_SOLAR_SELECTOR")
        if type(rule["fields"]) is not dict:
            _refuse("INVALID_SOLAR_FIELDS")
        for field, mapping in rule["fields"].items():
            if field in RESERVED or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,127}", field):
                _refuse("INVALID_SOLAR_FIELD")
            if (type(mapping) is not dict or set(mapping) != {"path", "status"}
                    or type(mapping["status"]) is not str
                    or mapping["status"] not in {"preserved", "derived", "not_represented"}
                    or type(mapping["path"]) is not str or len(mapping["path"]) > 512
                    or not re.fullmatch(r"(?:\$\.)?[A-Za-z_0-9]+(?:\.[A-Za-z_0-9]+)*", mapping["path"])):
                _refuse("INVALID_SOLAR_FIELD")
    if not {"project", "settings"} <= set(adapter["entity_kinds"]):
        _refuse("SOLAR_PROJECT_CONTEXT_REQUIRED")
    return adapter


def _path(context, path):
    current = context
    for key in path.removeprefix("$.").split("."):
        # Joined XRecord/XData chunks may themselves contain JSON. Decoding is
        # driven by the adapter path; the inspection keeps the raw record too.
        if type(current) is str:
            current = _json(current)
        if type(current) is dict and key in current:
            current = current[key]
        elif type(current) is list and key.isascii() and key.isdecimal() and int(key) < len(current):
            current = current[int(key)]
        else:
            _refuse("SOLAR_FIELD_NOT_FOUND")
    return copy.deepcopy(current)


def _require_intake_binding(intake, source_hash):
    _bounded_json(intake)
    source = intake.get("source") if type(intake) is dict else None
    if (type(source) is not dict or source.get("binding") == "unavailable"
            or source.get("intake_schema") != "v2"
            or type(source.get("dwg_sha256")) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", source["dwg_sha256"])
            or type(source.get("byte_length")) is not int
            or not 0 < source["byte_length"] <= 256 * 1024 * 1024):
        _refuse("SOLAR_INTAKE_BINDING_UNAVAILABLE")
    if source["dwg_sha256"] != source_hash:
        _refuse("INSPECTION_SOURCE_MISMATCH")


def import_solar_state(inspection, adapter_path=None, *, intake=_NO_INTAKE):
    """Return ``(validated_graph, handle_to_id)``. Handles stay outside graph ids.

    Direct licensed inspection callers already carry a drawing digest. Callers
    supplying intake must also prove its byte binding matches that digest.
    """
    adapter = load_adapter(adapter_path)
    inspection = validate_inspection(inspection)
    if intake is not _NO_INTAKE:
        _require_intake_binding(intake, inspection["dwg_sha256"])
    now = datetime.now(timezone.utc).isoformat()
    graph = {"graph_schema_version": 1, "rev": 0, "parent_rev": None,
             "source_hash": inspection["dwg_sha256"], "catalog_versions": {},
             "opaque_stores": {}, "orphaned_xdata": [],
             "extra": {"adapter_version": adapter["version"]}}
    graph.update({collection: [] for collection in COLLECTIONS.values()})
    handles = {}
    records = {e["handle"].upper(): e for e in inspection["entities"]}
    global_stores = [s for s in inspection["stores"] if s["entity_handle"] is None]
    for kind, rule in adapter["entity_kinds"].items():
        select = rule["select"]
        selector = next(key for key in select if key != "scope")
        candidates = [None] if kind in {"project", "settings"} else inspection["entities"]
        for entity in candidates:
            stores = global_stores if entity is None else [s for s in inspection["stores"]
                if s["entity_handle"] is not None and s["entity_handle"].upper() == entity["handle"].upper()]
            if select["scope"] == "entity":
                matches = [{}] if entity is not None and entity.get(selector) == select[selector] else []
            else:
                matches = [s for s in stores if s["scope"] == select["scope"] and
                           (entity.get(selector) if selector in {"layer", "block_name"} and entity else s.get(selector)) == select[selector]]
            if len(matches) > 1:
                _refuse("AMBIGUOUS_SOLAR_ENTITY")
            if not matches:
                continue
            raw = matches[0].get("payload", {})
            payload = _json(raw) if type(raw) is str and any(
                f["status"] != "not_represented" and f["path"] not in ("payload", "$.payload")
                and not f["path"].removeprefix("$.").startswith(("geometry.", "entity."))
                for f in rule["fields"].values()) else raw
            context = dict(payload) if type(payload) is dict else {}
            context.update({"payload": payload, "entity": entity,
                            "geometry": entity["geometry"] if entity else None})
            node = {"id": new_id(kind), "kind": kind, "rev": 0,
                    "provenance": {"created_by": "solar-import", "created_at": now,
                                   "last_writer": "solar-import", "source_rev": 0,
                                   "source_hash": inspection["dwg_sha256"], "fields": {}},
                    "validity": {"state": "unknown", "reasons": []},
                    "extra": {"stores": copy.deepcopy(stores)}}
            if entity is not None:
                handle = entity["handle"].upper()
                if handle in handles:
                    _refuse("AMBIGUOUS_SOLAR_ENTITY")
                handles[handle] = node["id"]
                node["extra"]["inspection"] = copy.deepcopy(entity)
            for field, mapping in rule["fields"].items():
                absent = mapping["status"] == "not_represented"
                derivable = kind == "string" and field in {"from_ref", "to_ref", "module_count"}
                if mapping["status"] == "derived" and not derivable:
                    _refuse("UNSUPPORTED_SOLAR_DERIVATION")
                node[field] = None if absent or derivable and mapping["status"] == "derived" else _path(context, mapping["path"])
                node["provenance"]["fields"][field] = "not-represented" if absent else mapping["status"]
            if kind in {"project", "settings"}:
                graph[kind] = node
            else:
                graph[COLLECTIONS[kind]].append(node)
    if set(handles) != set(records):
        _refuse("UNMAPPED_SOLAR_ENTITY")
    if "project" not in graph or "settings" not in graph:
        _refuse("SOLAR_PROJECT_CONTEXT_REQUIRED")
    units = graph["project"].get("units")
    if type(units) is not dict or any(units.get(k) != v for k, v in inspection["units"].items()):
        _refuse("SOLAR_UNITS_MISMATCH")

    def references(value, field=""):
        if type(value) is dict:
            return {k: references(v, k) for k, v in value.items()}
        if type(value) is list:
            return [references(v, field.removesuffix("s")) for v in value]
        if type(value) is str and (field.endswith("_ref") or field == "inverter_id"):
            if value.upper() not in handles:
                _refuse("UNMAPPED_SOLAR_REFERENCE")
            return handles[value.upper()]
        return value

    for node in [graph["project"], graph["settings"]] + [n for c in COLLECTIONS.values() for n in graph[c]]:
        for field in set(node) - RESERVED:
            node[field] = references(node[field], field)
    for string in graph["strings"]:
        fields = string["provenance"]["fields"]
        members = string.get("ordered_panel_refs")
        if type(members) is not list:
            _refuse("SOLAR_MEMBERSHIP_REQUIRED")
        for field, value, rule in (
                ("from_ref", members[0] if members else None, "polarity-from-membership"),
                ("to_ref", members[-1] if members else None, "polarity-from-membership"),
                ("module_count", len(members), "count-from-membership")):
            if fields.get(field) in {"not-represented", "derived"}:
                string[field] = value
                fields[field] = "derived:" + rule
    return validate_graph(graph), {entity["handle"]: handles[entity["handle"].upper()]
                                   for entity in inspection["entities"]}
