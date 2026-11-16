"""Read-only plotting-layer diagnostics for tiny synthetic ASCII DXF drawings.

Usage (from the repository root)::

    python tools/xbench/x01_offline/check_plot_layers.py INPUT.dxf
    python tools/xbench/x01_offline/check_plot_layers.py INPUT.dxf --module-layer MODULES --tag-layer TAGS
    python tools/xbench/x01_offline/check_plot_layers.py --self-test

This is not a full DXF reader, a bench grade, or a readiness check. Only ASCII
code/value records, LAYER tables, ordinary BLOCK/INSERT trees and modelspace
ENTITIES are supported. No geometry transforms, viewport overrides, external
references, attributes, or rendering are evaluated. Unsupported modelspace
entity types are unreadable. Bounds also apply to expanded occurrences.
Occurrence paths are arrays of INSERT handles followed by the leaf handle;
inspected_occurrences counts classified module/tag leaves, including repeats.
"""

import argparse
import json
import math
import re
import sys
import unittest


SCHEMA = "leaf.xbench.x01.offline-layers.v1"
MAX_BYTES = 1_048_576
MAX_PAIRS = 50_000
MAX_RECORDS = 20_000
MAX_OCCURRENCES = 100_000
MAX_DEPTH = 64
GEOMETRY = frozenset({"LINE", "LWPOLYLINE", "POLYLINE", "VERTEX", "POINT",
                      "CIRCLE", "ARC", "ELLIPSE", "SPLINE", "SOLID", "TRACE",
                      "3DFACE", "HATCH", "RAY", "XLINE"})
TEXT = frozenset({"TEXT", "MTEXT"})


def diagnostic(reason, handle=None, path=(), layer=None, role=None, **details):
    return dict(handle=handle, occurrence_path=list(path), effective_layer=layer,
                role=role, reason=reason, **details)


class Unreadable(ValueError):
    def __init__(self, reason, **context):
        self.diagnostic = diagnostic(reason, **context)
        super().__init__(reason)


def number(code, value):
    """Validate DXF's numeric group families even when unused by this checker."""
    integer = (60 <= code <= 99 or 160 <= code <= 179 or
               270 <= code <= 299 or 370 <= code <= 389 or
               400 <= code <= 409 or 420 <= code <= 429 or
               440 <= code <= 459 or 1060 <= code <= 1071)
    real = (10 <= code <= 59 or 110 <= code <= 149 or
            210 <= code <= 239 or 460 <= code <= 469 or
            1010 <= code <= 1059)
    if integer:
        if not re.fullmatch(r"[+-]?[0-9]+", value):
            raise ValueError("invalid integer")
        result = int(value)
        if 290 <= code <= 299 and result not in (0, 1):
            raise ValueError("invalid boolean")
        return result
    if real:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite real")
        return result
    return value


def records(data):
    if not data:
        raise Unreadable("empty_source")
    if len(data) > MAX_BYTES:
        raise Unreadable("byte_limit")
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeDecodeError:
        raise Unreadable("non_ascii_source") from None
    if len(lines) % 2:
        raise Unreadable("malformed_group_pair")
    if len(lines) // 2 > MAX_PAIRS:
        raise Unreadable("pair_limit")
    result = []
    for index in range(0, len(lines), 2):
        raw, value = lines[index].strip(), lines[index + 1].strip()
        if not re.fullmatch(r"[0-9]{1,4}", raw) or int(raw) > 1071:
            raise Unreadable("invalid_group_code", line=index + 1)
        code = int(raw)
        if code == 0:
            if not value:
                raise Unreadable("empty_record_type", line=index + 2)
            result.append((value.upper(), {}))
            if len(result) > MAX_RECORDS:
                raise Unreadable("record_limit")
        elif not result:
            raise Unreadable("missing_record_start", line=index + 1)
        else:
            fields = result[-1][1]
            try:
                number(code, value)
            except (ValueError, OverflowError):
                raise Unreadable("malformed_numeric", handle=fields.get(5, [None])[0],
                                 layer=fields.get(8, [None])[0], group=code,
                                 line=index + 2) from None
            fields.setdefault(code, []).append(value)
    return result


def field(record, code, default=None, required=False):
    values = record[1].get(code, [])
    if len(values) > 1 or (required and (not values or not values[0])):
        raise Unreadable("invalid_required_field", group=code,
                         handle=record[1].get(5, [None])[0])
    return values[0] if values else default


