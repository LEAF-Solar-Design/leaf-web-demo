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
