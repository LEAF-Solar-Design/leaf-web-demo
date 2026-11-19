"""Portable data-plan oracle; no CAD, server imports, or executable plan input.

Parsed operations retain argument lexemes so decimal precision survives an exact
round trip. Validation is syntactic (the Branch parser contract); apply_ops models
the entity effects of da/apply_lisp.py against an explicit, ordered seed drawing.
Coordinates of polylines remain in OCS, and stored angles are radians, like DXF.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re

MAX_OPERATIONS = 20_000
MAX_POINTS = 500_000
REFUSAL_CODES = (
    "plan_op_not_supported", "plan_malformed", "plan_too_large",
    "plan_clearmodel_position", "plan_header_invalid", "plan_non_ascii",
)
OP_NAMES = (
    "ADD", "ADDOPEN", "ADDLINE", "ADDCIRCLE", "ADDARC", "ADDINSERT",
    "ADDDIMLINEAR", "ADDDIMALIGNED", "ADDMLEADER", "ADDBLOCKDEF",
    "BLOCKCHILD", "ADDGROUP", "REMOVEGROUP", "REMOVE", "RELAYER",
    "SETPOINTS", "SETCIRCLE", "SETARC", "TRANSFORM", "SETCOLOR",
    "SETLINETYPE", "SETLINEWEIGHT",
)
WEIGHTS = {-3, -2, -1, 0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40,
           50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211}


def _require(condition):
    if not condition:
        raise ValueError("malformed operation")


def _number(value):
    _require(len(value) <= 32 and re.fullmatch(
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value))
    result = float(value)
    _require(math.isfinite(result))
    return result


def _vector(value, dimensions=3, normal=False):
    values = value.split(",")
    _require(len(values) == dimensions)
    result = [_number(v) for v in values]
    _require(not normal or any(abs(v) > 1e-9 for v in result))
    return result


def _name(value, maximum=255):
    _require(0 < len(value) <= maximum and re.fullmatch(r"[A-Za-z0-9_. $-]+", value))
    return value


def _block_name(value):
    _require(0 < len(value) <= 255 and not value.startswith("*")
             and all(32 <= ord(c) <= 126 and c != "|" for c in value))
    return value


def _handle(value):
    _require(re.fullmatch(r"[0-9A-Fa-f]{1,16}", value))
    return value.upper()


def _target(value, child=False):
    _require(len(value) > 2 and value[1] == ":")
    if value[0] == "H":
        _handle(value[2:])
    else:
        _require(value[0] == ("C" if child else "A")
                 and re.fullmatch(r"[0-9]{1,6}", value[2:]))
    return value


def _group_name(value):
    _require(0 < len(value) <= 255 and value == value.strip()
             and all(32 <= ord(c) <= 126 and c not in '<>/\\":;?*|,=`' for c in value))
    return value


def _property(tag, value):
    if tag == "SETLINETYPE":
        return _name(value)
    number = _number(value)
    _require(number.is_integer())
    result = int(number)
    _require(0 <= result <= 256 if tag == "SETCOLOR" else result in WEIGHTS)
    return result


def _geometry(tag, a):
    """Return a DXF-like entity and the number of explicit geometry points."""
    _require(len(a) >= 1)
    layer = _name(a[0], 255 if tag in (
        "ADDINSERT", "ADDDIMLINEAR", "ADDDIMALIGNED", "ADDMLEADER") else 64)
    entity = {"kind": tag.removeprefix("ADD"), "layer": layer, "space": "Model"}
    if tag in ("ADD", "ADDOPEN"):
        _require(len(a) == 4)
        entity.update(kind="LWPOLYLINE", normal=_vector(a[1], normal=True),
                      elevation=_number(a[2]), closed=tag == "ADD")
        pts = [_vector(p, 2) for p in a[3].split(";")]
        _require(len(pts) >= (3 if tag == "ADD" else 2))
        entity["points"] = pts
        return entity, len(pts)
    if tag == "ADDLINE":
        _require(len(a) == 3)
        pts = [_vector(p) for p in a[1:]]
        _require(any(abs(x-y) > 1e-9 for x, y in zip(*pts)))
        entity["points"] = pts
        return entity, 2
    if tag in ("ADDCIRCLE", "ADDARC"):
        _require(len(a) == (3 if tag == "ADDCIRCLE" else 5))
        entity.update(center=_vector(a[1]), radius=_number(a[2]), normal=[0.0, 0.0, 1.0])
        _require(entity["radius"] > 0)
        if tag == "ADDARC":
            start, end = _number(a[3]), _number(a[4])
            _require(abs(start-end) > 1e-9)
            entity.update(start=math.radians(start), end=math.radians(end))
        return entity, 1
    if tag == "ADDINSERT":
        _require(len(a) == 5)
        scale = _vector(a[4])
        _require(all(v != 0 for v in scale))
        entity.update(name=_block_name(a[1]), point=_vector(a[2]),
                      rotation=math.radians(_number(a[3])), scale=scale)
        return entity, 1
    if tag in ("ADDDIMLINEAR", "ADDDIMALIGNED"):
        _require(len(a) == (6 if tag == "ADDDIMLINEAR" else 5))
        entity.update(kind="DIMENSION", style=_name(a[1]), def1=_vector(a[2]),
                      def2=_vector(a[3]), dimline=_vector(a[4]),
                      dimtype="LINEAR" if tag == "ADDDIMLINEAR" else "ALIGNED")
        if tag == "ADDDIMLINEAR":
            entity["rotation"] = math.radians(_number(a[5]))
        return entity, 3
    _require(tag == "ADDMLEADER" and len(a) == 5)
    _require(0 < len(a[4]) <= 4096 and all(ord(c) >= 32 for c in a[4]))
    entity.update(kind="MULTILEADER", style=_name(a[1]),
                  points=[_vector(a[2]), _vector(a[3])], text=a[4])
    return entity, 2


def _child(a):
    _require(len(a) >= 9)
    _block_name(a[0])
    _require(re.fullmatch(r"[0-9]{1,6}", a[1]))
    kind = a[2]
    if kind == "LWPOLYLINE":
        _require(a[4] in ("0", "1"))
        tag, args = ("ADD" if a[4] == "1" else "ADDOPEN"), [a[3], *a[5:-3]]
    else:
        _require(kind in ("LINE", "CIRCLE", "ARC"))
        tag, args = "ADD" + kind, [a[3], *a[4:-3]]
    entity, count = _geometry(tag, args)
    if kind == "LWPOLYLINE":
        _require(entity["normal"] == [0, 0, 1])
    entity.update(color=_property("SETCOLOR", a[-3]),
                  linetype=_property("SETLINETYPE", a[-2]),
                  lineweight=_property("SETLINEWEIGHT", a[-1]))
    return entity, count


def _validate(tag, a):
    if tag in ("ADD", "ADDOPEN", "ADDLINE", "ADDCIRCLE", "ADDARC",
               "ADDINSERT", "ADDDIMLINEAR", "ADDDIMALIGNED", "ADDMLEADER"):
        return _geometry(tag, a)[1]
    if tag == "BLOCKCHILD":
        return _child(a)[1]
    if tag == "ADDBLOCKDEF":
        _require(len(a) == 3)
        _block_name(a[0])
        _vector(a[1])
        refs = a[2].split(";")
        _require(1 <= len(refs) <= 60 and len(set(refs)) == len(refs))
        for ref in refs:
            _target(ref, child=True)
        return 1
    if tag in ("ADDGROUP", "REMOVEGROUP"):
        _require(len(a) == (2 if tag == "ADDGROUP" else 1))
        _group_name(a[0])
        if tag == "ADDGROUP":
            refs = a[1].split(";")
            _require(len(refs) >= 2)
            for ref in refs:
                _target(ref)
        return 0
    if tag in ("SETCOLOR", "SETLINETYPE", "SETLINEWEIGHT"):
        _require(len(a) == 2)
        _target(a[0])
        _property(tag, a[1])
        return 0
    if tag == "CLEARMODEL":
        _require(not a)
        return 0
    _require(bool(a))
    _handle(a[0])
    if tag == "REMOVE":
        _require(len(a) == 1)
        return 0
    if tag == "RELAYER":
        _require(len(a) == 2)
        _name(a[1], 64)
        return 0
    if tag in ("SETCIRCLE", "SETARC"):
        return _geometry("ADD" + tag[3:], ["0", *a[1:]])[1]
    if tag == "SETPOINTS":
        _require(len(a) == 5 and a[1] in ("0", "1"))
        return _geometry("ADD" if a[1] == "1" else "ADDOPEN", ["0", *a[2:]])[1]
    _require(tag == "TRANSFORM" and len(a) == 4)
    return _geometry("ADD", ["0", *a[1:]])[1]


def parse_plan(data: bytes):
    """Return ordered {op, args} records, or one of REFUSAL_CODES.

    Runtime symbol/handle existence is an apply concern, not a parse concern.
    The 20,000 operations and 500,000 explicit geometry points are inclusive.
    """
    raw = data.split(b"\n", 2)
    if len(raw) < 2 or raw[0] not in (
            b"LEAF_MUTATION_PLAN|1", b"LEAF_MUTATION_PLAN|2", b"LEAF_MUTATION_PLAN|3"):
        return "plan_header_invalid"
    if not re.fullmatch(rb"BASE_SHA256\|[0-9a-fA-F]{64}", raw[1]):
        return "plan_header_invalid"
    version = int(raw[0][-1:])
    if version < 3 and any(b > 127 for b in data):
        return "plan_non_ascii"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "plan_malformed"
    lines = text.split("\n")[2:]
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) > MAX_OPERATIONS:
        return "plan_too_large"
    operations, points = [], 0
    children, definitions, used_children = set(), set(), set()
    try:
        for index, line in enumerate(lines):
            tag, *args = line.split("|")
            if tag not in (*OP_NAMES, "CLEARMODEL"):
                return "plan_op_not_supported"
            if tag == "CLEARMODEL" and (version != 3 or index != 0):
                return "plan_clearmodel_position"
            points += _validate(tag, args)
            if points > MAX_POINTS:
                return "plan_too_large"
            if tag == "BLOCKCHILD":
                key = (args[0].upper(), int(args[1]))
                _require(key not in children and key[0] not in definitions)
                children.add(key)
            if tag == "ADDBLOCKDEF":
                name = args[0].upper()
                _require(name not in definitions)
                definitions.add(name)
                for ref in args[2].split(";"):
                    if ref.startswith("C:"):
                        key = (name, int(ref[2:]))
                        _require(key in children)
                        used_children.add(key)
            operations.append({"op": tag, "args": args})
        _require(children == used_children)
    except (ValueError, IndexError):
        return "plan_malformed"
    return operations


def serialize_plan(operations, *, version, base_sha256):
    """Serialize validated records without changing their numeric lexemes."""
    lines = [f"LEAF_MUTATION_PLAN|{version}", f"BASE_SHA256|{base_sha256}"]
    lines.extend("|".join([op["op"], *op["args"]]) for op in operations)
    return ("\n".join(lines) + "\n").encode("utf-8" if version == 3 else "ascii")


def initial_state():
    """Shared drawing: model poly/line/circle/arc, paper line, block and group."""
    rows = [
        ("A", "ADD", ["0", "0,0,1", "0", "0,0;2,0;2,2;0,2"]),
        ("B", "ADDLINE", ["0", "0,0,0", "2,0,0"]),
        ("C", "ADDCIRCLE", ["0", "1,1,0", "2"]),
        ("D", "ADDARC", ["0", "0,0,0", "3", "0", "90"]),
        ("E", "ADDLINE", ["0", "0,0,0", "1,0,0"]),
    ]
    entities = []
    for handle, tag, args in rows:
        entity = _geometry(tag, args)[0]
        entity["id"] = "H:" + handle
        entities.append(entity)
    entities[-1]["space"] = "Paper"
    return {"entities": entities, "groups": [{"name": "G0", "members": ["H:A", "H:B"]}],
            "blocks": [{"name": "SYMBOL", "base": [0, 0, 0], "entities": []}]}


def _world_points(points, normal, elevation):
    # AutoCAD arbitrary-axis algorithm, equivalent to (trans p normal 0).
    length = math.sqrt(sum(v*v for v in normal))
    n = [v / length for v in normal]
    axis = [0, 1, 0] if abs(n[0]) < 1/64 and abs(n[1]) < 1/64 else [0, 0, 1]
    def cross(a, b):
        return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]
    x = cross(axis, n)
    length = math.sqrt(sum(v*v for v in x))
    x = [v / length for v in x]
    y = cross(n, x)
    return [[p[0]*x[i]+p[1]*y[i]+elevation*n[i] for i in range(3)] for p in points]


def apply_ops(operations, state=None):
    """Apply records in order; stable H:/A: ids replace nondeterministic handles.

    BLOCKCHILD declares geometry only; ADDBLOCKDEF copies members into a block
    without deleting the source entities. REMOVEGROUP deletes only the group.
    Repeated removal is idempotent here, as needed for the syntax cap vectors.
    """
    result = deepcopy(initial_state() if state is None else state)
    entities = {e["id"]: e for e in result["entities"]}
    children, created = {}, []
    for op in operations:
        tag, a = op["op"], op["args"]
        if tag in ("ADD", "ADDOPEN", "ADDLINE", "ADDCIRCLE", "ADDARC", "ADDINSERT",
                   "ADDDIMLINEAR", "ADDDIMALIGNED", "ADDMLEADER"):
            entity = _geometry(tag, a)[0]
            key = f"A:{len(created)}"
            created.append(key)
            entity["id"] = key
            entities[key] = entity
        elif tag == "CLEARMODEL":
            entities = {k: e for k, e in entities.items() if e["space"] != "Model"}
            for group in result["groups"]:
                group["members"] = [m for m in group["members"] if m in entities]
        elif tag == "BLOCKCHILD":
            children[(a[0].upper(), int(a[1]))] = _child(a)[0]
        elif tag == "ADDBLOCKDEF":
            members = []
            for ref in a[2].split(";"):
                member = deepcopy(entities[ref] if ref.startswith("H:") else
                                  children[(a[0].upper(), int(ref[2:]))])
                member.pop("id", None)
                member.pop("space", None)
                members.append(member)
            result["blocks"].append({"name": a[0], "base": _vector(a[1]), "entities": members})
        elif tag == "ADDGROUP":
            result["groups"].append({"name": a[0], "members": a[1].split(";")})
        elif tag == "REMOVEGROUP":
            result["groups"] = [g for g in result["groups"] if g["name"] != a[0]]
        elif tag == "REMOVE":
            key = "H:" + a[0].upper()
            entities.pop(key, None)
            for group in result["groups"]:
                group["members"] = [m for m in group["members"] if m != key]
        elif tag.startswith("SET") and tag in ("SETCOLOR", "SETLINETYPE", "SETLINEWEIGHT"):
            entity = entities[a[0]]
            field = {"SETCOLOR": "color", "SETLINETYPE": "linetype", "SETLINEWEIGHT": "lineweight"}[tag]
            if tag == "SETCOLOR":
                entity.pop("true_color", None)
                entity.pop("color_name", None)
            entity[field] = _property(tag, a[1])
        else:
            entity = entities["H:" + a[0].upper()]
            if tag == "RELAYER":
                entity["layer"] = a[1]
            elif tag in ("SETCIRCLE", "SETARC"):
                geometry = _geometry("ADD" + tag[3:], [entity["layer"], *a[1:]])[0]
                geometry.pop("normal")  # setters preserve the existing normal
                entity.update(geometry)
            else:
                closed = a[1] == "1" if tag == "SETPOINTS" else entity["closed"]
                geometry = _geometry("ADD" if closed else "ADDOPEN",
                                     [entity["layer"], *a[2 if tag == "SETPOINTS" else 1:]])[0]
                if entity["kind"] == "LINE":
                    entity["points"] = _world_points(geometry["points"], geometry["normal"], geometry["elevation"])
                else:
                    if tag == "SETPOINTS" and len(entity["points"]) != len(geometry["points"]):
                        for field in ("bulges", "start_widths", "end_widths"):
                            entity.pop(field, None)
                    entity.update(geometry)
    result["entities"] = list(entities.values())
    return result


def generate_case(descriptor):
    """Materialize a point-cap vector without committing megabytes of vertices."""
    if descriptor.get("generator") != "points":
        raise ValueError("unknown case generator")
    count, per_op, layer = descriptor["points"], descriptor["per_op"], descriptor["layer"]
    _require(type(count) is int and count >= 2 and type(per_op) is int and per_op >= 3)
    _name(layer, 64)
    lines = ["LEAF_MUTATION_PLAN|3", "BASE_SHA256|" + "0" * 64]
    while count:
        size = min(count, per_op)
        # Avoid an invalid one-point tail at the upper boundary.
        if count - size == 1:
            size -= 1
        _require(size >= 2)
        points = ";".join(f"{i % 2},0" for i in range(size))
        lines.append(f"ADDOPEN|{layer}|0,0,1|0|{points}")
        count -= size
    return ("\n".join(lines) + "\n").encode("ascii")
