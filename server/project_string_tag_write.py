"""Bounded ASCII TEXT splice of exact source groups, with annotation-only proof.

Only standard R2000+ table/block topology is admitted. Existing groups remain
raw latin-1 bytes; the parser is used for fidelity checks, never serialization.
"""
from copy import deepcopy
import hashlib
import json
import math
import re

import dxf_intake
import project_drawing_editor as editor
import project_drawing_write as writer
from solar_string_tags import MAX_COORDINATE, prepare_tags

APP = STYLE = "LEAF_STRING_TAGS"
SCHEMA = "leaf.solar-string-tags.v1"
_HEX = re.compile(r"[0-9A-Fa-f]{1,16}", re.ASCII)


def fail(code="STRING_TAGS_SOURCE_TOPOLOGY_UNSUPPORTED"):
    raise writer.ProducerRefused(code, 422)


def _one(record, code, default=None):
    values = [p.value for p in record if p.code == code]
    if len(values) > 1 or (not values and default is None):
        fail()
    return values[0] if values else default


def _handle(value):
    if not _HEX.fullmatch(value) or int(value, 16) == 0:
        fail()
    return value.upper()


def _float(record, code, default=None):
    try:
        number = float(_one(record, code, default))
        if not math.isfinite(number):
            fail()
        return number
    except (ValueError, TypeError, OverflowError):
        fail()


def _parse(raw):
    try:
        return dxf_intake.parse_dxf_bytes(raw, source_name="source")
    except (ValueError, TypeError, RecursionError):
        fail("STRING_TAGS_SOURCE_INVALID")


