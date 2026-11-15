"""Native metadata and raw-source conservation, independent of native execution."""
import copy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

import intake_dxf
import solar_frames_piles as fp
import solar_ground_frames as kernels
import solar_native_layout_dxf as native
import solar_physical_state as ps

SCOPE = {"organization_id": UUID("00000000-0000-4000-8000-000000000001"),
         "project_id": UUID("00000000-0000-4000-8000-000000000002"),
         "drawing_id": UUID("00000000-0000-4000-8000-000000000003")}


def pairs(raw):
    """Test-local tokenizer: does not use the production tokenizer or decoder."""
    lines = raw.decode("utf-8").splitlines()
    assert len(lines) % 2 == 0
    return [(int(code.strip()), value) for code, value in zip(lines[::2], lines[1::2])]


def sections(raw):
    data = pairs(raw)
    out = {}
    i = 0
    while data[i] != (0, "EOF"):
        assert data[i] == (0, "SECTION") and data[i + 1][0] == 2
        name = data[i + 1][1]
        assert name not in out
        end = data.index((0, "ENDSEC"), i + 2)
        out[name] = data[i + 2:end]
        i = end + 1
    assert i == len(data) - 1
    return out


def records(data):
    result = []
    for pair in data:
        if pair[0] == 0:
            result.append([])
        result[-1].append(pair)
    return result


def get(record, code, default=None):
    return next((v for c, v in record if c == code), default)


def emit(data):
    return "".join(f"{c}\n{v}\n" for c, v in data).encode()


def insert(raw, section, data):
    marker = f"0\nSECTION\n2\n{section}\n".encode()
    start = raw.index(marker) + len(marker)
    return raw[:start] + data + raw[start:]


@pytest.fixture
def generated():
    fixture = json.loads((ROOT / "docs/parity/evidence/ground/generate/intake.json").read_text(encoding="utf-8"))
    document, parameters = fp.prepare_initial_frames(boundary=[[0, 0], [20, 0], [20, 40], [0, 40]],
        preset=fixture["active_preset"], drawing_units="m")
    assert document["state"]["frames"]
    return document, parameters, fixture


def write(document, parameters, source=None):
    return native.materialize_native_layout(SCOPE, None if source is None else {"index": 1},
                                           document, source, parameters=parameters)


def piled(generated):
    document, parameters, fixture = copy.deepcopy(generated)
    preset, store = fp.load_preset(fixture["active_preset"])
    pile_store = fp.load_pile_store(fixture["pile_template"], preset, store)
    result = kernels.run_piling(document["state"]["frames"], preset, pile_store, 1.0,
                                terrain_z_m=lambda x, y: 12.25)
    assert result["entities"]
    document["state"]["piles"] = result["entities"]
    parameters["piling"] = {"preset": fixture["active_preset"], "pile_template": fixture["pile_template"]}
    parameters["operation"] = {"operation": "piling-generate", "base": "a" * 64,
                               **parameters["piling"]}
    return document, parameters


