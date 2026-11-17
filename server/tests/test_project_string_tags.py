"""Real source TEXT splicing and mounted canonical publication, without parser stubs."""
from copy import deepcopy
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
import zlib

import pytest

import deps
import dxf_intake
import project_drawing_editor as editor
import project_drawing_write as writer
import project_string_tag_write as tags
import solar_project_context as context
from solar_design_graph import GraphValidationError
from solar_sizing_client import digest
import test_project_drawing_editor as editor_cases
import test_project_drawing_write as writer_cases
from test_lab_corpus_executor import private_pg  # noqa: F401
from test_project_drawing_editor import checkout, lane, versions  # noqa: F401
from test_w2_string_add import solved

TOOL = next(row for row in json.loads((Path(__file__).resolve().parents[1] / "write_tools.json")
            .read_text())["tools"] if row["name"] == "solar-string-tags")
BUILTIN = writer._builtin(TOOL["entry"])


def groups(*rows):
    return "".join(f"{code}\n{value}\n" for code, value in rows).encode("latin-1")


# ezdxf 1.4.3 R2010 source, preserved verbatim; no runtime ezdxf dependency.
_PACKED_SOURCE = (
    "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"
    "7lhT"
)
_REAL_SOURCE = zlib.decompress(base64.b64decode(_PACKED_SOURCE))
assert len(_REAL_SOURCE) == 15657
assert hashlib.sha256(_REAL_SOURCE).hexdigest() == "bf31b9596eaa6bc9967f87d82197031de51294d873ece34bf6ff9a7eac105ab2"


def source(unit=1):
    """Change only HEADER INSUNITS in the genuine producer source."""
    pairs, _ = writer._pairs(_REAL_SOURCE)
    index = next(i + 1 for i, p in enumerate(pairs) if p.code == 9 and p.value == "$INSUNITS")
    original = pairs[index].raw
    code_line = original.splitlines(keepends=True)[0]
    return b"".join(code_line + str(unit).encode("ascii") + b"\n" if i == index else p.raw
                    for i, p in enumerate(pairs))


def companion(raw, graph):
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="source")
    intake.update(solar_design_graph=deepcopy(graph), solar_design_graph_sha256=digest(graph),
                  project_material={"retained": ["exact", 42]})
    return intake


def prepared(graph, **params):
    return {"expected_rev": graph["rev"], "paper_mm_per_drawing_unit": .254, **params}


def records(raw, section="ENTITIES"):
    pairs, _ = writer._pairs(raw)
    return [pairs[a:b] for a, b in writer._records(pairs, writer._section(pairs, section))]


def owned(raw):
    return [r for r in records(raw) if any(p.code == 1001 and p.value == tags.APP for p in r)]


def value(record, code):
    return next(p.value for p in record if p.code == code)


NOTE = next(b"".join(p.raw for p in r) for r in records(_REAL_SOURCE) if value(r, 5) == "A1")
LINE = next(b"".join(p.raw for p in r) for r in records(_REAL_SOURCE) if value(r, 5) == "A2")
LAYER = next(b"".join(p.raw for p in r) for r in records(_REAL_SOURCE, "TABLES")
             if r[0].value == "LAYER" and value(r, 2) == "Notes")


