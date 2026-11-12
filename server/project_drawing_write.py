"""Project drawing.write producer: a registered builtin's pure mutations, committed
as exactly one new immutable project drawing version.

Contract (fails closed before any durable write):
- Support comes from trusted registry metadata plus a server-owned builtin file
  listed in PRODUCERS. The tool record never chooses the code that runs.
- The builtin's pure run(intake, params) sees a deep copy of the exact stored
  input version's intake, and must remove an entity that exists, so a no-op
  never becomes a version.
- Its declared by-handle mutations are spliced into the version's ACTUAL source
  DXF bytes (splice_source); unrelated entities, tables and metadata keep their
  exact bytes. Input that cannot be changed faithfully fails closed first.
- The output is published only through
  project_drawing_editor.save_dxf, which owns project role access, checkout
  custody, the parent compare-and-set and idempotent replay. One confirmation
  (Idempotency-Key) maps to one request id, so a replay with changed params or a
  changed parent is refused by the editor's replay fence instead of publishing.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid5

import product_capability_availability as capability_catalog
import dxf_intake
import entitlements
import project_drawing_editor as drawing_editor
import solar_project_context as project
import write_loop

SCHEMA = "leaf.project-drawing-write.v1"
_SERVER = Path(__file__).resolve().parent
_BUILTINS = (_SERVER / "builtins").resolve()
# Builtins whose run(intake, params) only DECLARES removed/added entities.
# Listing a name here is the only way a write tool gains a project producer.
PRODUCERS = {"delete-marked-panel": "builtins/delete_marked_panel.py"}
_NAMESPACE = UUID("0c01d7a1-5f1e-4b6e-9a51-6f0c3b8e3c1d")
MAX_PARAMS_BYTES = 16 * 1024
MAX_IDEMPOTENCY_KEY = 128


class ProducerRefused(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def producer_entry(tool):
    """The server-owned builtin file for a trusted project write tool, else None."""
    if not isinstance(tool, dict):
        return None
    name = tool.get("name")
    entry = PRODUCERS.get(name)
    if (entry is None or tool.get("capabilities") != ["drawing.write"]
            or tool.get("kind") != "script" or tool.get("entry") != entry
            or not write_loop.is_write_tool(tool)
            or entitlements.tool_required_capability(tool) != "run_write"
            or tool.get("canonical_only") or "solar" in tool or "graph_input" in tool
            or name in capability_catalog.SOLAR_CAPABILITIES
            or capability_catalog.capability_adapter(name) is not None):
        return None
    return entry


def _builtin(entry):
    path = (_SERVER / entry).resolve()
    if path.parent != _BUILTINS or not path.is_file():
        raise ProducerRefused("PROJECT_WRITE_PRODUCER_UNAVAILABLE", 503)
    spec = importlib.util.spec_from_file_location("leaf_project_write_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "run", None)):
        raise ProducerRefused("PROJECT_WRITE_PRODUCER_UNAVAILABLE", 503)
    return module


def _handles(intake):
    return {str(row["handle"]) for key in drawing_editor._GEOMETRY
            for row in (intake.get(key) or []) if isinstance(row, dict) and row.get("handle") is not None}


_NOT_REPRESENTABLE = "PROJECT_WRITE_SOURCE_NOT_REPRESENTABLE"
# Group codes that hold a handle reference to another object.
_POINTER_CODES = frozenset(range(320, 370)) | frozenset(range(390, 400)) | {480, 481, 1005}
_VERTEX_CODES = frozenset((10, 20, 30, 40, 41, 42, 91))
_POLYLINE_KEYS = frozenset(("layer", "closed", "pts", "xdata", "handle"))
MAX_ADDED_POINTS = 1024


def _refuse_source():
    raise ProducerRefused(_NOT_REPRESENTABLE, 409)


class _Pair:
    """One DXF group: integer code, decoded value, and the exact original two lines."""
    __slots__ = ("code", "value", "raw")

    def __init__(self, code, value, raw):
        self.code, self.value, self.raw = code, value, raw


def _pairs(raw):
    if raw.startswith(b"AutoCAD Binary DXF"):
        _refuse_source()
    lines = raw.splitlines(keepends=True)
    if len(lines) % 2:
        _refuse_source()
    pairs = []
    for index in range(0, len(lines), 2):
        code = lines[index].strip()
        if not code.isdigit() or len(code) > 4:
            _refuse_source()
        pairs.append(_Pair(int(code), lines[index + 1].rstrip(b"\r\n").decode("latin-1").strip(),
                           lines[index] + lines[index + 1]))
    return pairs, (b"\r\n" if lines and lines[0].endswith(b"\r\n") else b"\n")


def _section(pairs, name):
    for index in range(len(pairs) - 1):
        if (pairs[index].code, pairs[index].value) == (0, "SECTION") and (
                pairs[index + 1].code, pairs[index + 1].value) == (2, name):
            end = next((j for j in range(index + 2, len(pairs))
                        if (pairs[j].code, pairs[j].value) == (0, "ENDSEC")), None)
            if end is None:
                _refuse_source()
            return index + 2, end
    return None


def _records(pairs, bounds):
    start, end = bounds
    starts = [j for j in range(start, end) if pairs[j].code == 0]
    return list(zip(starts, starts[1:] + [end]))


def _group(code, value, eol):
    return f"{code}".encode("ascii") + eol + value.encode("ascii") + eol


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value \
            or value in (float("inf"), float("-inf")):
        _refuse_source()
    return repr(float(value))


def _fresh_handle(pairs):
    values = [int(p.value, 16) for p in pairs if p.code in (5, 105)
              and p.value and all(c in "0123456789ABCDEFabcdef" for c in p.value)]
    header = _section(pairs, "HEADER")
    seed = None
    if header is not None:
        for j in range(*header):
            if pairs[j].code == 9 and pairs[j].value == "$HANDSEED" and j + 1 < header[1]:
                seed = j + 1
                values.append(int(pairs[seed].value, 16))
    return max(values, default=0) + 1, seed


def _geometry(intake):
    return {key: intake.get(key) or [] for key in drawing_editor._GEOMETRY}


def splice_source(source, intake, mutations):
    """Apply by-handle removals and polyline additions to the exact source bytes.

    Every byte outside the removed record, the inserted records and $HANDSEED is
    preserved in order. Anything that cannot be changed faithfully fails closed
    before a write: ownership or reactor groups, a reference to the removed
    handle from anywhere else, a non-LWPOLYLINE target, a source whose geometry
    is not the version's intake, or any mutation other than removed/added.
    """
    if set(mutations) - {"added", "removed"}:
        raise ProducerRefused("PROJECT_WRITE_MUTATION_UNSUPPORTED", 422)
    removed = [str(h).upper() for h in mutations.get("removed") or []]
    added = mutations.get("added") or []
    if len(removed) != 1 or len(added) > 1 or not all(
            isinstance(row, dict) and set(row) <= _POLYLINE_KEYS and isinstance(row.get("layer"), str)
            and row["layer"] and isinstance(row.get("pts"), list) and 2 <= len(row["pts"]) <= MAX_ADDED_POINTS
            for row in added):
        raise ProducerRefused("PROJECT_WRITE_MUTATION_UNSUPPORTED", 422)
    try:
        before = dxf_intake.parse_dxf_bytes(source, source_name="source")
    except (dxf_intake.DxfParseError, ValueError, TypeError, RecursionError):
        _refuse_source()
    if _geometry(before) != _geometry(intake):
        _refuse_source()
    pairs, eol = _pairs(source)
    entities = _section(pairs, "ENTITIES")
    if entities is None:
        _refuse_source()
    target = [(a, b) for a, b in _records(pairs, entities)
              if any(p.code == 5 and p.value.upper() == removed[0] for p in pairs[a:b])]
    if len(target) != 1:
        _refuse_source()
    a, b = target[0]
    record = pairs[a:b]
    if record[0].value != "LWPOLYLINE" or any(p.code == 102 for p in record):
        _refuse_source()
    if any(p.code in _POINTER_CODES and p.value.upper() == removed[0]
           for j, p in enumerate(pairs) if not a <= j < b):
        _refuse_source()
    next_handle, seed = _fresh_handle(pairs)
    inserted, new_handles, table_insert = [], set(), None
    for row in added:
        handle = f"{next_handle:X}"
        next_handle += 1
        new_handles.add(handle)
        head, tail, seen_vertex, has_flag = [], [], False, False
        for pair in record:
            if pair.code in _VERTEX_CODES:
                seen_vertex = True
                continue
            if pair.code >= 1000:
                continue
            if pair.code == 5:
                out = _group(5, handle, eol)
            elif pair.code == 8:
                out = _group(8, row["layer"], eol)
            elif pair.code == 90:
                out = _group(90, str(len(row["pts"])), eol)
            elif pair.code == 70:
                has_flag = True
                out = _group(70, str((int(pair.value or 0) & ~1) | (1 if row.get("closed") else 0)), eol)
            else:
                out = pair.raw
            (tail if seen_vertex else head).append(out)
        if not has_flag:
            head.append(_group(70, "1" if row.get("closed") else "0", eol))
        vertices = []
        for point in row["pts"]:
            if not isinstance(point, list) or len(point) < 2:
                _refuse_source()
            vertices += [_group(10, _number(point[0]), eol), _group(20, _number(point[1]), eol)]
        inserted.append(b"".join(head + vertices + tail))
        layer_table = _layer_table(pairs, record, row["layer"], next_handle, eol)
        if layer_table is not None:
            table_insert, next_handle = layer_table
    out = [p.raw for p in pairs]
    if seed is not None:
        out[seed] = _group(5, f"{next_handle:X}", eol)
    out[a:b] = inserted
    if table_insert is not None:
        position, raw_record = table_insert
        if position >= a:
            _refuse_source()
        out.insert(position, raw_record)
    result = b"".join(out)
    try:
        after = dxf_intake.parse_dxf_bytes(result, source_name="source")
    except (dxf_intake.DxfParseError, ValueError, TypeError, RecursionError):
        _refuse_source()
    # Fidelity control: every unrelated parsed entity survives unchanged.
    for key, rows in _geometry(before).items():
        kept = [r for r in rows if str(r.get("handle", "")).upper() not in removed]
        survived = [r for r in _geometry(after)[key]
                    if str(r.get("handle", "")).upper() not in new_handles]
        if kept != survived:
            _refuse_source()
    # Only the removed entity's own layer may vanish (a table-less DXF names layers by use).
    vanished = set(before.get("layers") or []) - set(after.get("layers") or [])
    if vanished - {p.value for p in record if p.code == 8}:
        _refuse_source()
    return result


def _layer_table(pairs, record, layer, next_handle, eol):
    """Register a new layer by cloning the removed entity's own LAYER record."""
    tables = _section(pairs, "TABLES")
    if tables is None:
        return None
    rows = _records(pairs, tables)
    in_layer, layer_rows, endtab = False, [], None
    for a, b in rows:
        kind = pairs[a].value
        if kind == "TABLE":
            in_layer = any(p.code == 2 and p.value == "LAYER" for p in pairs[a:b])
        elif kind == "ENDTAB" and in_layer:
            endtab, in_layer = a, False
        elif in_layer:
            layer_rows.append((a, b))
    if endtab is None:
        return None
    names = {p.value.casefold(): (a, b) for a, b in layer_rows for p in pairs[a:b] if p.code == 2}
    if layer.casefold() in names:
        return None
    source_layer = next((p.value for p in record if p.code == 8), "0")
    template = names.get(source_layer.casefold())
    if template is None or any(p.code == 102 or p.code >= 1000 for p in pairs[slice(*template)]):
        _refuse_source()
    clone = []
    for pair in pairs[slice(*template)]:
        if pair.code == 2:
            clone.append(_group(2, layer, eol))
        elif pair.code == 5:
            clone.append(_group(5, f"{next_handle:X}", eol))
        else:
            clone.append(pair.raw)
    return (endtab, b"".join(clone)), next_handle + 1