def test_initial_real_kernel_geometry_metadata_and_envelope(generated):
    document, parameters, _ = generated
    # Exercise explicit color, saturated cells and the optional revision independently.
    frame = document["state"]["frames"][0]
    frame.update(color={"method": "ByAci", "index": 17}, row=40000, col=50000,
                 site_revision="site-revision-1", elevation=9.125)
    document["state"]["frames"][1]["color"] = {"method": "ByLayer", "index": 256}
    raw = write(document, parameters)
    assert raw == write(document, parameters)
    sec = sections(raw)
    assert (1, "AC1015") in sec["HEADER"]
    ents = records(sec["ENTITIES"])
    assert len(ents) == len(document["state"]["frames"])
    ent = ents[0]
    assert get(ent, 0) == "LWPOLYLINE" and get(ent, 8) == "LEAF-TRACKERS"
    assert get(ent, 62) == "17" and get(ent, 90) == "4" and get(ent, 70) == "1"
    assert float(get(ent, 38)) == frame["elevation"]
    assert [(float(x), float(y)) for x, y in zip([v for c, v in ent if c == 10],
                                                               [v for c, v in ent if c == 20])] == frame["vertices"]
    assert ent[-4:] == [(1001, "LEAFFRAMECELL"), (1070, "32767"), (1070, "32767"), (1000, "site-revision-1")]
    assert all(get(e, 62) is None for e, f in zip(ents[1:], document["state"]["frames"][1:])
               if f["color"]["method"] == "ByLayer")
    table = records(sec["TABLES"])
    for name, aci in (("LEAF-TRACKERS", "3"), ("LEAF-PILING", "1")):
        layer = next(r for r in table if get(r, 0) == "LAYER" and get(r, 2) == name)
        assert get(layer, 62) == aci and get(layer, 370) == "-3" and get(layer, 390)
    assert {get(r, 2) for r in table if get(r, 0) == "APPID"} >= {"LEAFFRAMECELL", "LEAFPILING"}
    defpoints = next(r for r in table if get(r, 0) == "LAYER" and get(r, 2).lower() == "defpoints")
    assert get(defpoints, 290) == "0"
    assert any(get(r, 0) == "ACDBDICTIONARYWDFLT" for r in records(sec["OBJECTS"]))
    assert (3, "ACAD_GROUP") in sec["OBJECTS"]
    decoded = native.decode_native_layout(raw)
    assert decoded.document == document and decoded.parameters == parameters
    assert decoded.scope == {k: str(v) for k, v in SCOPE.items()}
    assert decoded.xrecord["document"] == ps.encode_document(document).decode()
    assert decoded.xrecord["document_sha256"] == hashlib.sha256(ps.encode_document(document)).hexdigest()
    canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert decoded.xrecord["parameters_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert decoded.handles[frame["handle"]] == get(ent, 5)
    empty = sections(intake_dxf.intake_to_dxf({"layers": ["0", "Defpoints"]}))["HEADER"]
    seed = int(next(v for c, v in empty if c == 5), 16)
    assert all(int(h, 16) > seed for h in decoded.handles.values())
    import project_drawing_editor as editor
    assert json.loads(editor._parse(raw, SCOPE["drawing_id"]))["polylines"]


def test_successor_piles_all_ten_tags_and_second_replacement(generated):
    initial = write(generated[0], generated[1])
    document, parameters = piled(generated)
    raw = write(document, parameters, initial)
    assert raw == write(document, parameters, initial)
    decoded = native.decode_native_layout(raw)
    assert decoded.document == document and decoded.parameters == parameters
    ents = records(sections(raw)["ENTITIES"])
    circles = [r for r in ents if get(r, 0) == "CIRCLE"]
    assert len(circles) == len(document["state"]["piles"])
    for actual, expected, back in zip(circles, document["state"]["piles"], decoded.piles):
        assert get(actual, 8) == "LEAF-PILING" and get(actual, 1001) == "LEAFPILING"
        assert float(get(actual, 39)) == expected["thickness"]
        assert tuple(float(get(actual, c)) for c in (10, 20, 30)) == expected["center"]
        assert float(get(actual, 40)) == expected["radius"]
        assert [get(actual, c) for c in (210, 220, 230)] == ["0", "0", "1"]
        tags = [v for c, v in actual if c == 1000]
        record = expected["pile"]
        assert len(tags) == 10 and tags[:2] == [record["placement"], record["placement_detail"]]
        assert tags[2] == "SourceTracker=" + decoded.handles[record["source_tracker"]]
        for index, key, prefix in ((3, "template", "Template="), (4, "mapping", "Mapping="),
                                    (5, "station_label", "StationLabel=")):
            assert tags[index] == (prefix + record[key] if record[key] else "")
        for index, key, prefix in ((6, "diameter_m", "DiameterM="), (7, "reveal_m", "RevealM="),
                                    (8, "embedment_m", "EmbedmentM=")):
            assert tags[index] == prefix + native.format_dotnet(record[key])
        assert tags[9] == "DepthM=" + native.format_dotnet(record["reveal_m"] + record["embedment_m"])
        assert back["tags"] == tuple(tags)
    again = write(document, parameters, raw)
    sec = sections(again)
    assert len(records(sec["ENTITIES"])) == len(ents)
    assert sec["OBJECTS"].count((3, native.NAMESPACE)) == 1
    assert sum(get(r, 0) == "XRECORD" and (1, native.SCHEMA) in r
               for r in records(sec["OBJECTS"])) == 1
    assert set(decoded.handles.values()).isdisjoint(native.decode_native_layout(again).handles.values())


def foreign_source():
    raw = intake_dxf.intake_to_dxf({"layers": ["0", "Defpoints", "Foreign"]})
    root = next(r for r in records(sections(raw)["OBJECTS"])
                if get(r, 0) == "DICTIONARY" and get(r, 330) == "0")
    foreign_entities = (b"  0\r\nTEXT\r\n  5\r\nF000\r\n100\r\nAcDbEntity\r\n8\r\nForeign\r\n"
        b"100\r\nAcDbText\r\n10\r\n1.00\r\n20\r\n2.00\r\n30\r\n0\r\n40\r\n2.5\r\n1\r\nKeep exact text\r\n100\r\nAcDbText\r\n"
        b"0\nLINE\n5\nF001\n100\nAcDbEntity\n8\nLEAF-LBD\n100\nAcDbLine\n10\n0\n20\n0\n30\n0\n11\n4\n21\n5\n31\n0\n1001\nFOREIGN_APP\n1000\nkeep-this-tag\n")
    foreign_object = emit([(0, "XRECORD"), (5, "F002"), (330, get(root, 5)),
                           (100, "AcDbXrecord"), (280, "1"), (1, "unrelated object"), (90, "42")])
    raw = insert(raw, "ENTITIES", foreign_entities)
    raw = insert(raw, "OBJECTS", foreign_object)
    return raw, foreign_entities, foreign_object


def test_ordinary_source_and_foreign_records_preserved_byte_for_byte(generated):
    source, foreign_entities, foreign_object = foreign_source()
    document, parameters = piled(generated)
    raw = write(document, parameters, source)
    assert foreign_entities in raw and foreign_object in raw
    # Every original pair remains in order and byte-identical except the explicitly
    # mutable seed/count values. This checks all foreign envelope data as well.
    def retained(raw):
        lines = raw.splitlines(keepends=True)
        return [lines[i] + lines[i + 1] for i in range(0, len(lines), 2)]
    original = retained(source)
    output = retained(raw)
    mutable = {b"5\n" + sections(source)["HEADER"][3][1].encode() + b"\n"}
    # Match by pair position, allowing only seed and APPID/LAYER count replacement.
    drawing = sections(source)
    seed = next(v for i, (c, v) in enumerate(drawing["HEADER"])
                if c == 5 and drawing["HEADER"][i - 1] == (9, "$HANDSEED"))
    mutable.add(emit([(5, seed)]))
    for row in records(drawing["TABLES"]):
        if get(row, 0) == "TABLE" and get(row, 2) in ("APPID", "LAYER"):
            mutable.add(emit([(70, get(row, 70))]))
    cursor = 0
    for pair in original:
        if pair in mutable:
            continue
        assert pair in output[cursor:]
        cursor = output.index(pair, cursor) + 1
    assert native.decode_native_layout(raw).document == document
    again = write(document, parameters, raw)
    assert foreign_entities in again and foreign_object in again


@pytest.mark.parametrize("number, expected", [(0, "0"), (-12.125, "-12.125"), (1e-9, "0"),
    (-1e-9, "0"), (12345678.123456789, "12345678.1234568"),
    (0.000000005, "0.00000001"), (-0.000000005, "-0.00000001"), (10, "10")])
def test_dotnet_custom_format_reference_values(number, expected):
    assert native.format_dotnet(number) == expected


def test_unicode_and_literal_dxf_escape_roundtrip(generated):
    document, parameters, _ = generated
    document["state"]["frames"][0]["site_revision"] = "site-\u00e9-\U0001f31e"
    parameters["literal"] = r"a\U+0041"
    raw = write(document, parameters)
    raw.decode("ascii")
    decoded = native.decode_native_layout(raw)
    assert decoded.document == document and decoded.parameters == parameters
    assert decoded.frames[0]["site_revision"] == "site-\u00e9-\U0001f31e"
    assert native.decode_native_layout(write(document, parameters, raw)).parameters == parameters


def refuse(code, function, *args, **kwargs):
    with pytest.raises(native.NativeLayoutError) as error:
        function(*args, **kwargs)
    assert error.value.code == "NATIVE_LAYOUT_" + code


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 1e15 + 1])
def test_numeric_refusals(generated, value):
    document, parameters, _ = generated
    document["state"]["frames"][0]["elevation"] = value
    refuse("NUMBER_INVALID", write, document, parameters)