@pytest.mark.parametrize("unit,scale", [(1, .0254), (6, 1), (0, 1)])
@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_real_builtin_splice_and_upsert(solved, unit, scale, newline):
    graph = deepcopy(solved)
    graph["project"]["units"].update(drawing_units="in" if unit == 1 else "m",
        meters_per_unit=scale, source="explicit", drawing_unit_is_feet=False)
    raw = source(unit).replace(b"\n", newline)
    intake = companion(raw, graph)
    original, original_intake = bytes(raw), deepcopy(intake)
    params = prepared(graph, paper_mm_per_drawing_unit=10 * scale)
    result, overlay = BUILTIN.run(intake, params)
    assert overlay is None and result["tag_count"] == len(graph["strings"])
    plan = result["string_tag_plan"]
    output, metadata = tags.splice_tags(raw, intake, plan)
    assert raw == original and intake == original_intake
    assert tags.splice_tags(raw, intake, plan) == (output, metadata)
    source_pairs, _ = writer._pairs(source(unit))
    unit_index = next(i for i, p in enumerate(source_pairs) if p.code == 9 and p.value == "$INSUNITS")
    unit_groups = source_pairs[unit_index].raw + source_pairs[unit_index + 1].raw
    comments = b"".join(p.raw for p in source_pairs if p.code == 999)
    assert comments and b"\xe9" in comments
    for kept in (NOTE, LINE, LAYER, unit_groups, comments):
        assert kept.replace(b"\n", newline) in output
    for section in ("TABLES", "BLOCKS", "ENTITIES", "OBJECTS"):
        for rec in records(raw, section):
            if rec[0].value == "TABLE" and value(rec, 2) in ("LAYER", "STYLE", "APPID"):
                continue  # Only these existing table counts change.
            assert b"".join(p.raw for p in rec) in output
    assert b"\r\n" in output if newline == b"\r\n" else b"\r" not in output
    text_records = owned(output)
    assert len(text_records) == len(graph["strings"])
    assert len({value(r, 5) for r in text_records}) == len(text_records)
    boxes = []
    labels = {s["id"]: s["circuit_tag"] for s in graph["strings"]}
    for rec in text_records:
        data = [p.value for p in rec if p.code == 1000]
        assert data[0] == tags.SCHEMA and value(rec, 1) == labels[data[1]]
        assert value(rec, 330) == "17" and value(rec, 7) == tags.STYLE
        assert value(rec, 72) == value(rec, 73) == value(rec, 50) == "0"
        h, x, y = (float(value(rec, c)) for c in (40, 10, 20))
        assert h * params["paper_mm_per_drawing_unit"] == pytest.approx(2)
        boxes.append((x, y, x + 1.2 * len(value(rec, 1)) * h, y + h))
    for i, (ax, ay, bx, by) in enumerate(boxes):
        for cx, cy, dx, dy in boxes[i + 1:]:
            assert bx < cx or dx < ax or by < cy or dy < ay
    table_rows = records(output, "TABLES")
    layer = next(r for r in table_rows if r[0].value == "LAYER" and value(r, 2) == plan["tag_layer"])
    assert value(layer, 70) == "0" and value(layer, 290) == "1" and int(value(layer, 62)) > 0
    zero_layer = next(r for r in table_rows if r[0].value == "LAYER" and value(r, 2) == "0")
    assert value(layer, 370) == "-3"
    assert value(layer, 390) == value(zero_layer, 390) == "13"
    assert value(layer, 390) in {value(r, 5) for r in records(output, "OBJECTS")}
    style = next(r for r in table_rows if r[0].value == "STYLE" and value(r, 2) == tags.STYLE)
    assert value(style, 3) == "txt" and value(style, 40) == value(style, 50) == "0"
    assert value(style, 41) == "1"
    merged = json.loads(tags.annotation_intake(output, intake, metadata, uuid4()))
    assert merged["solar_design_graph"] == graph
    assert merged["solar_design_graph_sha256"] == digest(graph)
    assert merged["project_material"] == intake["project_material"]
    child, _ = tags.splice_tags(output, merged, BUILTIN.run(merged, params)[0]["string_tag_plan"])
    assert len(owned(child)) == len(labels)
    assert NOTE.replace(b"\n", newline) in child
    assert not ({value(r, 5) for r in owned(child)} & {value(r, 5) for r in text_records})


def test_producer_registration_is_exact():
    assert writer.producer_entry(TOOL) == "builtins/solar_string_tags.py"
    assert writer.producer_entry({**TOOL, "entry": "authored.py"}) is None
    assert writer.producer_entry({**TOOL, "graph_input": True}) is None
    assert writer.producer_entry({**TOOL, "capabilities": ["drawing.write", "drawing.read"]}) is None
    assert "default" not in TOOL["params"]["properties"]["paper_mm_per_drawing_unit"]


