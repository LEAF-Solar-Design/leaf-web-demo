"""Lossless offline inspection for the Solar Rooftop write boundary.

V1 read-only import remains in solar_interchange. V2 stores carry their own
ordered native records; payload_ref is a content address, never a fetch URL.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import re
import threading
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

try:
    from .solar_design_graph import GraphValidationError, _bounded_json
except ImportError:
    from solar_design_graph import GraphValidationError, _bounded_json


MAX_BYTES = 16 * 1024 * 1024  # Existing graph transport is lower than 64 MiB.
UNIT_SCALES = {"m": 1.0, "mm": .001, "cm": .01, "km": 1000.0,
               "in": .0254, "ft": .3048, "yd": .9144}
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_HANDLE = re.compile(r"[0-9A-Fa-f]{1,32}\Z")


def refuse(code):
    raise GraphValidationError(code)


def canonical_bytes(value):
    _bounded_json(value)
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                     separators=(",", ":")).encode("ascii")
    if len(raw) > MAX_BYTES:
        refuse("SOLAR_TRANSPORT_LIMIT_EXCEEDED")
    return raw


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def parse_json(raw):
    if type(raw) not in (str, bytes) or len(raw) > MAX_BYTES:
        refuse("SOLAR_TRANSPORT_LIMIT_EXCEEDED")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                refuse("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, GraphValidationError):
            raise
        refuse("INVALID_SOLAR_JSON")
    _bounded_json(value)
    return value


@lru_cache(maxsize=2)
def load_schema(kind="inspection"):
    """Bound local schema reads, including on slow mounted workspaces."""
    filenames = {"inspection": "solar-inspection.v2.schema.json",
                 "apply": "solar-apply.v1.schema.json"}
    if kind not in filenames:
        refuse("UNKNOWN_SOLAR_SCHEMA")
    result = []
    done = threading.Event()

    def read():
        try:
            path = Path(__file__).resolve().parents[1] / "contract" / filenames[kind]
            with path.open("rb") as source:
                result.append(source.read(MAX_BYTES + 1))
        except OSError:
            result.append(None)
        finally:
            done.set()

    threading.Thread(target=read, daemon=True).start()
    if not done.wait(5):
        refuse("SOLAR_SCHEMA_READ_TIMEOUT")
    if not result or result[0] is None:
        refuse("SOLAR_SCHEMA_UNREADABLE")
    return parse_json(result[0])


def validate_schema(value, kind):
    canonical_bytes(value)
    if not Draft202012Validator(load_schema(kind)).is_valid(value):
        refuse("INVALID_SOLAR_" + kind.upper() + "_SCHEMA")


def validate_units(units):
    if (type(units) is not dict or units.get("drawing_units") not in UNIT_SCALES
            or type(units.get("meters_per_unit")) not in (int, float)
            or not math.isfinite(units["meters_per_unit"])
            or abs(units["meters_per_unit"] - UNIT_SCALES[units["drawing_units"]]) > 1e-12):
        refuse("UNKNOWN_UNITS")


def normalize_handle(value):
    if type(value) is not str or not _HANDLE.fullmatch(value):
        refuse("INVALID_NATIVE_HANDLE")
    return value.upper()


def validate_records(records):
    if type(records) is not list or len(records) > 100000:
        refuse("INVALID_RAW_RECORDS")
    for record in records:
        if type(record) is not dict or set(record) != {"code", "type", "value"}:
            refuse("INVALID_RAW_RECORD")
        code, kind, value = record["code"], record["type"], record["value"]
        if type(code) is not int or not 0 <= code <= 32767:
            refuse("INVALID_RECORD_CODE")
        if kind in {"int16", "int32", "int64"}:
            bits = int(kind[3:])
            if type(value) is not int or not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
                refuse("INVALID_TYPED_INTEGER")
        elif kind == "double":
            if type(value) not in (int, float) or not math.isfinite(value):
                refuse("INVALID_TYPED_DOUBLE")
        elif kind == "point3d":
            if (type(value) is not list or len(value) != 3 or
                    any(type(v) not in (int, float) or not math.isfinite(v) for v in value)):
                refuse("INVALID_TYPED_POINT")
        elif kind in {"string", "handle", "binary"}:
            if type(value) is not str:
                refuse("INVALID_TYPED_STRING")
            if kind == "handle":
                normalize_handle(value)
            if kind == "binary":
                try:
                    decoded = base64.b64decode(value, validate=True)
                    if base64.b64encode(decoded).decode("ascii") != value:
                        refuse("INVALID_TYPED_BINARY")
                except (ValueError, UnicodeError):
                    refuse("INVALID_TYPED_BINARY")
        else:
            refuse("UNKNOWN_RECORD_TYPE")
    _bounded_json(records)


def raw_record_bytes(records):
    """Version 1: typed ordered JSON, with doubles represented by IEEE hex.

    The record type, code, string spelling and binary base64 survive unchanged.
    A double's hex form preserves signed zero and distinguishes it from integers.
    This is a native-record envelope, never a reserialization of store JSON.
    """
    validate_records(records)
    result = copy.deepcopy(records)
    for record in result:
        if record["type"] == "double":
            record["value"] = float(record["value"]).hex()
        elif record["type"] == "point3d":
            record["value"] = [float(v).hex() for v in record["value"]]
    return canonical_bytes({"schema": "leaf.native-records.v1", "records": result})


def store_evidence(records):
    raw = raw_record_bytes(records)
    sha = hashlib.sha256(raw).hexdigest()
    return {"payload_ref": "sha256:" + sha, "sha256": sha, "byte_length": len(raw)}


def entity_digest(entity):
    return digest({key: value for key, value in entity.items() if key != "sha256"})


def validate_geometry(geometry):
    kind = geometry["kind"]
    if kind == "polyline":
        count = len(geometry["vertices"])
        if any(len(geometry[k]) != count for k in ("bulges", "start_widths", "end_widths")):
            refuse("POLYLINE_SEGMENT_COUNT_MISMATCH")
    if kind in {"polyline", "block_reference"} and not any(geometry["normal"]):
        refuse("INVALID_NATIVE_NORMAL")
    if kind == "block_reference":
        if any(v == 0 for v in geometry["scale"]):
            refuse("INVALID_BLOCK_SCALE")
        definition = geometry["definition"]
        if definition["mode"] == "new":
            for polyline in definition["polylines"]:
                validate_geometry(polyline)
            tags = [a["tag"] for a in definition["attribute_definitions"]]
            if len(set(tags)) != len(tags):
                refuse("DUPLICATE_ATTRIBUTE_TAG")
    if kind == "table":
        rows, cols = len(geometry["rows"]) + 2, len(geometry["headers"])
        if (any(len(row) != cols for row in geometry["rows"])
                or len(geometry["column_widths"]) != cols
                or len(geometry["row_heights"]) != rows
                or len(geometry["text_heights"]) != rows):
            refuse("TABLE_DIMENSION_MISMATCH")


def store_address(store):
    handle = store["entity_handle"]
    return (store["scope"], tuple(store["dictionary_path"]), store["key"],
            store["app"], normalize_handle(handle) if handle else None,
            store.get("entity_create_ref"))


def validate_inspection(value):
    if type(value) is dict and value.get("schema") == "leaf.solar-inspection.v1":
        refuse("WRITE_REQUIRES_INSPECTION_V2")
    validate_schema(value, "inspection")
    validate_units(value["units"])
    handles = set()
    for entity in value["entities"]:
        handle = normalize_handle(entity["handle"])
        if handle in handles:
            refuse("DUPLICATE_NATIVE_HANDLE")
        handles.add(handle)
        if entity["kind"] != entity["native_geometry"]["kind"]:
            refuse("NATIVE_KIND_MISMATCH")
        validate_geometry(entity["native_geometry"])
        if entity["sha256"] != entity_digest(entity):
            refuse("ENTITY_HASH_MISMATCH")
        bbox = entity["geometry"]["bbox"]
        if bbox[0] > bbox[2] or bbox[1] > bbox[3]:
            refuse("INVALID_INSPECTION_BBOX")
        tags = [a["tag"] for a in entity["attributes"]]
        if len(set(tags)) != len(tags):
            refuse("DUPLICATE_ATTRIBUTE_TAG")
    addresses = set()
    for store in value["stores"]:
        address = store_address(store)
        if address in addresses:
            refuse("DUPLICATE_STORE_ADDRESS")
        addresses.add(address)
        if store["entity_handle"] and normalize_handle(store["entity_handle"]) not in handles:
            refuse("ORPHAN_INSPECTION_STORE")
        evidence = store_evidence(store["raw_records"])
        if any(store[k] != v for k, v in evidence.items()):
            refuse("PAYLOAD_EVIDENCE_MISMATCH")
    return copy.deepcopy(value)


def parse_inspection(raw, dwg_sha256, byte_length):
    value = validate_inspection(parse_json(raw))
    if (type(byte_length) is not int or value["dwg_sha256"] != dwg_sha256
            or value["byte_length"] != byte_length):
        refuse("INSPECTION_SOURCE_MISMATCH")
    return value