@pytest.mark.parametrize("value", ["x" * 256, "newline\n", "null\x00", "tab\t"])
def test_string_refusals(generated, value):
    document, parameters, _ = generated
    document["state"]["frames"][0]["site_revision"] = value
    refuse("STRING_INVALID", write, document, parameters)


@pytest.mark.parametrize("key", ["tracker_rows", "trenches", "grade_pads", "settings"])
def test_unsupported_state_is_not_dropped(generated, key):
    document, parameters, _ = generated
    document["state"][key] = [{}] if key != "settings" else {}
    refuse("STATE_UNSUPPORTED", write, document, parameters)


def test_insert_and_unknown_shapes_refused(generated):
    document, parameters, _ = generated
    original = document["state"]["frames"][0]["type"]
    document["state"]["frames"][0]["type"] = "INSERT"
    refuse("FRAME_UNSUPPORTED", write, document, parameters)
    document["state"]["frames"][0]["type"] = "LINE"
    refuse("FRAME_UNSUPPORTED", write, document, parameters)
    document["state"]["frames"][0]["type"] = original
    document, parameters = piled(generated)
    document["state"]["piles"][0]["type"] = "SOLID"
    refuse("PILE_UNSUPPORTED", write, document, parameters)


def test_caps_and_output_limit(generated, monkeypatch):
    document, parameters, _ = generated
    monkeypatch.setattr(native, "MAX_FRAMES", 0)
    refuse("LIMIT_EXCEEDED", write, document, parameters)
    monkeypatch.setattr(native, "MAX_FRAMES", kernels.MAX_FRAMES)
    document, parameters = piled(generated)
    monkeypatch.setattr(native, "MAX_PILES", 0)
    refuse("LIMIT_EXCEEDED", write, document, parameters)
    monkeypatch.setattr(native, "MAX_PILES", kernels.MAX_PILES)
    # The base envelope fits; generated geometry and metadata exceed this cap.
    envelope = intake_dxf.intake_to_dxf({"layers": ["0", "Defpoints"]})
    monkeypatch.setattr(native, "MAX_DXF_BYTES", len(envelope) + 1)
    refuse("LIMIT_EXCEEDED", write, document, parameters)