@pytest.mark.parametrize("problem,code", [
    ("missing", "GRAPH_NOT_EMBEDDED"), ("digest", "GRAPH_DIGEST_MISMATCH"),
    ("native", "STRING_TAGS_NATIVE_PARENT_UNSUPPORTED"),
    ("params", "INVALID_STRING_TAGS_REQUEST"), ("scale", "INVALID_STRING_TAG_SCALE"),
    ("missing_scale", "INVALID_STRING_TAGS_REQUEST"),
    ("revision", "STALE_GRAPH_REV"),
])
def test_builtin_refuses_payload_free(solved, problem, code):
    intake, params = companion(source(), solved), prepared(solved)
    if problem == "missing":
        del intake["solar_design_graph"]
    elif problem == "digest":
        intake["solar_design_graph_sha256"] = "0" * 64
    elif problem == "native":
        intake["solar_physical_layout"] = {}
    elif problem == "params":
        params["graph"] = {"secret": "DO_NOT_LEAK"}
    elif problem == "scale":
        params["paper_mm_per_drawing_unit"] = True
    elif problem == "missing_scale":
        del params["paper_mm_per_drawing_unit"]
    else:
        params["expected_rev"] += 1
    with pytest.raises(GraphValidationError) as caught:
        BUILTIN.run(intake, params)
    # Existing graph revision codes remain owned by checked_graph.
    if problem == "revision":
        assert "REV" in caught.value.code
    else:
        assert caught.value.code == code
    assert "DO_NOT_LEAK" not in str(caught.value)


@pytest.mark.parametrize("problem", ["mismatch", "units", "unitless", "binary", "duplicate", "topology", "plan"])
def test_source_boundary_refusals(solved, problem):
    raw = source()
    intake = companion(raw, solved)
    plan = BUILTIN.run(intake, prepared(solved))[0]["string_tag_plan"]
    if problem == "mismatch":
        intake["texts"][0]["text"] = "forged"
    elif problem == "units":
        raw = source(6)
    elif problem == "unitless":
        raw = source(0)
        intake["solar_design_graph"]["project"]["units"]["source"] = "insunits"
        intake["solar_design_graph_sha256"] = digest(intake["solar_design_graph"])
    elif problem == "binary":
        raw = b"AutoCAD Binary DXF\r\n\x1a\x00"
    elif problem == "duplicate":
        raw = raw.replace(b"5\nA2\n", b"5\nA1\n")
        intake = companion(raw, solved)
    elif problem == "topology":
        raw = raw.replace(b"2\n*Model_Space\n", b"2\nOther_Space\n")
    else:
        plan["tags"][0]["text"] = "forged"
    with pytest.raises((writer.ProducerRefused, GraphValidationError)):
        tags.splice_tags(raw, intake, plan)


@pytest.mark.parametrize("problem", ["style", "layer", "lineweight", "plot_style", "marker", "reactor", "pointer", "foreign_app"])
def test_controlled_record_conflicts_refuse(solved, problem):
    intake = companion(source(), solved)
    plan = BUILTIN.run(intake, prepared(solved))[0]["string_tag_plan"]
    raw, metadata = tags.splice_tags(source(), intake, plan)
    if problem == "style":
        # Change only the new controlled style, keeping Standard untouched.
        index = raw.rfind(b"3\ntxt\n")
        raw = raw[:index] + raw[index:].replace(b"3\ntxt\n", b"3\nunsafe.ttf\n", 1)
    elif problem == "layer":
        raw = raw.replace(b"290\n1\n", b"290\n0\n")
    elif problem == "lineweight":
        raw = raw.replace(groups((370, -3), (390, "13")), groups((370, 25), (390, "13")))
    elif problem == "plot_style":
        raw = raw.replace(groups((370, -3), (390, "13")), groups((370, -3), (390, "F00")))
    elif problem == "marker":
        raw = raw.replace(tags.SCHEMA.encode(), b"forged.schema")
    elif problem in ("reactor", "foreign_app"):
        extra = groups((102, "{ACAD_REACTORS"), (330, "F00"), (102, "}")) if problem == "reactor" else groups((1001, "OTHER"), (1000, "external"))
        raw = raw.replace(groups((1001, tags.APP)), extra + groups((1001, tags.APP)), 1)
    else:
        target = next(iter(metadata["handles"].values()))
        pairs, _ = writer._pairs(raw)
        end = writer._section(pairs, "OBJECTS")[1]
        raw = (b"".join(p.raw for p in pairs[:end])
            + groups((0, "DICTIONARY"), (5, "F00"), (100, "AcDbDictionary"),
                     (3, "reference"), (350, target))
            + b"".join(p.raw for p in pairs[end:]))
    current = companion(raw, solved)
    with pytest.raises(writer.ProducerRefused):
        tags.splice_tags(raw, current, plan)