def _finite(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _plan(intake, plan):
    # Reload the trusted builtin helper so the same companion/native checks are
    # applied independently at the byte writer boundary.
    graph = writer._builtin("builtins/solar_string_tags.py").stored_graph(intake)
    if (type(plan) is not dict or set(plan) != {"schema", "tag_layer", "paper_mm_per_drawing_unit", "tags"}
            or plan["schema"] != SCHEMA or type(plan["tags"]) is not list
            or not 1 <= len(plan["tags"]) <= 10000):
        fail("STRING_TAGS_PLAN_INVALID")
    factor = plan["paper_mm_per_drawing_unit"]
    rows = plan["tags"]
    for row in rows:
        if (type(row) is not dict or set(row) != {"string_ref", "text", "position", "height", "rotation_degrees"}
                or type(row["string_ref"]) is not str or type(row["text"]) is not str
                or type(row["position"]) is not list or len(row["position"]) != 3
                or type(row["rotation_degrees"]) not in (int, float) or row["rotation_degrees"] != 0):
            fail("STRING_TAGS_PLAN_INVALID")
        for value in [row["height"], *row["position"]]:
            if not _finite(value) or abs(value) > MAX_COORDINATE:
                fail("STRING_TAGS_PLAN_INVALID")
        if row["height"] <= 0 or row["position"][2] != 0:
            fail("STRING_TAGS_PLAN_INVALID")
        width = 1.2 * len(row["text"]) * row["height"]
        if (row["height"] != rows[0]["height"]
                or abs(row["position"][0] + width) > MAX_COORDINATE
                or abs(row["position"][1] + row["height"]) > MAX_COORDINATE):
            fail("STRING_TAGS_PLAN_INVALID")
    if not _finite(factor) or factor <= 0:
        fail("STRING_TAGS_PLAN_INVALID")
    paper_height = rows[0]["height"] * factor
    if math.isclose(paper_height, 1.8, rel_tol=1e-15):
        paper_height = max(paper_height, 1.8)
    canonical = prepare_tags(graph, {"expected_rev": graph["rev"],
        "paper_mm_per_drawing_unit": factor, "paper_height_mm": paper_height,
        "tag_layer": plan["tag_layer"]})
    if len(canonical["tags"]) != len(rows):
        fail("STRING_TAGS_PLAN_INVALID")
    for row, expected in zip(rows, canonical["tags"]):
        if (row["string_ref"] != expected["string_ref"] or row["text"] != expected["text"]
                or not math.isclose(row["height"], expected["height"], rel_tol=1e-15)
                or any(not math.isclose(a, b, rel_tol=1e-15, abs_tol=1e-300)
                       for a, b in zip(row["position"], expected["position"]))):
            fail("STRING_TAGS_PLAN_INVALID")
    return graph


def splice_tags(source, intake, plan):
    """Return exact spliced bytes and server-derived metadata, without writes."""
    graph = _plan(intake, plan)
    if (type(source) is not bytes or not source or len(source) > editor.MAX_DXF_BYTES
            or source.startswith(b"AutoCAD Binary DXF") or b"\x00" in source):
        fail("STRING_TAGS_SOURCE_INVALID")
    before = _parse(source)
    if writer._geometry(before) != writer._geometry(intake):
        fail("STRING_TAGS_SOURCE_INTAKE_MISMATCH")
    lines = source.splitlines(keepends=True)
    if (len(lines) % 2 or len(lines) > 2000000
            or any(not line.endswith(b"\n") or b"\r" in line.rstrip(b"\r\n") for line in lines)):
        fail("STRING_TAGS_SOURCE_INVALID")
    if any(not line.strip().isdigit() or len(line.strip()) > 4
           or int(line.strip()) > 1071 for line in lines[::2]):
        fail("STRING_TAGS_SOURCE_INVALID")
    pairs, eol = writer._pairs(source)
    # Unique, nonnested sections and a terminal EOF; no partial source grammar.
    sections, opened = {}, None
    for i, pair in enumerate(pairs):
        if pair.code == 0 and pair.value == "EOF":
            if opened is not None or i != len(pairs) - 1:
                fail()
        elif pair.code == 0 and pair.value == "SECTION":
            if opened is not None or i + 1 >= len(pairs) or pairs[i + 1].code != 2:
                fail()
            opened = (pairs[i + 1].value, i + 2)
            if opened[0] != opened[0].upper() or opened[0] in sections:
                fail()
        elif pair.code == 0 and pair.value == "ENDSEC":
            if opened is None:
                fail()
            sections[opened[0]] = (opened[1], i)
            opened = None
        elif pair.code == 0 and opened is None and pair.value != "EOF":
            fail()
    if opened is not None or (pairs[-1].code, pairs[-1].value) != (0, "EOF"):
        fail()
    if not {"HEADER", "TABLES", "BLOCKS", "ENTITIES"} <= sections.keys():
        fail()
    for section in ("TABLES", "BLOCKS", "ENTITIES"):
        a, b = sections[section]
        if a < b and pairs[a].code != 0:
            fail()
    header = {}
    a, b = sections["HEADER"]
    for i in range(a, b):
        if pairs[i].code == 9:
            name = pairs[i].value
            if name in header or i + 1 >= b:
                fail()
            header[name] = i + 1
    if not {"$INSUNITS", "$ACADVER", "$HANDSEED"} <= header.keys():
        fail()
    version = pairs[header["$ACADVER"]]
    if version.code != 1 or not re.fullmatch(r"AC[0-9]{4}", version.value) or int(version.value[2:]) < 1015:
        fail("STRING_TAGS_SOURCE_VERSION_UNSUPPORTED")
    units = pairs[header["$INSUNITS"]]
    scale = graph["project"]["units"]
    if (units.code != 70 or units.value not in ("0", "1", "6")
            or (units.value == "0" and scale.get("source") != "explicit")
            or (units.value in ("1", "6") and scale["meters_per_unit"] != {"1": .0254, "6": 1}[units.value])):
        fail("STRING_TAGS_SOURCE_UNITS_MISMATCH")
    seed = header["$HANDSEED"]
    if pairs[seed].code != 5:
        fail()
    next_handle = int(_handle(pairs[seed].value), 16)
    handles = set()
    for i, pair in enumerate(pairs):
        if pair.code in (5, 105) and i != seed:
            handle = _handle(pair.value)
            if handle in handles:
                fail("STRING_TAGS_DUPLICATE_HANDLE")
            handles.add(handle)
            next_handle = max(next_handle, int(handle, 16) + 1)
        elif pair.code in writer._POINTER_CODES:
            if not _HEX.fullmatch(pair.value):
                fail()
            # Do not give an existing dangling reference a new meaning.
            next_handle = max(next_handle, int(pair.value, 16) + 1)

    def fresh():
        nonlocal next_handle
        result = f"{next_handle:X}"
        next_handle += 1
        if len(result) > 16:
            fail()
        return result

    def emit(groups):
        return b"".join(writer._group(code, str(value), eol) for code, value in groups)

    tables, current = {}, None
    for a, b in writer._records(pairs, sections["TABLES"]):
        rec = pairs[a:b]
        if rec[0].value == "TABLE":
            name = _one(rec, 2).upper()
            if current is not None or name in tables:
                fail()
            _one(rec, 70)
            current = {"record": rec, "rows": {}, "count": a + next(j for j, p in enumerate(rec) if p.code == 70)}
            tables[name] = current
        elif rec[0].value == "ENDTAB":
            if current is None:
                fail()
            current["end"] = a
            current = None
        else:
            if current is None:
                fail()
            name = _one(rec, 2).casefold()
            if name in current["rows"]:
                fail()
            current["rows"][name] = rec
    if current is not None or not {"LAYER", "STYLE", "APPID", "BLOCK_RECORD", "LTYPE"} <= tables.keys():
        fail()
    for table in tables.values():
        if "end" not in table:
            fail()
        _handle(_one(table["record"], 5))
        if _float(table["record"], 70) != len(table["rows"]):
            fail()
    model = tables["BLOCK_RECORD"]["rows"].get("*model_space")
    if (model is None or model[0].value != "BLOCK_RECORD"
            or _handle(_one(model, 330)) != _handle(_one(tables["BLOCK_RECORD"]["record"], 5))):
        fail()
    continuous = tables["LTYPE"]["rows"].get("continuous")
    if (continuous is None or continuous[0].value != "LTYPE"
            or _float(continuous, 70) != 0 or _float(continuous, 73) != 0 or _float(continuous, 40) != 0):
        fail("STRING_TAGS_LINETYPE_CONFLICT")
    owner = _handle(_one(model, 5))
    model_blocks = []
    block = None
    for a, b in writer._records(pairs, sections["BLOCKS"]):
        rec = pairs[a:b]
        if rec[0].value == "BLOCK":
            if block is not None:
                fail()
            block = rec
        elif rec[0].value == "ENDBLK":
            if block is None:
                fail()
            if _one(block, 2).casefold() == "*model_space":
                if (_handle(_one(block, 330)) != owner or _handle(_one(rec, 330)) != owner
                        or _one(block, 3).casefold() != "*model_space"):
                    fail()
                model_blocks.append(block)
            block = None
        elif block is None or _one(block, 2).casefold() == "*model_space":
            fail()
    if block is not None or len(model_blocks) != 1:
        fail()

    replacements, inserts = {}, {}

    # Reuse the source's genuine normal plot style, never a guessed handle.
    zero_layer = tables["LAYER"]["rows"].get("0")
    if zero_layer is None or zero_layer[0].value != "LAYER":
        fail("STRING_TAGS_LAYER_CONFLICT")
    normal_style = _handle(_one(zero_layer, 390))
    object_handles = set()
    if "OBJECTS" in sections:
        for a, b in writer._records(pairs, sections["OBJECTS"]):
            object_handles.add(_handle(_one(pairs[a:b], 5)))
    if normal_style not in object_handles:
        fail("STRING_TAGS_LAYER_CONFLICT")

    def table_record(name, symbol, fields, check):
        table = tables[name]
        subclass = {"LAYER": "AcDbLayerTableRecord", "STYLE": "AcDbTextStyleTableRecord",
                    "APPID": "AcDbRegAppTableRecord"}[name]
        existing = table["rows"].get(symbol.casefold())
        if existing is not None:
            if (existing[0].value != name or _one(existing, 2) != symbol
                    or any(p.code == 102 or p.code >= 1000 for p in existing)
                    or [p.value for p in existing if p.code == 100] != ["AcDbSymbolTableRecord", subclass]
                    or _handle(_one(existing, 330)) != _handle(_one(table["record"], 5))
                    or any(p.code in writer._POINTER_CODES - ({330, 390} if name == "LAYER" else {330})
                           for p in existing)
                    or not check(existing)):
                fail("STRING_TAGS_" + name + "_CONFLICT")
            return
        handle = fresh()
        raw = emit([(0, name), (5, handle), (330, _one(table["record"], 5)),
                    (100, "AcDbSymbolTableRecord"),
                    (100, subclass), (2, symbol), *fields])
        inserts[table["end"]] = raw
        replacements[table["count"]] = emit([(70, len(table["rows"]) + 1)])

    table_record("LAYER", plan["tag_layer"], [(70, 0), (62, 7), (6, "Continuous"), (290, 1),
                 (370, -3), (390, normal_style)],
                 lambda r: _float(r, 70) == 0 and 1 <= _float(r, 62) <= 255
                 and _one(r, 6).casefold() == "continuous" and _float(r, 290, "1") == 1
                 and _float(r, 370) in (-3, -1, 0)
                 and _handle(_one(r, 390)) == normal_style
                 and not any(p.code in (420, 430, 440, 347) for p in r))
    table_record("STYLE", STYLE, [(70, 0), (40, 0), (41, 1), (50, 0), (71, 0), (42, 2), (3, "txt"), (4, "")],
                 lambda r: _float(r, 70) == 0 and _float(r, 40) == 0 and _float(r, 41) == 1
                 and _float(r, 50) == 0 and _float(r, 71) == 0 and _one(r, 3).lower() == "txt"
                 and _one(r, 4, "") == "")
    table_record("APPID", APP, [(70, 0)], lambda r: _float(r, 70) == 0)

    removed, ranges, owned_refs = set(), {}, set()
    for a, b in writer._records(pairs, sections["ENTITIES"]):
        rec = pairs[a:b]
        if not any(p.code == 1001 and p.value.casefold() == APP.casefold() for p in rec):
            continue
        # A partial/forged ownership marker is a refusal, never permission to
        # remove an arbitrary customer entity or silently create duplicate tags.
        xdata = [(p.code, p.value) for p in rec if p.code >= 1000]
        if (rec[0].value != "TEXT" or len(xdata) != 3 or xdata[:2] != [(1001, APP), (1000, SCHEMA)]
                or xdata[2][0] != 1000
                or not re.fullmatch(r"leaf:string:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", xdata[2][1])
                or xdata[2][1] in owned_refs
                or any(p.code == 102 or p.code in writer._POINTER_CODES - {330} for p in rec)
                or _handle(_one(rec, 330)) != owner or _one(rec, 7) != STYLE
                or _one(rec, 8).casefold() not in tables["LAYER"]["rows"]
                or _float(rec, 40) <= 0 or _float(rec, 50) != 0
                or _float(rec, 41) != 1 or _float(rec, 51) != 0 or _float(rec, 71) != 0
                or _float(rec, 72) != 0 or _float(rec, 73) != 0
                or any(p.code in (60, 67, 410, 210, 220, 230) for p in rec)):
            fail("STRING_TAGS_OWNERSHIP_CONFLICT")
        owned_refs.add(xdata[2][1])
        handle = _handle(_one(rec, 5))
        removed.add(handle)
        ranges[a] = b
    for i, pair in enumerate(pairs):
        if pair.code in writer._POINTER_CODES and pair.value.upper() in removed:
            if not any(a <= i < b for a, b in ranges.items()):
                fail("STRING_TAGS_REFERENCED_TAG")
    new_handles, texts = {}, []
    for row in plan["tags"]:
        handle = fresh()
        new_handles[row["string_ref"]] = handle
        x, y, z = row["position"]
        texts.append(emit([(0, "TEXT"), (5, handle), (330, owner), (100, "AcDbEntity"),
            (8, plan["tag_layer"]), (100, "AcDbText"), (10, repr(float(x))), (20, repr(float(y))),
            (30, repr(float(z))), (40, repr(float(row["height"]))), (1, row["text"]),
            (50, 0), (41, 1), (51, 0), (7, STYLE), (71, 0), (72, 0),
            (100, "AcDbText"), (73, 0), (1001, APP), (1000, SCHEMA), (1000, row["string_ref"])]))
    inserts[sections["ENTITIES"][1]] = b"".join(texts)
    if len(f"{next_handle:X}") > 16:
        fail()
    replacements[seed] = emit([(5, f"{next_handle:X}")])
    output, i = [], 0
    while i < len(pairs):
        if i in inserts:
            output.append(inserts[i])
        if i in ranges:
            i = ranges[i]
            continue
        output.append(replacements.get(i, pairs[i].raw))
        i += 1
    raw = b"".join(output)
    if len(raw) > editor.MAX_DXF_BYTES:
        fail("STRING_TAGS_OUTPUT_TOO_LARGE")
    after = _parse(raw)
    fresh_handles = set(new_handles.values())
    for key, rows in writer._geometry(before).items():
        if isinstance(rows, dict):
            if rows != writer._geometry(after)[key]:
                fail("STRING_TAGS_SOURCE_FIDELITY_FAILED")
            continue
        kept = [r for r in rows if str(r.get("handle", "")).upper() not in removed]
        actual = [r for r in writer._geometry(after)[key] if str(r.get("handle", "")).upper() not in fresh_handles]
        if kept != actual:
            fail("STRING_TAGS_SOURCE_FIDELITY_FAILED")
    emitted = {r["handle"].upper(): r for r in after.get("texts", []) if r["handle"].upper() in fresh_handles}
    if len(emitted) != len(plan["tags"]):
        fail("STRING_TAGS_TEXT_VERIFICATION_FAILED")
    for row in plan["tags"]:
        actual = emitted[new_handles[row["string_ref"]]]
        if (actual["text"] != " ".join(row["text"].split()) or actual["layer"] != plan["tag_layer"]
                or actual["pt"] != row["position"][:2]):
            fail("STRING_TAGS_TEXT_VERIFICATION_FAILED")
    output_pairs, _ = writer._pairs(raw)
    output_entities = writer._section(output_pairs, "ENTITIES")
    by_handle = {handle: row for row in plan["tags"] for handle in [new_handles[row["string_ref"]]]}
    for a, b in writer._records(output_pairs, output_entities):
        rec = output_pairs[a:b]
        handle = _one(rec, 5, "").upper()
        if handle not in by_handle:
            continue
        row = by_handle[handle]
        text_group = next(p for p in rec if p.code == 1)
        if (text_group.raw.splitlines()[1].decode("ascii") != row["text"]
                or _float(rec, 40) != row["height"] or _float(rec, 50) != 0
                or _float(rec, 72) != 0 or _float(rec, 73) != 0
                or _one(rec, 330) != owner or _one(rec, 7) != STYLE):
            fail("STRING_TAGS_TEXT_VERIFICATION_FAILED")
    metadata = {"schema": SCHEMA, "plan": deepcopy(plan), "handles": new_handles,
        "input_source_sha256": hashlib.sha256(source).hexdigest(),
        "output_source_sha256": hashlib.sha256(raw).hexdigest(),
        "graph_sha256": intake["solar_design_graph_sha256"]}
    return raw, metadata


def annotation_intake(output, intake, metadata, drawing_id):
    """Merge verified new CAD fields with exact unchanged project companions."""
    parsed = json.loads(editor._parse(output, drawing_id))
    merged = {key: deepcopy(value) for key, value in intake.items() if key not in editor._DXF_FIELDS}
    merged.update(parsed)
    merged["leaf_string_tags"] = deepcopy(metadata)
    return json.dumps(merged, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