@pytest.mark.parametrize("source", [b"AutoCAD Binary DXF\r\n\x1a\x00", b"not dxf", b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n", b"0\nEOF\n1\n"])
def test_invalid_source_refused(generated, source):
    with pytest.raises(native.NativeLayoutError):
        write(generated[0], generated[1], source)


def test_invalid_head_source_combinations(generated):
    document, parameters, _ = generated
    for head, source in (({}, None), (None, b"source")):
        refuse("SOURCE_REQUIRED", native.materialize_native_layout, SCOPE, head, document, source,
               parameters=parameters)


def test_duplicate_sections_handles_and_unmanaged_leaf_refused(generated):
    source, _, _ = foreign_source()
    duplicate = source.replace(b"0\nEOF\n", b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n")
    refuse("SOURCE_AMBIGUOUS", write, generated[0], generated[1], duplicate)
    duplicate = insert(source, "ENTITIES", b"0\nPOINT\n5\nF001\n8\nForeign\n10\n0\n20\n0\n")
    refuse("SOURCE_AMBIGUOUS", write, generated[0], generated[1], duplicate)
    unmanaged = insert(source, "ENTITIES", b"0\nCIRCLE\n5\nF003\n8\nLEAF-PILING\n40\n1\n")
    refuse("OWNERSHIP_AMBIGUOUS", write, generated[0], generated[1], unmanaged)


def test_namespace_corruption_scope_and_entity_tampering_refused(generated):
    document, parameters, _ = generated
    raw = write(document, parameters)
    damaged = raw.replace(native.SCHEMA.encode(), b"foreign-namespace-schema")
    refuse("NAMESPACE_INVALID", write, document, parameters, damaged)
    refuse("NAMESPACE_INVALID", native.decode_native_layout, damaged)
    wrong_scope = dict(SCOPE, drawing_id=UUID("00000000-0000-4000-8000-000000000004"))
    refuse("SCOPE_MISMATCH", native.materialize_native_layout, wrong_scope, {}, document, raw,
           parameters=parameters)
    real_handle = native.decode_native_layout(raw).frames[0]["handle"]
    needle = f"0\nLWPOLYLINE\n5\n{real_handle}\n".encode()
    tampered = raw.replace(needle, needle.replace(b"LWPOLYLINE", b"LINE"), 1)
    refuse("OWNERSHIP_AMBIGUOUS", write, document, parameters, tampered)
    # Mutate only digest bytes, across arbitrary JSON chunks, without using the decoder.
    xr = next(r for r in records(sections(raw)["OBJECTS"]) if (1, native.SCHEMA) in r)
    payload = json.loads("".join(v for c, v in xr if c == 1 and v != native.SCHEMA))
    for field in ("document_sha256", "parameters_sha256"):
        broken = dict(payload, **{field: "0" * 64})
        text = json.dumps(broken, sort_keys=True, separators=(",", ":"))
        replacement = [(c, v) for c, v in xr if c != 1] + [(1, native.SCHEMA)]
        replacement += [(1, text[i:i + 255]) for i in range(0, len(text), 255)]
        refuse("NAMESPACE_INVALID", native.decode_native_layout, raw.replace(emit(xr), emit(replacement)))


@pytest.mark.parametrize("mutation", ["extra_foreign", "alias"])
def test_ownership_map_cannot_claim_unrelated_or_alias_entities(generated, mutation):
    document, parameters, _ = generated
    source, foreign_entities, _ = foreign_source()
    raw = write(document, parameters, source)
    xr = next(r for r in records(sections(raw)["OBJECTS"]) if (1, native.SCHEMA) in r)
    payload = json.loads("".join(v for c, v in xr if c == 1 and v != native.SCHEMA))
    if mutation == "extra_foreign":
        payload["handle_map"]["not-a-layout-entity"] = "F001"
    else:
        first, second = list(payload["handle_map"])[:2]
        restored = ps.decode_document(payload["document"].encode())
        neutral_handle = restored["state"]["frames"][1]["handle"]
        restored["state"]["frames"][1] = copy.deepcopy(restored["state"]["frames"][0])
        restored["state"]["frames"][1]["handle"] = neutral_handle
        payload["document"] = ps.encode_document(restored).decode()
        payload["document_sha256"] = hashlib.sha256(payload["document"].encode()).hexdigest()
        payload["handle_map"][second] = payload["handle_map"][first]
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    replacement = [(c, v) for c, v in xr if c != 1] + [(1, native.SCHEMA)]
    replacement += [(1, text[i:i + 255]) for i in range(0, len(text), 255)]
    tampered = raw.replace(emit(xr), emit(replacement))
    assert foreign_entities in tampered
    refuse("OWNERSHIP_AMBIGUOUS", native.decode_native_layout, tampered)
    refuse("OWNERSHIP_AMBIGUOUS", write, document, parameters, tampered)
    assert foreign_entities in raw


def test_optional_independent_ezdxf_recovery(generated):
    # All geometry/source assertions above run without this optional dependency.
    recover = pytest.importorskip("ezdxf.recover")
    document, parameters = piled(generated)
    raw = write(document, parameters, write(generated[0], generated[1]))
    drawing, auditor = recover.read(BytesIO(raw))
    assert not auditor.errors
    assert len(drawing.modelspace().query('LWPOLYLINE[layer=="LEAF-TRACKERS"]')) == len(document["state"]["frames"])
    assert len(drawing.modelspace().query('CIRCLE[layer=="LEAF-PILING"]')) == len(document["state"]["piles"])
    first = drawing.modelspace().query('CIRCLE[layer=="LEAF-PILING"]')[0]
    assert len(first.get_xdata("LEAFPILING")) == 10


def insertion_units_header(raw, declarations):
    """Replace only the test fixture's insertion-unit variables."""
    data = pairs(raw)
    start = data.index((2, "HEADER")) + 1
    end = data.index((0, "ENDSEC"), start)
    header, index = [], start
    while index < end:
        if data[index] == (9, "$INSUNITS"):
            index += 2
        else:
            header.append(data[index])
            index += 1
    return emit(data[:start] + header + declarations + data[end:])


def synthetic_units_layout(units):
    preset = {"Name": "Synthetic units", "Rows": 2, "Columns": 3,
              "ModuleLengthM": 2.0, "ModuleWidthM": 1.0, "ModulePowerWp": 400,
              "Orientation": "Portrait", "FramingType": "FixedTilt",
              "HorizontalGapM": 0.1, "VerticalGapM": 0.1,
              "TiltDegrees": 15.0, "AxisAzimuthDeg": 180.0,
              "HeightAtLowPoseM": 0.7, "HeightAtHighPoseM": 1.7,
              "PileTemplateName": "Synthetic piles"}
    return fp.prepare_initial_frames(boundary=[[0, 0], [20, 0], [20, 40], [0, 40]],
                                     preset=preset, drawing_units=units)


@pytest.mark.parametrize("units,code", [("m", "6"), ("ft", "2")])
def test_insertion_units_declare_physical_document(units, code):
    document, parameters = synthetic_units_layout(units)
    raw = write(document, parameters)
    header = sections(raw)["HEADER"]
    index = header.index((9, "$INSUNITS"))
    assert header[index + 1] == (70, code)
    assert header.count((9, "$INSUNITS")) == 1
    assert native.decode_native_layout(raw).document == document


@pytest.mark.parametrize("code", [None, "0"])
def test_insertion_units_legacy_read_and_explicit_rewrite(code):
    document, parameters = synthetic_units_layout("m")
    declarations = [] if code is None else [(9, "$INSUNITS"), (70, code)]
    legacy = insertion_units_header(write(document, parameters), declarations)
    assert native.decode_native_layout(legacy).document == document
    rewritten = write(document, parameters, legacy)
    header = sections(rewritten)["HEADER"]
    assert header[header.index((9, "$INSUNITS")) + 1] == (70, "6")
    assert native.decode_native_layout(rewritten).document == document


@pytest.mark.parametrize("units,conflicting", [("m", "2"), ("ft", "6")])
def test_insertion_units_conflict_refuses_without_rescaling(units, conflicting):
    document, parameters = synthetic_units_layout(units)
    raw = insertion_units_header(write(document, parameters), [(9, "$INSUNITS"), (70, conflicting)])
    refuse("UNITS_MISMATCH", native.decode_native_layout, raw)
    refuse("UNITS_MISMATCH", write, document, parameters, raw)


@pytest.mark.parametrize("declarations", [
    [(9, "$INSUNITS"), (70, "6"), (9, "$INSUNITS"), (70, "6")],
    [(9, "$INSUNITS"), (40, "6")],
    [(9, "$INSUNITS"), (70, "6.0")],
    [(9, "$INSUNITS"), (70, "-1")],
])
def test_insertion_units_malformed_or_duplicate_refuses(declarations):
    document, parameters = synthetic_units_layout("m")
    raw = insertion_units_header(write(document, parameters), declarations)
    refuse("UNITS_INVALID", native.decode_native_layout, raw)
    refuse("UNITS_INVALID", write, document, parameters, raw)


def test_insertion_units_preserve_unrelated_raw_header_and_frame_attributes():
    document, parameters = synthetic_units_layout("m")
    raw = insertion_units_header(write(document, parameters), [])
    retained = b"  9\r\n$USERI1\r\n 70\r\n17\r\n"
    raw = insert(raw, "HEADER", retained)
    before = native.decode_native_layout(raw)
    result = write(document, parameters, raw)
    assert retained in result
    after = native.decode_native_layout(result)
    assert after.document == before.document
    assert after.parameters == before.parameters
    assert [(r['row'], r['col'], r['vertices'], r['elevation']) for r in after.frames] == [
        (r['row'], r['col'], r['vertices'], r['elevation']) for r in before.frames]


# Native worker byte repair contract. This executes Node, not WASM.
from copy import deepcopy
import base64
import shutil
import subprocess
import solar_frames_piles as frames

HELPER = ROOT / "vendor/acadrust-worker/native-layout-bytes.mjs"
NODE = shutil.which("node")


def worker_repair(parent, written):
    # Missing Node is a failed prerequisite, never an optional passing skip.
    assert NODE, "Node is required for the native worker export contract"
    script = """
import {readFileSync} from 'node:fs';
const {readNativeLayout,repairNativeLayout}=await import(process.argv[1]);
const input=JSON.parse(readFileSync(0,'utf8'));
try {
 const parent=await readNativeLayout(Uint8Array.from(Buffer.from(input.parent,'base64')));
 const result=await repairNativeLayout(parent,Uint8Array.from(Buffer.from(input.written,'base64')));
 process.stdout.write(JSON.stringify({bytes:Buffer.from(result).toString('base64')}));
} catch(error) {process.stdout.write(JSON.stringify({error:error.message}));}
"""
    result = subprocess.run([NODE, "--input-type=module", "-e", script, HELPER.as_uri()],
        input=json.dumps({"parent": base64.b64encode(parent).decode(),
                          "written": base64.b64encode(written).decode()}),
        text=True, capture_output=True, check=True, timeout=20)
    reply = json.loads(result.stdout)
    return reply if "error" in reply else base64.b64decode(reply["bytes"])


@pytest.fixture
def source():
    fixture = json.loads((ROOT / "docs/parity/evidence/ground/generate/intake.json").read_text(encoding="utf-8"))
    document, parameters = frames.prepare_initial_frames(
        boundary=[[0, 0], [20, 0], [20, 40], [0, 40]],
        preset=fixture["active_preset"], drawing_units="m")
    parameters["export_test"] = {"label": "synthetic \u00e9", "small": 1e-12}
    scope = {key: UUID(int=index + 1) for index, key in enumerate(
        ("organization_id", "project_id", "drawing_id"))}
    raw = native.materialize_native_layout(scope, None, document, None, parameters=parameters)
    return raw, native.decode_native_layout(raw)


def written_bytes(raw, change):
    """Model only the observed writer lexemes; this is not a fake WASM proof."""
    drawing = native._Drawing(raw)
    row = next(r for r in drawing.records["ENTITIES"] if r.one(0) == "LWPOLYLINE")
    pairs = []
    first_x = True
    for pair in row.pairs:
        value = pair.value
        if pair.code == 10:
            value = format(float(value) + (2 if change != "resize" or first_x else 0), ".17g")
            first_x = False
        if pair.code == 8 and change == "layer":
            value = "Changed"
        pairs.append((pair.code, value))
        if pair.code == 20:
            pairs.extend([(40, "1" if change == "width" else "0"), (41, "0"), (42, "0")])
    body = native._pack(pairs)
    # The actual writer pads group codes. The owned canonical output must not.
    lines = body.splitlines(keepends=True)
    body = b"".join(code.rstrip(b"\r\n").rjust(3) + b"\n" + value
                    for code, value in zip(lines[::2], lines[1::2]))
    result = (b"".join(p.raw for p in drawing.pairs[:row.start]) + body
              + b"".join(p.raw for p in drawing.pairs[row.end:]))
    if change == "added":
        result = result.replace(b"0\nENDSEC\n0\nSECTION\n2\nOBJECTS\n",
            native._pack([(0, "LWPOLYLINE"), (5, "FFFFFFFF"), (8, "LEAF-TRACKERS")])
            + b"0\nENDSEC\n0\nSECTION\n2\nOBJECTS\n", 1)
    if change == "metadata":
        result = result.replace(b'"document_sha256":"', b'"document_sha256":"f', 1)
    return result


def foreign(raw, handles):
    drawing = native._Drawing(raw)
    owned = [r for rows in drawing.records.values() for r in rows
             if r.one(5) in handles or (r.one(0) == "XRECORD" and r.values(1)[0] == native.SCHEMA)]
    offsets = [0]
    for pair in drawing.pairs:
        offsets.append(offsets[-1] + len(pair.raw))
    spans = sorted((offsets[r.start], offsets[r.end]) for r in owned)
    chunks, start = [], 0
    for begin, end in spans:
        chunks.append(raw[start:begin])
        start = end
    return b"".join(chunks) + raw[start:]


def test_worker_export_rigid_edit_updates_tuple_state_and_keeps_opaque_metadata(source):
    raw, before = source
    written = written_bytes(raw, "move")
    # Retain foreign UTF-8 entity text byte for byte, not a re-encoded string.
    marker = b"0\nSECTION\n2\nENTITIES\n"
    written = written.replace(marker, marker +
        b"0\nTEXT\n5\nFFFFFFFE\n8\nForeign\n10\n0\n20\n0\n1\nforeign \xc3\xa9\n", 1)
    with pytest.raises(native.NativeLayoutError):
        native.decode_native_layout(written)
    repaired = worker_repair(raw, written)
    assert isinstance(repaired, bytes), repaired
    after = native.decode_native_layout(repaired)
    expected = deepcopy(before.document)
    frame = expected["state"]["frames"][0]
    frame["vertices"] = [(x + 2, y) for x, y in frame["vertices"]]
    assert after.document == expected
    assert after.parameters == before.parameters and after.scope == before.scope
    assert after.handles == before.handles
    assert after.xrecord["parameters_sha256"] == before.xrecord["parameters_sha256"]
    assert foreign(repaired, set(before.handles.values())) == foreign(written, set(before.handles.values()))


def test_worker_export_second_edit_retains_the_first_pose_and_metadata(source):
    raw, before = source
    first = worker_repair(raw, written_bytes(raw, "move"))
    second = worker_repair(first, written_bytes(first, "move"))
    after = native.decode_native_layout(second)
    expected = deepcopy(before.document)
    expected["state"]["frames"][0]["vertices"] = [
        (x + 4, y) for x, y in expected["state"]["frames"][0]["vertices"]]
    assert after.document == expected and after.parameters == before.parameters


@pytest.mark.parametrize("change", ["resize", "layer", "width", "added", "metadata"])
def test_worker_export_refuses_non_pose_changes_without_mutating_parent(source, change):
    raw, _ = source
    original = bytes(raw)
    result = worker_repair(raw, written_bytes(raw, change))
    assert isinstance(result, dict) and result["error"].startswith("native_layout:"), result
    assert raw == original


def test_worker_export_non_native_bytes_bypass_without_rewriting():
    raw = b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n"
    assert worker_repair(raw, raw) == raw