@pytest.mark.parametrize("reference", [None, "0", "F00", "ambiguous"])
def test_source_normal_plot_style_reference_refuses(solved, reference):
    raw = source()
    zero_layer = next(r for r in records(raw, "TABLES")
                      if r[0].value == "LAYER" and value(r, 2) == "0")
    original = b"".join(p.raw for p in zero_layer)
    changed = b"".join(p.raw for p in zero_layer if p.code != 390)
    if reference is not None:
        changed += groups((390, "13"), (390, "13")) if reference == "ambiguous" else groups((390, reference))
    raw = raw.replace(original, changed, 1)
    intake = companion(raw, solved)
    plan = BUILTIN.run(intake, prepared(solved))[0]["string_tag_plan"]
    with pytest.raises(writer.ProducerRefused):
        tags.splice_tags(raw, intake, plan)


@pytest.fixture(params=[1, 6, 0], ids=["inches", "metres", "explicit-unitless"])
def source_lane(request, monkeypatch, solved):
    # Seed full source topology BEFORE the genuine upload adoption in lane.
    raw = source(request.param)
    if request.node.name.split("[", 1)[0] == "test_normal_child_upsert_preserves_customer_text_on_reused_layer":
        # Only this positive reuse case gets a safe scratch layer. The exact
        # producer source retains its material pointer in every other test.
        pairs, _ = writer._pairs(raw)
        layers = [(a, b) for a, b in writer._records(pairs, writer._section(pairs, "TABLES"))
                  if pairs[a].value == "LAYER" and value(pairs[a:b], 2) == "Notes"]
        assert len(layers) == 1
        start, end = layers[0]
        assert [p.value for p in pairs[start:end] if p.code == 347] == ["21"]
        raw = b"".join(p.raw for i, p in enumerate(pairs)
                       if not (start <= i < end and p.code == 347))
    monkeypatch.setattr(editor_cases, "drawing", lambda *a, **kw: raw)
    current = request.getfixturevalue("lane")
    graph = deepcopy(solved)
    graph["project"]["units"].update(drawing_units="in" if request.param == 1 else "m",
        meters_per_unit=.0254 if request.param == 1 else 1, source="explicit", drawing_unit_is_feet=False)
    cap = checkout(current)
    # Existing server-only publication seam, with real parsing and publication;
    # no forged managed-producer state or substitute graph store.
    intake = companion(raw, graph)
    receipt = editor.save_dxf(current.tenant, current.project, current.drawing, current.parent.version_id,
        raw, request_id=uuid4(), capability=cap,
        _restored_intake=json.dumps(intake, sort_keys=True, separators=(",", ":")).encode())
    current.initial_version = current.parent.version_id
    current.parent = SimpleNamespace(version_id=UUID(receipt["drawing_version"]["version_id"]))
    current.graph, current.cap = graph, cap
    return current


@pytest.fixture
def run(source_lane, monkeypatch):
    yield from writer_cases.run.__wrapped__(source_lane, monkeypatch)


