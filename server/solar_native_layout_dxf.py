"""Pure native Solar DXF emission; source drawings are edited as raw group pairs.

Only our namespace and its verified entities confer ownership. Nothing in this
module executes a drawing, interprets Lisp, or contacts a native CAD provider.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
import json
import math
import re
from uuid import UUID

import intake_dxf
import solar_ground_frames as kernels
import solar_physical_state as ps

MAX_FRAMES = kernels.MAX_FRAMES
MAX_PILES = kernels.MAX_PILES
MAX_DXF_BYTES = 16 * 1024 * 1024
NAMESPACE = "LEAF_STUDIO_LAYOUT"
SCHEMA = "leaf.studio-native-layout.v1"
_HEX = re.compile(r"[0-9A-Fa-f]{1,16}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class NativeLayoutError(ValueError):
    """Payload-free, coded serialization refusal."""

    def __init__(self, code):
        self.code = "NATIVE_LAYOUT_" + code
        super().__init__(self.code)


def _fail(code):
    raise NativeLayoutError(code)


def _typed(function):
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except NativeLayoutError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            _fail("STATE_INVALID")
    return checked


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        _fail("STATE_INVALID")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _text(value):
    if (type(value) is not str or len(value) > 255 or _CONTROL.search(value)
            or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
        _fail("STRING_INVALID")
    return value


def _number(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e15
    except OverflowError:
        valid = False
    if not valid:
        _fail("NUMBER_INVALID")
    return value


def _walk(value, depth=0):
    if depth > 32:
        _fail("STATE_INVALID")
    if isinstance(value, dict):
        for key, child in value.items():
            _text(key)
            _walk(child, depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _walk(child, depth + 1)
    elif type(value) is str:
        _text(value)
    elif type(value) in (int, float):
        _number(value)
    elif value is not None and type(value) is not bool:
        _fail("STATE_INVALID")


def format_dotnet(value):
    """.NET custom double format 0.######## (15 significant digits, half up).

    Custom formats retain the double formatter's 15-digit decimal conversion;
    rounding the binary float directly with Python's round() is different.
    """
    _number(value)
    with localcontext() as ctx:
        ctx.prec = 40
        rounded = Decimal(format(float(value), ".15g")).quantize(
            Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    if not rounded:
        return "0"
    return format(rounded, "f").rstrip("0").rstrip(".")


def _num(value):
    return format(_number(value), ".17g")


def _scope(context):
    if not isinstance(context, dict):
        binding = getattr(context, "binding", None)
        if binding is None:
            _fail("SCOPE_INVALID")
        context = {"organization_id": binding.organization_id,
                   "project_id": binding.project_id, "drawing_id": binding.drawing_id}
    try:
        return {key: str(UUID(str(context[key])))
                for key in ("organization_id", "project_id", "drawing_id")}
    except (ValueError, KeyError, TypeError, AttributeError):
        _fail("SCOPE_INVALID")


def _document(document, parameters):
    if type(parameters) is not dict or type(document) is not dict:
        _fail("STATE_INVALID")
    _walk(document)
    _walk(parameters)
    if json.loads(_json(parameters)) != parameters:
        _fail("STATE_INVALID")
    state = document.get("state")
    if type(state) is not dict:
        _fail("STATE_INVALID")
    # Terrain and counters are document data, not additional generated shapes.
    defaults = {"mesh_faces": 0, "collision_layer": [], "slope_markers": []}
    for key, value in state.items():
        if key in ("frames", "piles", "grid", "next_handle"):
            continue
        if key not in defaults or value != defaults[key]:
            _fail("STATE_UNSUPPORTED")
    for key, cap in (("frames", MAX_FRAMES), ("piles", MAX_PILES)):
        if type(state.get(key, [])) is not list:
            _fail("STATE_INVALID")
        if len(state.get(key, [])) > cap:
            _fail("LIMIT_EXCEEDED")
    try:
        encoded = ps.encode_document(document)
    except ps.PhysicalStateError:
        _fail("STATE_INVALID")
    return encoded.decode("utf-8")


def _pack(pairs):
    text = "".join(f"{code}\n{value}\n" for code, value in pairs)
    # R2000's text representation is ASCII with DXF Unicode escapes.
    text = re.sub(r"\\(?=U\+[0-9A-Fa-f]{4})", lambda m: "\\U+005C", text)
    out = []
    for char in text:
        if ord(char) < 128:
            out.append(char)
        else:
            encoded = char.encode("utf-16-le")
            out.extend(f"\\U+{int.from_bytes(encoded[i:i + 2], 'little'):04X}"
                       for i in range(0, len(encoded), 2))
    return "".join(out).encode("ascii")


def _decode_text(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        # Retained legacy code-page data is never re-encoded. Critical envelope
        # identities are ASCII; this fallback only makes foreign values readable.
        text = raw.decode("latin-1")
    text = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m[1], 16)), text)
    try:
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    except UnicodeError:
        _fail("SOURCE_INVALID")


@dataclass(frozen=True)
class _Pair:
    code: int
    value: str
    raw: bytes


@dataclass(frozen=True)
class _Record:
    start: int
    end: int
    pairs: tuple

    def values(self, code):
        return [p.value for p in self.pairs if p.code == code]

    def one(self, code, default=None):
        values = self.values(code)
        if len(values) > 1:
            _fail("SOURCE_AMBIGUOUS")
        return values[0] if values else default


class _Drawing:
    def __init__(self, raw):
        if type(raw) is not bytes or not raw or raw.startswith(b"AutoCAD Binary DXF"):
            _fail("SOURCE_INVALID")
        if len(raw) > MAX_DXF_BYTES:
            _fail("LIMIT_EXCEEDED")
        lines = raw.splitlines(keepends=True)
        if len(lines) % 2:
            _fail("SOURCE_INVALID")
        self.pairs = []
        for i in range(0, len(lines), 2):
            try:
                code_text = lines[i].rstrip(b"\r\n").decode("ascii").strip()
                if not re.fullmatch(r"[0-9]{1,4}", code_text):
                    _fail("SOURCE_INVALID")
                code = int(code_text)
                value = _decode_text(lines[i + 1].rstrip(b"\r\n"))
                if code > 1071 or "\x00" in value:
                    _fail("SOURCE_INVALID")
            except UnicodeError:
                _fail("SOURCE_INVALID")
            self.pairs.append(_Pair(code, value, lines[i] + lines[i + 1]))
        p = self.pairs
        self.sections = {}
        i = 0
        while i < len(p) - 1:
            if (p[i].code, p[i].value) != (0, "SECTION") or p[i + 1].code != 2:
                _fail("SOURCE_INVALID")
            name = p[i + 1].value
            if name in self.sections:
                _fail("SOURCE_AMBIGUOUS")
            start = i + 2
            i = start
            while i < len(p) and (p[i].code, p[i].value) != (0, "ENDSEC"):
                if p[i].code == 0 and p[i].value in ("SECTION", "EOF"):
                    _fail("SOURCE_INVALID")
                i += 1
            if i == len(p):
                _fail("SOURCE_INVALID")
            self.sections[name] = (start, i)
            i += 1
        if i != len(p) - 1 or (p[i].code, p[i].value) != (0, "EOF"):
            _fail("SOURCE_INVALID")
        if not {"HEADER", "TABLES", "BLOCKS", "ENTITIES", "OBJECTS"} <= self.sections.keys():
            _fail("SOURCE_UNSAFE")
        self.records = {}
        self.handles = {}
        for name, (start, end) in self.sections.items():
            if name == "HEADER":
                continue
            indexes = [j for j in range(start, end) if p[j].code == 0]
            if start != end and (not indexes or indexes[0] != start):
                _fail("SOURCE_INVALID")
            records = [_Record(a, b, tuple(p[a:b]))
                       for a, b in zip(indexes, indexes[1:] + [end])]
            self.records[name] = records
            for record in records:
                hs = record.values(5) + record.values(105)
                if len(hs) > 1:
                    _fail("SOURCE_AMBIGUOUS")
                if hs:
                    h = hs[0].upper()
                    if not _HEX.fullmatch(h) or int(h, 16) == 0 or h in self.handles:
                        _fail("SOURCE_AMBIGUOUS")
                    self.handles[h] = record
        start, end = self.sections["HEADER"]
        seeds = [j + 1 for j in range(start, end) if p[j].code == 9 and p[j].value == "$HANDSEED"]
        versions = [j + 1 for j in range(start, end) if p[j].code == 9 and p[j].value == "$ACADVER"]
        if (len(seeds) != 1 or seeds[0] >= end or p[seeds[0]].code != 5
                or not _HEX.fullmatch(p[seeds[0]].value) or len(versions) != 1
                or versions[0] >= end or p[versions[0]].code != 1
                or p[versions[0]].value not in ("AC1015", "AC1018", "AC1021", "AC1024", "AC1027", "AC1032")):
            _fail("SOURCE_UNSAFE")
        self.seed_index = seeds[0]
        self.entity_starts = {r.start for r in self.records["ENTITIES"]}
        self.object_starts = {r.start for r in self.records["OBJECTS"]}
        self.next_handle = max(int(p[seeds[0]].value, 16),
                               max((int(h, 16) for h in self.handles), default=0)) + 1
        roots = [r for r in self.records["OBJECTS"]
                 if r.one(0) == "DICTIONARY" and r.one(330, "0") == "0"]
        if len(roots) != 1 or roots[0].one(5) is None:
            _fail("SOURCE_UNSAFE")
        self.root = roots[0]
        self.tables = {}
        current = None
        for record in self.records["TABLES"]:
            kind = record.one(0)
            if kind == "TABLE":
                name = record.one(2)
                if current is not None or name in self.tables or record.one(5) is None:
                    _fail("SOURCE_AMBIGUOUS")
                current = [record, [], None]
                self.tables[name] = current
            elif kind == "ENDTAB":
                if current is None:
                    _fail("SOURCE_INVALID")
                current[2] = record.start
                current = None
            elif current is None:
                _fail("SOURCE_INVALID")
            else:
                current[1].append(record)
        if current is not None:
            _fail("SOURCE_INVALID")
        if not {"LAYER", "APPID", "BLOCK_RECORD"} <= self.tables.keys():
            _fail("SOURCE_UNSAFE")
        for _, records, _ in self.tables.values():
            names = [r.one(2, "").casefold() for r in records]
            if len(names) != len(set(names)):
                _fail("SOURCE_AMBIGUOUS")
        models = [r for r in self.tables["BLOCK_RECORD"][1]
                  if r.one(2, "").casefold() == "*model_space"]
        if len(models) != 1 or models[0].one(5) is None:
            _fail("SOURCE_UNSAFE")
        self.model = models[0].one(5)

    def fresh(self):
        if self.next_handle >= 2 ** 64 - 1:
            _fail("LIMIT_EXCEEDED")
        result = f"{self.next_handle:X}"
        self.next_handle += 1
        return result


def _entity_pairs(document, handle_map, owner):
    state = document["state"]
    out = []
    seen = set()
    for index, frame in enumerate(state.get("frames", [])):
        if type(frame) is not dict or frame.get("type") == "INSERT":
            _fail("FRAME_UNSUPPORTED")
        if (frame.get("type") != "LWPOLYLINE" or frame.get("layer") != "LEAF-TRACKERS"
                or frame.get("closed") is not True or len(frame.get("vertices", [])) != 4):
            _fail("FRAME_UNSUPPORTED")
        key = frame.get("handle")
        if type(key) is not str or not _HEX.fullmatch(key) or key in seen:
            _fail("OWNERSHIP_AMBIGUOUS")
        seen.add(key)
        handle = handle_map.get(key)
        if handle is None:
            _fail("OWNERSHIP_AMBIGUOUS")
        pairs = [(0, "LWPOLYLINE"), (5, handle), (330, owner), (100, "AcDbEntity"), (8, "LEAF-TRACKERS")]
        color = frame.get("color")
        if type(color) is not dict:
            _fail("FRAME_UNSUPPORTED")
        if color.get("method") == "ByAci":
            aci = color.get("index")
            if type(aci) is not int or not 1 <= aci <= 255:
                _fail("FRAME_UNSUPPORTED")
            pairs.append((62, str(aci)))
        elif color.get("method") != "ByLayer":
            _fail("FRAME_UNSUPPORTED")
        pairs += [(100, "AcDbPolyline"), (90, "4"), (70, "1"), (38, _num(frame.get("elevation")))]
        for point in frame["vertices"]:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                _fail("FRAME_UNSUPPORTED")
            pairs += [(10, _num(point[0])), (20, _num(point[1]))]
        pairs.append((1001, "LEAFFRAMECELL"))
        for name in ("row", "col"):
            value = frame.get(name)
            if type(value) is not int or value < 0:
                _fail("FRAME_UNSUPPORTED")
            pairs.append((1070, str(min(value, 32767))))
        if frame.get("site_revision") is not None:
            pairs.append((1000, _text(frame["site_revision"])))
        out.append(pairs)
    for index, pile in enumerate(state.get("piles", [])):
        if (type(pile) is not dict or pile.get("type") != "CIRCLE"
                or pile.get("layer") != "LEAF-PILING" or tuple(pile.get("normal", ())) != (0, 0, 1)
                or pile.get("color", {}).get("method") != "ByLayer"):
            _fail("PILE_UNSUPPORTED")
        handle = handle_map.get(f"pile:{index}")
        if handle is None:
            _fail("OWNERSHIP_AMBIGUOUS")
        center = pile.get("center")
        if not isinstance(center, (list, tuple)) or len(center) != 3:
            _fail("PILE_UNSUPPORTED")
        if _number(pile.get("radius")) <= 0 or _number(pile.get("thickness")) <= 0:
            _fail("PILE_UNSUPPORTED")
        pairs = [(0, "CIRCLE"), (5, handle), (330, owner), (100, "AcDbEntity"), (8, "LEAF-PILING"),
                 (100, "AcDbCircle"), (39, _num(pile["thickness"])),
                 *[(code, _num(value)) for code, value in zip((10, 20, 30), center)],
                 (40, _num(pile["radius"])), (210, "0"), (220, "0"), (230, "1"), (1001, "LEAFPILING")]
        data = pile.get("pile")
        if type(data) is not dict or data.get("placement") not in ("Grid", "Joint", "Station"):
            _fail("PILE_UNSUPPORTED")
        tags = [data["placement"], data.get("placement_detail", "")]
        for key, prefix in (("source_tracker", "SourceTracker="), ("template", "Template="),
                            ("mapping", "Mapping="), ("station_label", "StationLabel=")):
            value = data.get(key)
            if value is None or not str(value).strip():
                tags.append("")
                continue
            if key == "source_tracker":
                if value not in seen:
                    _fail("OWNERSHIP_AMBIGUOUS")
                value = handle_map[value]
            tags.append(prefix + _text(value))
        for key, prefix in (("diameter_m", "DiameterM="), ("reveal_m", "RevealM="),
                            ("embedment_m", "EmbedmentM="), ("depth_m", "DepthM=")):
            value = data.get(key)
            if key == "depth_m":
                value = data["reveal_m"] + data["embedment_m"]
                if data.get(key) != value:
                    _fail("PILE_UNSUPPORTED")
            tags.append(prefix + format_dotnet(value))
        pairs += [(1000, _text(tag)) for tag in tags]
        out.append(pairs)
    expected = seen | {f"pile:{i}" for i in range(len(state.get("piles", [])))}
    if set(handle_map) != expected or len(set(handle_map.values())) != len(expected):
        _fail("OWNERSHIP_AMBIGUOUS")
    return out


def _namespace(drawing):
    root = drawing.root
    for other in drawing.records["OBJECTS"]:
        if (other.start != root.start and other.one(0) in ("DICTIONARY", "ACDBDICTIONARYWDFLT")
                and any(v.casefold() == NAMESPACE.casefold() for v in other.values(3))):
            _fail("OWNERSHIP_AMBIGUOUS")
    entries = []
    pairs = root.pairs
    names = []
    for i, pair in enumerate(pairs):
        if pair.code == 3:
            names.append(pair.value.casefold())
            if i + 1 >= len(pairs) or pairs[i + 1].code not in (350, 360):
                _fail("SOURCE_UNSAFE")
            if pair.value.casefold() == NAMESPACE.casefold():
                entries.append(pairs[i + 1].value.upper())
    if len(names) != len(set(names)) or len(entries) > 1:
        _fail("OWNERSHIP_AMBIGUOUS")
    if not entries:
        return None, None
    record = drawing.handles.get(entries[0])
    if (record is None or record.start not in drawing.object_starts or record.one(0) != "XRECORD"
            or record.one(330, "").upper() != root.one(5).upper()
            or record.one(280) != "1"):
        _fail("NAMESPACE_INVALID")
    # A second dictionary reference makes replacement ownership ambiguous.
    references = [p for records in drawing.records.values() for r in records for p in r.pairs
                  if p.code in (350, 360) and p.value.upper() == entries[0]]
    if len(references) != 1:
        _fail("OWNERSHIP_AMBIGUOUS")
    chunks = record.values(1)
    if not chunks or chunks[0] != SCHEMA or any(len(s) > 255 for s in chunks):
        _fail("NAMESPACE_INVALID")
    try:
        text = "".join(chunks[1:])
        payload = json.loads(text)
        if (_json(payload) != text or set(payload) != {"document", "parameters", "scope", "handle_map",
                                                     "document_sha256", "parameters_sha256"}
                or _scope(payload["scope"]) != payload["scope"]
                or type(payload["document"]) is not str
                or type(payload["handle_map"]) is not dict):
            _fail("NAMESPACE_INVALID")
        document = ps.decode_document(payload["document"].encode("utf-8"))
        _document(document, payload["parameters"])
        if (_sha(payload["document"].encode("utf-8")) != payload["document_sha256"]
                or _sha(_json(payload["parameters"]).encode("utf-8")) != payload["parameters_sha256"]):
            _fail("NAMESPACE_INVALID")
        for value in payload["handle_map"].values():
            if type(value) is not str or not _HEX.fullmatch(value):
                _fail("NAMESPACE_INVALID")
        expected = _entity_pairs(document, payload["handle_map"], drawing.model)
        for pairs in expected:
            handle = next(v for c, v in pairs if c == 5)
            actual = drawing.handles.get(handle)
            if (actual is None or actual.start not in drawing.entity_starts
                    or [(p.code, p.value) for p in actual.pairs] != pairs):
                _fail("OWNERSHIP_AMBIGUOUS")
        owned = {h.upper() for h in payload["handle_map"].values()}
        for actual in drawing.records["ENTITIES"]:
            if (actual.one(8, "").upper() in ("LEAF-TRACKERS", "LEAF-PILING")
                    or any(v.upper() in ("LEAFFRAMECELL", "LEAFPILING") for v in actual.values(1001))):
                if actual.one(5, "").upper() not in owned:
                    _fail("OWNERSHIP_AMBIGUOUS")
        # Replacing entities must not strand references held by retained foreign data.
        for rows in drawing.records.values():
            for actual in rows:
                if actual.one(5, "").upper() in owned:
                    continue
                if any((330 <= p.code <= 369 or 390 <= p.code <= 399 or p.code == 1005)
                       and p.value.upper() in owned for p in actual.pairs):
                    _fail("OWNERSHIP_AMBIGUOUS")
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        if isinstance(exc, NativeLayoutError):
            raise
        _fail("NAMESPACE_INVALID")
    return record, payload


@dataclass(frozen=True)
class NativeLayout:
    frames: tuple
    piles: tuple
    handles: dict
    xrecord: dict
    document: dict
    parameters: dict
    scope: dict


@_typed
def decode_native_layout(raw):
    drawing = _Drawing(raw)
    _, payload = _namespace(drawing)
    if payload is None:
        _fail("NAMESPACE_INVALID")
    document = ps.decode_document(payload["document"].encode("utf-8"))
    frames, piles = [], []
    for pairs in _entity_pairs(document, payload["handle_map"], drawing.model):
        values = lambda code: [v for c, v in pairs if c == code]
        base = {"handle": values(5)[0], "layer": values(8)[0]}
        if pairs[0][1] == "LWPOLYLINE":
            base.update(vertices=tuple(zip(map(float, values(10)), map(float, values(20)))),
                        elevation=float(values(38)[0]), row=int(values(1070)[0]), col=int(values(1070)[1]),
                        site_revision=next(iter(values(1000)), None),
                        color=int(values(62)[0]) if values(62) else 256)
            frames.append(base)
        else:
            tags = values(1000)
            data = {"placement": tags[0], "placement_detail": tags[1]}
            for i, (key, prefix) in enumerate((("source_tracker", "SourceTracker="), ("template", "Template="),
                                              ("mapping", "Mapping="), ("station_label", "StationLabel=")), 2):
                data[key] = tags[i][len(prefix):] if tags[i] else None
            for i, key in enumerate(("diameter_m", "reveal_m", "embedment_m", "depth_m"), 6):
                data[key] = float(tags[i].split("=", 1)[1])
            base.update(center=tuple(float(values(c)[0]) for c in (10, 20, 30)),
                        radius=float(values(40)[0]), thickness=float(values(39)[0]),
                        normal=(0.0, 0.0, 1.0), tags=tuple(tags), pile=data)
            piles.append(base)
    return NativeLayout(tuple(frames), tuple(piles), payload["handle_map"], payload,
                        document, payload["parameters"], payload["scope"])


@_typed
def materialize_native_layout(context, head, document, source, *, parameters):
    scope = _scope(context)
    document_text = _document(document, parameters)
    if (head is None) != (source is None):
        _fail("SOURCE_REQUIRED")
    raw = intake_dxf.intake_to_dxf({"layers": ["0", "Defpoints"]}) if source is None else source
    drawing = _Drawing(raw)
    old_record, old = _namespace(drawing)
    if old is not None and old["scope"] != scope:
        _fail("SCOPE_MISMATCH")
    owned = set(old["handle_map"].values()) if old else set()
    for record in drawing.records["ENTITIES"]:
        if (record.one(8, "").upper() in ("LEAF-TRACKERS", "LEAF-PILING")
                or any(v.upper() in ("LEAFFRAMECELL", "LEAFPILING") for v in record.values(1001))):
            if record.one(5) not in owned:
                _fail("OWNERSHIP_AMBIGUOUS")
    handle_map = {}
    for frame in document["state"].get("frames", []):
        if type(frame) is not dict or type(frame.get("handle")) is not str:
            _fail("FRAME_UNSUPPORTED")
        if frame["handle"] in handle_map:
            _fail("OWNERSHIP_AMBIGUOUS")
        handle_map[frame["handle"]] = drawing.fresh()
    for i, _ in enumerate(document["state"].get("piles", [])):
        handle_map[f"pile:{i}"] = drawing.fresh()
    entities = _entity_pairs(document, handle_map, drawing.model)
    payload = {"document": document_text, "parameters": parameters, "scope": scope, "handle_map": handle_map,
               "document_sha256": _sha(document_text.encode("utf-8")),
               "parameters_sha256": _sha(_json(parameters).encode("utf-8"))}
    replacements = {}
    additions = {}

    def append(index, pairs):
        additions[index] = additions.get(index, b"") + _pack(pairs)

    for handle in owned:
        record = drawing.handles[handle]
        replacements[record.start] = (record.end, b"")
    for pairs in entities:
        append(drawing.sections["ENTITIES"][1], pairs)
    for table, names in (("APPID", (("LEAFFRAMECELL", None), ("LEAFPILING", None))),
                         ("LAYER", (("LEAF-TRACKERS", 3), ("LEAF-PILING", 1)))):
        header, rows, end = drawing.tables[table]
        existing = {r.one(2).casefold() for r in rows}
        missing = [(name, color) for name, color in names if name.casefold() not in existing]
        if not missing:
            continue
        count_indexes = [header.start + i for i, p in enumerate(header.pairs) if p.code == 70]
        if len(count_indexes) != 1:
            _fail("SOURCE_UNSAFE")
        replacements[count_indexes[0]] = (count_indexes[0] + 1, _pack([(70, str(len(rows) + len(missing)))]))
        for name, color in missing:
            pairs = [(0, table), (5, drawing.fresh()), (330, header.one(5)),
                     (100, "AcDbSymbolTableRecord"),
                     (100, "AcDbRegAppTableRecord" if table == "APPID" else "AcDbLayerTableRecord"),
                     (2, name), (70, "0")]
            if table == "LAYER":
                normals = [r.one(390) for r in rows if r.one(390) is not None]
                if not normals:
                    _fail("SOURCE_UNSAFE")
                pairs += [(62, str(color)), (6, "Continuous"), (370, "-3"), (390, normals[0])]
            append(end, pairs)
    xhandle = old_record.one(5) if old_record else drawing.fresh()
    if old_record:
        replacements[old_record.start] = (old_record.end, b"")
    else:
        append(drawing.root.end, [(3, NAMESPACE), (350, xhandle)])
    text = _json(payload)
    append(drawing.sections["OBJECTS"][1], [(0, "XRECORD"), (5, xhandle), (330, drawing.root.one(5)),
           (100, "AcDbXrecord"), (280, "1"), (1, SCHEMA),
           *[(1, text[i:i + 255]) for i in range(0, len(text), 255)]])
    replacements[drawing.seed_index] = (drawing.seed_index + 1, _pack([(5, f"{drawing.next_handle:X}")]))
    result = []
    i = 0
    while i < len(drawing.pairs):
        result.append(additions.get(i, b""))
        if i in replacements:
            i, data = replacements[i]
            result.append(data)
        else:
            result.append(drawing.pairs[i].raw)
            i += 1
    output = b"".join(result)
    if len(output) > MAX_DXF_BYTES:
        _fail("LIMIT_EXCEEDED")
    return output