def parse(data):
    source = records(data)
    sections = {}
    index = 0
    while index < len(source) and source[index][0] == "SECTION":
        name = field(source[index], 2, required=True).upper()
        if name in sections:
            raise Unreadable("duplicate_section", section=name)
        index += 1
        start = index
        while index < len(source) and source[index][0] != "ENDSEC":
            if source[index][0] in {"SECTION", "EOF"}:
                raise Unreadable("unclosed_section", section=name)
            index += 1
        if index == len(source):
            raise Unreadable("unclosed_section", section=name)
        sections[name] = source[start:index]
        index += 1
    if index != len(source) - 1 or source[index][0] != "EOF" or source[index][1]:
        raise Unreadable("invalid_dxf_envelope")
    if "TABLES" not in sections or "ENTITIES" not in sections:
        raise Unreadable("missing_required_section")
    layers = {}
    table = None
    for record in sections["TABLES"]:
        kind = record[0]
        if kind == "TABLE" and table is None:
            table = field(record, 2, required=True).upper()
        elif kind == "ENDTAB" and table is not None:
            table = None
        elif table is None or kind in {"TABLE", "ENDTAB"}:
            raise Unreadable("malformed_table")
        elif table == "LAYER":
            if kind != "LAYER":
                raise Unreadable("malformed_layer_table")
            name = field(record, 2, required=True)
            key = name.casefold()
            if key in layers:
                raise Unreadable("duplicate_layer", layer=name)
            flags = int(field(record, 70, "0"))
            color = int(field(record, 62, "7"))
            if flags < 0 or not 1 <= abs(color) <= 255:
                raise Unreadable("invalid_layer_flags", layer=name)
            reasons = []
            if color < 0:
                reasons.append("layer_off")
            if flags & 1:
                reasons.append("layer_frozen")
            if int(field(record, 290, "1")) == 0:
                reasons.append("layer_nonplot")
            layers[key] = (name, reasons)
    if table is not None:
        raise Unreadable("unclosed_table")
    if not layers:
        raise Unreadable("missing_layer_table")
    blocks = {}
    current = None
    for record in sections.get("BLOCKS", []):
        kind = record[0]
        if kind == "BLOCK" and current is None:
            current = field(record, 2, required=True).casefold()
            if current in blocks:
                raise Unreadable("duplicate_block", block=current)
            if int(field(record, 70, "0")) & 12:
                raise Unreadable("unsupported_external_block", block=current)
            blocks[current] = []
        elif kind == "ENDBLK" and current is not None:
            current = None
        elif current is None or kind in {"BLOCK", "ENDBLK"}:
            raise Unreadable("malformed_blocks")
        else:
            blocks[current].append(record)
    if current is not None:
        raise Unreadable("unclosed_block", block=current)
    return layers, blocks, sections["ENTITIES"]


def check_bytes(data, module_layers=None, tag_layers=None):
    modules = {name.casefold() for name in (module_layers if module_layers is not None else ["MODULES"])}
    tags = {name.casefold() for name in (tag_layers if tag_layers is not None else ["TAGS"])}
    diagnostics = []
    inspected = 0
    visited = 0
    try:
        layers, blocks, entities = parse(data)

        def walk(items, inherited="0", path=(), active=(), ancestor_reasons=()):
            nonlocal inspected, visited
            if len(path) > MAX_DEPTH:
                raise Unreadable("depth_limit", path=path)
            for record in items:
                visited += 1
                if visited > MAX_OCCURRENCES:
                    raise Unreadable("occurrence_limit", path=path)
                kind = record[0]
                handle = field(record, 5)
                raw_layer = field(record, 8, "0")
                effective = inherited if raw_layer.casefold() == "0" else raw_layer
                key = effective.casefold()
                role = "tag" if kind in TEXT and key in tags else (
                    "module" if kind in GEOMETRY and key in modules else None)
                occurrence = path + (handle or "<missing-handle>",)
                context = dict(handle=handle, path=occurrence, layer=effective, role=role)
                space = int(field(record, 67, "0"))
                hidden = int(field(record, 60, "0"))
                if space not in (0, 1) or hidden not in (0, 1):
                    raise Unreadable("invalid_entity_flags", **context)
                if space == 1:
                    continue
                if not handle:
                    raise Unreadable("missing_entity_handle", **context)
                if key not in layers:
                    raise Unreadable("unknown_layer", **context)
                effective, layer_reasons = layers[key]
                context["layer"] = effective
                reasons = tuple(ancestor_reasons) + tuple(layer_reasons)
                if hidden:
                    reasons += ("entity_hidden",)
                if kind == "INSERT":
                    name = field(record, 2, required=True).casefold()
                    if name not in blocks:
                        raise Unreadable("missing_block", block=name, **context)
                    if name in active:
                        raise Unreadable("cyclic_block", block=name, **context)
                    # Array INSERTs require separate occurrence identifiers; refuse them.
                    if int(field(record, 70, "1")) != 1 or int(field(record, 71, "1")) != 1:
                        raise Unreadable("unsupported_array_insert", **context)
                    walk(blocks[name], effective, occurrence, active + (name,), reasons)
                elif role is not None:
                    inspected += 1
                    for reason in dict.fromkeys(reasons):
                        diagnostics.append(diagnostic(reason, **context))
                elif kind not in GEOMETRY | TEXT | {"SEQEND"}:
                    raise Unreadable("unsupported_entity", entity_type=kind, **context)

        walk(entities)
        if inspected == 0:
            raise Unreadable("no_classified_entities")
    except Unreadable as error:
        diagnostics.append(error.diagnostic)
        verdict = "UNREADABLE"
    else:
        verdict = "FAIL" if diagnostics else "PASS"
    return dict(schema=SCHEMA, verdict=verdict,
                inspected_occurrences=inspected, diagnostics=diagnostics)