def test_normal_run_export_graph_fidelity_and_replay(source_lane, run):
    s = source_lane
    params, key = prepared(s.graph), str(uuid4())
    assert deps.find_tool(TOOL["name"], str(s.tenant))["entry"] == TOOL["entry"]
    before = context.resolve_context(s.tenant, s.project, s.parent.version_id, drawing_id=s.drawing)
    count = len(versions(s))
    response = run(TOOL, params=params, cap=s.cap, key=key)
    assert response.status_code == 200, response.text
    body = response.json()
    child = body["version"]
    assert len(versions(s)) == count + 1 and child["seq"] == 3
    assert body["producer"]["source_mutation"] == "string-tag-splice"
    exported = s.http.get(s.base + f"/versions/{child['version_id']}/dxf")
    assert exported.status_code == 200
    assert exported.headers["x-leaf-source-sha256"] == hashlib.sha256(exported.content).hexdigest()
    assert body["producer"]["output_source_sha256"] == exported.headers["x-leaf-source-sha256"]
    assert len(owned(exported.content)) == len(s.graph["strings"])
    after = context.resolve_context(s.tenant, s.project, UUID(child["version_id"]), drawing_id=s.drawing)
    assert after.intake["solar_design_graph"] == before.intake["solar_design_graph"]
    assert after.intake["solar_design_graph_sha256"] == before.intake["solar_design_graph_sha256"]
    assert after.intake["project_material"] == before.intake["project_material"]
    assert body["producer"]["output_intake_sha256"] == after.intake_sha256
    assert s.http.get(s.base + f"/versions/{s.parent.version_id}/dxf").content == s.raw
    assert body["producer"]["input_intake_sha256"] == before.intake_sha256
    assert body["producer"]["input_source_sha256"] == hashlib.sha256(s.raw).hexdigest()
    replay = run(TOOL, params=params, cap=s.cap, key=key)
    assert replay.status_code == 200 and replay.json()["version"] == child
    assert run(TOOL, params={**params, "paper_height_mm": 2.1}, cap=s.cap, key=key).status_code == 409
    # Even a newly explicit default, producing identical CAD, changes the
    # confirmation and must be fenced by the internally bound params digest.
    assert run(TOOL, params={**params, "paper_height_mm": 2.0}, cap=s.cap, key=key).status_code == 409
    assert run(TOOL, params=params, cap=s.cap).status_code == 409  # stale parent
    assert len(versions(s)) == count + 1
    released = s.http.delete(s.base + "/checkout", headers={"X-Checkout-Capability": s.cap})
    assert released.status_code == 200, released.text
    replay = run(TOOL, params=params, cap=s.cap, key=key)
    assert replay.status_code == 200 and replay.json()["version"] == child
    assert len(versions(s)) == count + 1


def test_normal_child_upsert_preserves_customer_text_on_reused_layer(source_lane, run):
    s = source_lane
    expected_note = next(b"".join(p.raw for p in r) for r in records(s.raw) if value(r, 5) == "A1")
    expected_line = next(b"".join(p.raw for p in r) for r in records(s.raw) if value(r, 5) == "A2")
    notes_layer = next(r for r in records(s.raw, "TABLES")
                       if r[0].value == "LAYER" and value(r, 2) == "Notes")
    assert not any(p.code == 347 for p in notes_layer)
    expected_layer = b"".join(p.raw for p in notes_layer)
    count = len(versions(s))
    first = run(TOOL, params=prepared(s.graph), cap=s.cap)
    assert first.status_code == 200, first.text
    child = first.json()["version"]["version_id"]
    second = run(TOOL, version=child, params=prepared(s.graph, tag_layer="Notes"), cap=s.cap)
    assert second.status_code == 200, second.text
    newer = second.json()["version"]["version_id"]
    output = s.http.get(s.base + f"/versions/{newer}/dxf")
    assert output.status_code == 200
    assert all(kept in output.content for kept in (expected_note, expected_line, expected_layer))
    for section in ("TABLES", "BLOCKS", "ENTITIES", "OBJECTS"):
        for rec in records(s.raw, section):
            if rec[0].value == "TABLE" and value(rec, 2) in ("LAYER", "STYLE", "APPID"):
                continue  # Only the required table counts may change.
            assert b"".join(p.raw for p in rec) in output.content
    parent = s.http.get(s.base + f"/versions/{s.parent.version_id}/dxf")
    assert parent.status_code == 200 and parent.content == s.raw
    assert output.headers["x-leaf-source-sha256"] == hashlib.sha256(output.content).hexdigest()
    assert len(owned(output.content)) == len(s.graph["strings"])
    assert {tuple(p.value for p in rec if p.code == 1000) for rec in owned(output.content)} == {
        (tags.SCHEMA, string["id"]) for string in s.graph["strings"]}
    assert all(value(rec, 8) == "Notes" for rec in owned(output.content))
    stored = context.resolve_context(s.tenant, s.project, UUID(newer), drawing_id=s.drawing)
    assert stored.intake["solar_design_graph"] == s.graph
    assert stored.intake["solar_design_graph_sha256"] == digest(s.graph)
    assert len(versions(s)) == count + 2


