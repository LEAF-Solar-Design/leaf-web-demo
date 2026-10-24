"""Closed widget admission with the renderer's semantic constraints."""
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry

from lab_canonical import canonical_bytes
from lab_doc_validators import InvalidGraph, TooLarge, MAX_GRAPH_BYTES, _depth
from lab_validate import _pointer

CONTRACT = Path(__file__).resolve().parents[1] / "contract"
SCHEMA = json.loads((CONTRACT / "widget-doc.v1.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, registry=Registry())
WIDTHS = dict(zip(
    ("la", "diExp", "diCompact", "diMin", "standby", "lockRect", "lockCirc",
     "lockInline", "homeS", "homeM", "homeL", "watchStack", "watchCorner"),
    (361, 371, 250, 37, 852, 170, 72, 234, 170, 364, 364, 188, 62)))
RENDERER = (1, 0, 0)


class InvalidWidget(InvalidGraph):
    def __init__(self, reason="invalid_widget_document", pointer=""):
        super().__init__(reason, pointer)
        self.code = "invalid_widget_document"
        self.reason = reason


def require_renderer(version, pointer=""):
    if tuple(map(int, version.split("."))) > RENDERER:
        raise InvalidWidget("Unsupported min_renderer.", pointer)


def _schema_failure(error, pointer="", *, binding_error=False):
    """Keep renderer rejection reasons while retaining schema-owned pointers."""
    path = list(error.absolute_path)
    field = path[-1] if path else None
    if error.validator == "oneOf" and isinstance(error.instance, dict):
        binding = error.instance
        branch = "graphBinding" if binding.get("source") == "graph.output" else "boardBinding"
        try:
            Draft202012Validator({"$ref": f"#/$defs/{branch}", "$defs": SCHEMA["$defs"]},
                                 registry=Registry()).validate(binding)
        except ValidationError as nested:
            failure = _schema_failure(nested, pointer + _pointer(path), binding_error=True)
            if (binding.get("source") == "kashbot.board" and binding.get("field") == "winRate"
                    and binding.get("window") != "today-utc"):
                return InvalidWidget("Win rate requires the today-utc window field.", failure.pointer)
            return failure
    reason = "Unsupported document field."
    if field == "kind":
        reason = "Unknown primitive kind."
    elif field in ("scale", "tone") and (binding_error or "binding" in path):
        reason = "Invalid binding scale or tone."
    elif field in ("x", "y", "w", "h", "scale"):
        reason = "Numeric value outside bounds."
    elif field in ("weight", "color", "align", "visible"):
        reason = "Invalid primitive style."
    elif field == "count" and error.validator == "maximum" and error.validator_value == 4:
        reason = "Ledger supports at most 4 rows."
    elif field == "min_renderer":
        reason = "Invalid renderer version."
    elif field in ("source", "doc_id", "output", "field"):
        reason = "Unknown binding."
    elif field == "window":
        reason = "Unknown window field."
    return InvalidWidget(reason, pointer + _pointer(path))


def validate_binding(binding, pointer=""):
    try:
        Draft202012Validator({"$ref": "#/$defs/binding", "$defs": SCHEMA["$defs"]},
                             registry=Registry()).validate(binding)
    except ValidationError as error:
        raise _schema_failure(error, pointer, binding_error=True) from error
    require_renderer(binding["min_renderer"], pointer + "/min_renderer")
    return binding


def validate_widget(doc):
    try:
        try:
            _depth(doc)
        except InvalidGraph as error:
            raise InvalidWidget("Widget document exceeds maximum depth.", error.pointer) from error
        data = canonical_bytes(doc)
        if len(data) > MAX_GRAPH_BYTES:
            raise TooLarge("widget_too_large")
        VALIDATOR.validate(doc)
        require_renderer(doc["min_renderer"], "/min_renderer")
        if doc["active"] not in doc["formats"]:
            raise InvalidWidget("Unknown active format.", "/active")
        for name, surface in doc["formats"].items():
            seen = set()
            for index, element in enumerate(surface["els"]):
                pointer = _pointer(("formats", name, "els", index))
                if element["id"] in seen:
                    raise InvalidWidget("Invalid or duplicate element ID.", pointer + "/id")
                seen.add(element["id"])
                require_renderer(element["min_renderer"], pointer + "/min_renderer")
                if (element["x"] + element["w"] > WIDTHS[name]
                        or element["y"] + element["h"] > surface["h"]):
                    raise InvalidWidget("Geometry outside surface.", pointer)
                if element.get("preset") in ("history", "historyList") and element.get("count", 0) > 4:
                    raise InvalidWidget("Ledger supports at most 4 rows.", pointer + "/count")
                if "text" in element and len(element["text"].encode("utf-16-le")) // 2 > 512:
                    raise InvalidWidget("Invalid text.", pointer + "/text")
                if "binding" in element:
                    validate_binding(element["binding"], pointer + "/binding")
        return data
    except ValidationError as error:
        raise _schema_failure(error) from error
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as error:
        if isinstance(error, (InvalidWidget, TooLarge)):
            raise
        raise InvalidWidget() from error


validate_widget_doc = validate_widget