def check_file(path, module_layers=None, tag_layers=None):
    try:
        with open(path, "rb") as source:
            data = source.read(MAX_BYTES + 1)
    except OSError as error:
        return dict(schema=SCHEMA, verdict="UNREADABLE", inspected_occurrences=0,
                    diagnostics=[diagnostic("source_unavailable", detail=str(error))])
    return check_bytes(data, module_layers, tag_layers)


def fixture(layers=(), entities=(), blocks=()):
    """Construct tiny pair strings in memory, never customer fixture files."""
    pairs = [(0, "SECTION"), (2, "TABLES"), (0, "TABLE"), (2, "LAYER")]
    definitions = {"0": (), "MODULES": (), "TAGS": ()}
    definitions.update(layers)
    for name, extra in definitions.items():
        pairs.extend([(0, "LAYER"), (2, name)] + list(extra))
    pairs.extend([(0, "ENDTAB"), (0, "ENDSEC"), (0, "SECTION"), (2, "BLOCKS")])
    for name, children in blocks:
        pairs.extend([(0, "BLOCK"), (2, name)])
        for child in children:
            pairs.extend(child)
        pairs.append((0, "ENDBLK"))
    pairs.extend([(0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES")])
    for entity in entities:
        pairs.extend(entity)
    pairs.extend([(0, "ENDSEC"), (0, "EOF")])
    return "".join(f"{code}\n{value}\n" for code, value in pairs).encode("ascii")


def entity(handle="A", layer="MODULES", kind="LINE", extra=()):
    return [(0, kind), (5, handle), (8, layer)] + list(extra)


class SelfTests(unittest.TestCase):
    def assert_issue(self, result, verdict, handle, path, layer, role, reason):
        self.assertEqual(result["verdict"], verdict)
        self.assertEqual(result["diagnostics"], [
            diagnostic(reason, handle, path, layer, role)])

    def test_layer_flags(self):
        for flags, reason in [(((62, -7),), "layer_off"),
                              (((70, 1),), "layer_frozen"),
                              (((290, 0),), "layer_nonplot")]:
            with self.subTest(reason=reason):
                result = check_bytes(fixture([("MODULES", flags)], [entity()]))
                self.assert_issue(result, "FAIL", "A", ["A"], "MODULES", "module", reason)
        for flags in [(), ((290, 1),), ((70, 2),), ((62, 7),)]:
            with self.subTest(flags=flags):
                self.assertEqual(check_bytes(fixture([("VISIBLE", flags)],
                    [entity(layer="VISIBLE")]), ["VISIBLE"])["verdict"], "PASS")

    def test_tags_and_case(self):
        for kind in ("TEXT", "MTEXT"):
            result = check_bytes(fixture([("TAGS", ((290, 0),))],
                                         [entity("B", "tAgS", kind)]))
            self.assert_issue(result, "FAIL", "B", ["B"], "TAGS", "tag", "layer_nonplot")
        self.assertEqual(check_bytes(fixture(entities=[entity(layer="mOdUlEs")]))["verdict"], "PASS")

    def test_custom_layers_and_tag_inheritance(self):
        result = check_bytes(fixture([("LABELS", ((290, 0),))],
            [entity("10", "LABELS", "INSERT", [(2, "B")])],
            [("B", [entity("A", "0", "MTEXT")])]),
            ["PANELS", "MODULES"], ["LABELS", "TAGS"])
        self.assert_issue(result, "FAIL", "A", ["10", "A"], "LABELS", "tag", "layer_nonplot")

    def test_signed_nonplot_flag(self):
        result = check_bytes(fixture([("MODULES", ((290, "+0"),))], [entity()]))
        self.assert_issue(result, "FAIL", "A", ["A"], "MODULES", "module", "layer_nonplot")

    def test_layer_zero_and_reuse(self):
        blocks = [("B", [entity("A", "0")])]
        result = check_bytes(fixture([("BAD", ((62, -7),))], [
            entity("10", "MODULES", "INSERT", [(2, "B")]),
            entity("11", "BAD", "INSERT", [(2, "b")])], blocks), ["MODULES", "BAD"])
        self.assertEqual(result["inspected_occurrences"], 2)
        self.assert_issue(result, "FAIL", "A", ["11", "A"], "BAD", "module", "layer_off")

    def test_nested_inheritance_and_explicit_child(self):
        for child_layer in ("0", "MODULES"):
            blocks = [("B", [entity("A", child_layer)]),
                      ("OUTER", [entity("12", "0", "INSERT", [(2, "B")])])]
            result = check_bytes(fixture([("BAD", ((70, 1),))],
                [entity("11", "BAD", "INSERT", [(2, "OUTER")])], blocks), ["MODULES", "BAD"])
            self.assert_issue(result, "FAIL", "A", ["11", "12", "A"],
                              "BAD" if child_layer == "0" else "MODULES", "module", "layer_frozen")

    def test_hidden_and_paperspace(self):
        result = check_bytes(fixture(entities=[entity(extra=[(60, 1)])]))
        self.assert_issue(result, "FAIL", "A", ["A"], "MODULES", "module", "entity_hidden")
        result = check_bytes(fixture(entities=[entity(), entity("B", "UNKNOWN", extra=[(67, 1)])]))
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["inspected_occurrences"], 1)
        result = check_bytes(fixture(entities=[entity("10", "MODULES", "INSERT", [(2, "B"), (60, 1)])],
                                     blocks=[("B", [entity("A", "0")])]))
        self.assert_issue(result, "FAIL", "A", ["10", "A"], "MODULES", "module", "entity_hidden")

    def test_unknown_layer(self):
        result = check_bytes(fixture(entities=[entity(layer="UNKNOWN")]))
        self.assert_issue(result, "UNREADABLE", "A", ["A"], "UNKNOWN", None, "unknown_layer")

    def test_missing_and_cyclic_blocks(self):
        result = check_bytes(fixture(entities=[entity("10", "MODULES", "INSERT", [(2, "B")])]))
        self.assertEqual(result["verdict"], "UNREADABLE")
        self.assertEqual(result["diagnostics"], [diagnostic("missing_block", "10", ["10"], "MODULES", block="b")])
        result = check_bytes(fixture(entities=[entity("10", "MODULES", "INSERT", [(2, "B")])],
            blocks=[("B", [entity("11", "0", "INSERT", [(2, "B")])])]))
        self.assertEqual(result["verdict"], "UNREADABLE")
        self.assertEqual(result["diagnostics"], [diagnostic("cyclic_block", "11", ["10", "11"], "MODULES", block="b")])

    def test_no_classified_entities(self):
        for entities in ([], [entity(layer="0")], [entity(extra=[(67, 1)])]):
            self.assert_issue(check_bytes(fixture(entities=entities)), "UNREADABLE",
                              None, [], None, None, "no_classified_entities")

    def test_malformed(self):
        cases = [(b"", diagnostic("empty_source")),
                 (b"hello\nworld\n", diagnostic("invalid_group_code", line=1)),
                 (b"0\nSECTION\n2\n", diagnostic("malformed_group_pair")),
                 (b"0\nEOF\n", diagnostic("missing_required_section")),
                 (b"0\nSECTION\n2\nENTITIES\n", diagnostic("unclosed_section", section="ENTITIES"))]
        for data, expected in cases:
            result = check_bytes(data)
            self.assertEqual(result["verdict"], "UNREADABLE")
            self.assertEqual(result["diagnostics"], [expected])
        for code, value in [(60, "bad"), (10, "nan"), (70, "1.5"), (290, "2")]:
            data = fixture(entities=[entity(extra=[(code, value)])])
            result = check_bytes(data)
            lines = data.decode().splitlines()
            value_line = next(index + 2 for index in range(0, len(lines), 2)
                              if lines[index:index + 2] == [str(code), value])
            self.assertEqual(result["verdict"], "UNREADABLE")
            self.assertEqual(result["diagnostics"], [diagnostic("malformed_numeric", "A", layer="MODULES", group=code,
                line=value_line)])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?")
    parser.add_argument("--module-layer", action="append")
    parser.add_argument("--tag-layer", action="append")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(SelfTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    if args.input is None:
        result = dict(schema=SCHEMA, verdict="UNREADABLE", inspected_occurrences=0,
                      diagnostics=[diagnostic("missing_source")])
    else:
        result = check_file(args.input, args.module_layer, args.tag_layer)
    print(json.dumps(result, sort_keys=True))
    return {"PASS": 0, "FAIL": 1, "UNREADABLE": 4}[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