def test_normal_reused_layer_material_pointer_refuses_without_version(source_lane, run):
    s = source_lane
    assert s.raw in (source(1), source(6), source(0))
    notes_layer = next(r for r in records(s.raw, "TABLES")
                       if r[0].value == "LAYER" and value(r, 2) == "Notes")
    assert [p.value for p in notes_layer if p.code == 347] == ["21"]
    assert b"".join(p.raw for p in notes_layer) == LAYER
    count = len(versions(s))
    response = run(TOOL, params=prepared(s.graph, tag_layer="Notes"), cap=s.cap)
    assert response.status_code == 422, response.text
    assert response.json()["reason_code"] == "STRING_TAGS_LAYER_CONFLICT"
    assert len(versions(s)) == count
    parent = s.http.get(s.base + f"/versions/{s.parent.version_id}/dxf")
    assert parent.status_code == 200 and parent.content == s.raw


def test_normal_run_admission_creates_no_versions(source_lane, run):
    s = source_lane
    params, count = prepared(s.graph), len(versions(s))
    assert run(TOOL, params=params).status_code == 403
    assert run(TOOL, params=params, cap="lco1." + "0" * 64).status_code == 403
    assert run(TOOL, params=params, cap=s.cap, org=uuid4()).status_code in (403, 409)
    assert run(TOOL, params=params, cap=s.cap, version=uuid4()).status_code in (404, 409)
    assert run(TOOL, params=params, cap=s.cap, digest=False).status_code == 409
    invalid = run(TOOL, params={**params, "graph": {}}, cap=s.cap)
    assert invalid.status_code in (400, 422), invalid.text
    assert len(versions(s)) == count


def test_normal_run_missing_companion_and_native_refuse(source_lane, run):
    s = source_lane
    params, count = prepared(s.graph), len(versions(s))
    missing = run(TOOL, params=params, cap=s.cap, version=s.initial_version)
    assert missing.status_code == 422, missing.text
    assert missing.json()["reason_code"] == "GRAPH_NOT_EMBEDDED"
    intake = companion(s.raw, s.graph)
    intake["solar_physical_layout"] = {"untrusted": "DO_NOT_LEAK"}
    receipt = editor.save_dxf(s.tenant, s.project, s.drawing, s.parent.version_id, s.raw,
        request_id=uuid4(), capability=s.cap,
        _restored_intake=json.dumps(intake, sort_keys=True, separators=(",", ":")).encode())
    native = run(TOOL, params=params, cap=s.cap, version=receipt["drawing_version"]["version_id"])
    assert native.status_code == 422, native.text
    assert native.json()["reason_code"] == "STRING_TAGS_NATIVE_PARENT_UNSUPPORTED"
    assert "DO_NOT_LEAK" not in native.text
    assert len(versions(s)) == count + 1  # only the explicit server-side fixture seed