def request_id_for(org_id, project_id, drawing_id, idempotency_key):
    """One confirmation key is one publication request in this drawing."""
    if (not isinstance(idempotency_key, str) or not idempotency_key
            or len(idempotency_key) > MAX_IDEMPOTENCY_KEY):
        raise ProducerRefused("PROJECT_WRITE_IDEMPOTENCY_REQUIRED", 400)
    return uuid5(_NAMESPACE, f"{org_id}:{project_id}:{drawing_id}:{idempotency_key}")


def commit(tenant, project_id, drawing_id, parent_id, tool, params, *,
           catalog_digest, idempotency_key, capability):
    entry = producer_entry(tool)
    if entry is None:
        raise ProducerRefused("PROJECT_WRITE_PRODUCER_UNAVAILABLE", 409)
    if not isinstance(catalog_digest, str) or not catalog_digest:
        raise ProducerRefused("PROJECT_WRITE_CONFIRMATION_REQUIRED", 409)
    try:
        params_json = json.dumps(params, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ProducerRefused("PROJECT_WRITE_PARAMS_INVALID", 400) from None
    if not isinstance(params, dict) or len(params_json) > MAX_PARAMS_BYTES:
        raise ProducerRefused("PROJECT_WRITE_PARAMS_INVALID", 400)
    if not all(isinstance(value, UUID) for value in (project_id, drawing_id, parent_id)):
        project.refuse("INVALID_BINDING")
    # Role and tenant gate before any execution; save_dxf repeats it in its transaction.
    org_id, _actor = project._access(tenant, project_id, write=True)
    request_id = request_id_for(org_id, project_id, drawing_id, idempotency_key)
    parent = project.resolve_context(tenant, project_id, parent_id, drawing_id=drawing_id, write=True)
    try:
        result, overlay = _builtin(entry).run(deepcopy(parent.intake), deepcopy(params))
        mutations = result["mutations"]
        removed = [str(handle) for handle in mutations.get("removed") or []]
    except ProducerRefused:
        raise
    except Exception:
        raise ProducerRefused("PROJECT_WRITE_MUTATION_INVALID", 422) from None
    if not removed or not set(removed) <= _handles(parent.intake):
        raise ProducerRefused("PROJECT_WRITE_TARGET_NOT_FOUND", 422)
    # Mutate the version's ACTUAL source DXF; never regenerate it from the intake subset.
    source, source_sha256 = drawing_editor.read_dxf(tenant, project_id, drawing_id, parent_id)
    output = splice_source(source, parent.intake, mutations)
    receipt = drawing_editor.save_dxf(tenant, project_id, drawing_id, parent_id, output,
                                      request_id=request_id, capability=capability)
    producer = {
        "schema": SCHEMA, "tool": tool["name"], "tool_version": tool.get("version"),
        "entry": entry, "catalog_digest": catalog_digest,
        "params_sha256": hashlib.sha256(params_json.encode("utf-8")).hexdigest(),
        "input_version_id": str(parent_id), "input_intake_sha256": parent.intake_sha256,
        "input_source_sha256": source_sha256, "request_id": str(request_id),
        "source_mutation": "splice",
    }
    return {"result": result, "overlay": overlay, "receipt": receipt, "producer": producer}
