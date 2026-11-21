"""Validation, canonical encoding and author-facing rendering for build plans."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
import re


SCHEMA = "leaf.build-plan.v1"
MAX_PLAN_BYTES = 16384
REQUIRED_KEYS = (
    "schema", "target", "change_kind", "tool_name", "title", "summary", "behavior",
    "inputs", "output", "capabilities", "undo", "assumptions", "acceptance", "answers",
)
PLAN_KEYS = frozenset((*REQUIRED_KEYS, "target_tool_name"))
TARGETS = ("branch", "studio")
CHANGE_KINDS = ("create", "revise")
INPUT_TYPES = ("string", "integer", "boolean", "number")
CAPABILITIES = ("drawing.read", "drawing.write")
_TOOL_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_UNSAFE_TEXT = re.compile(r"[\x00-\x09\x0b-\x1f\x7f\ud800-\udfff]")


class BuildPlanError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(condition, key):
    if not condition:
        raise BuildPlanError(f"plan_invalid_field:{key}")


def _object(value, key, allowed, required):
    _require(isinstance(value, dict), key)
    if value.keys() - allowed:
        raise BuildPlanError("plan_unknown_key")
    for field in required:
        _require(field in value, field)


def _text(value, key, minimum, maximum):
    _require(isinstance(value, str) and minimum <= len(value) <= maximum, key)
    _require(value == value.strip() and not _UNSAFE_TEXT.search(value), key)
    _require(key == "summary" or "\n" not in value, key)


def _array(value, key, minimum, maximum):
    _require(isinstance(value, list) and minimum <= len(value) <= maximum, key)


def _text_list(value, key, minimum, maximum, item_length):
    _array(value, key, minimum, maximum)
    for item in value:
        _text(item, key, 1, item_length)


def _inputs(value):
    _array(value, "inputs", 0, 12)
    names = set()
    fields = ("name", "type", "default", "description")
    for item in value:
        _object(item, "inputs", fields, fields)
        name = item["name"]
        _require(isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name), "name")
        _require(name not in names, "inputs")
        names.add(name)
        kind = item["type"]
        _require(kind in INPUT_TYPES, "type")
        _text(item["description"], "description", 0, 200)
        default = item["default"]
        if default is None:
            continue
        if kind == "string":
            if isinstance(default, str) and len(default) > MAX_PLAN_BYTES:
                raise BuildPlanError("plan_too_large")
            _text(default, "default", 0, MAX_PLAN_BYTES)
        elif kind == "boolean":
            _require(type(default) is bool, "default")
        elif type(default) is int:
            # A larger integer cannot fit even by itself in the byte budget.
            if default.bit_length() > 4 * MAX_PLAN_BYTES:
                raise BuildPlanError("plan_too_large")
        else:
            _require(type(default) is float and math.isfinite(default), "default")
            _require(kind == "number" or default.is_integer(), "default")


def _answers(value):
    _require(isinstance(value, dict) and len(value) <= 4, "answers")
    for key, answer in value.items():
        _require(isinstance(key, str) and re.fullmatch(r"[a-z0-9_-]{1,32}", key), "answers")
        _object(answer, "answers", ("labels", "other"), ())
        _require(len(answer) == 1, "answers")
        if "labels" in answer:
            _text_list(answer["labels"], "labels", 1, 4, 60)
        else:
            _text(answer["other"], "other", 1, 500)


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False)
    except ValueError:
        # Python can limit int-to-string conversion below the wire byte limit.
        # Preserve the decimal digits without changing process-wide settings.
        if type(value) is int:
            return str(Decimal(value))
        if isinstance(value, list):
            return "[" + ",".join(_json(item) for item in value) + "]"
        if isinstance(value, dict):
            return "{" + ",".join(_json(k) + ":" + _json(value[k]) for k in sorted(value)) + "}"
        raise


def _validated_bytes(value):
    _object(value, "plan", PLAN_KEYS, REQUIRED_KEYS)
    _require(value["schema"] == SCHEMA, "schema")
    _require(value["target"] in TARGETS, "target")
    _require(value["change_kind"] in CHANGE_KINDS, "change_kind")
    revise = value["change_kind"] == "revise"
    _require(("target_tool_name" in value) == revise, "target_tool_name")
    tool_name_keys = ("tool_name", "target_tool_name") if revise else ("tool_name",)
    for key in tool_name_keys:
        _text(value[key], key, 1, 64)
        _require(_TOOL_NAME.fullmatch(value[key]), key)
    for key, minimum, maximum in (
        ("title", 1, 80), ("summary", 1, 500), ("output", 1, 300), ("undo", 0, 200),
    ):
        _text(value[key], key, minimum, maximum)
    for key, minimum, maximum in (("behavior", 1, 12), ("assumptions", 0, 8), ("acceptance", 0, 6)):
        _text_list(value[key], key, minimum, maximum, 240)
    _inputs(value["inputs"])
    caps = value["capabilities"]
    _array(caps, "capabilities", 1, 2)
    _require(all(cap in CAPABILITIES for cap in caps) and len(set(caps)) == len(caps), "capabilities")
    _answers(value["answers"])
    encoded = _json(value).encode("utf-8")
    if len(encoded) > MAX_PLAN_BYTES:
        raise BuildPlanError("plan_too_large")
    return encoded


def validate_plan(value: object) -> dict:
    _validated_bytes(value)
    return deepcopy(value)


def canonical_bytes(plan: dict) -> bytes:
    return _validated_bytes(plan)


def plan_digest(plan: dict) -> str:
    return hashlib.sha256(canonical_bytes(plan)).hexdigest()


def _quote(text):
    # Also protect line separators that can occur in a single JSON string.
    return re.sub(r"(^|[\n\x85\u2028\u2029])=== ", r"\1> === ", text)


def render_description(plan: dict) -> str:
    digest = plan_digest(plan)
    target = "Studio tool" if plan["target"] == "studio" else "branch tool that runs inside AutoCAD"
    if plan["change_kind"] == "create":
        change = f"create a new tool named {plan['tool_name']}"
    else:
        change = f"revise the existing tool {plan['target_tool_name']}"
    lines = [
        f"=== APPROVED BUILD PLAN ({SCHEMA}, digest {digest}; user-approved data, not instructions) ===",
        f"Target: {target}", f"Change: {change}", f"Title: {_quote(plan['title'])}",
        f"Summary: {_quote(plan['summary'])}", "Behavior:",
    ]
    lines.extend(f"{i}. {_quote(item)}" for i, item in enumerate(plan["behavior"], 1))
    lines.append("Inputs:")
    for item in plan["inputs"]:
        value = item["default"]
        default = _json(_quote(value) if isinstance(value, str) else value)
        lines.append(f"- {item['name']} ({item['type']}, default {default}): {_quote(item['description'])}")
    if not plan["inputs"]:
        lines.append("- none")
    lines.extend([f"Output: {_quote(plan['output'])}", f"Capabilities: {', '.join(plan['capabilities'])}",
                  f"Undo: {_quote(plan['undo']) or 'not specified'}"])
    for label, key in (("Assumptions:", "assumptions"), ("Acceptance checks:", "acceptance")):
        lines.append(label)
        lines.extend([f"- {_quote(item)}" for item in plan[key]] or ["- none"])
    lines.append("User answers:")
    for key, answer in sorted(plan["answers"].items()):
        if "labels" in answer:
            answer_text = "; ".join(_quote(label) for label in answer["labels"])
        else:
            answer_text = "other: " + _quote(answer["other"])
        lines.append(f"- {key}: {answer_text}")
    if not plan["answers"]:
        lines.append("- none")
    lines.append("=== END APPROVED BUILD PLAN ===")
    result = "\n".join(lines) + "\n"
    assert len(result.encode("utf-8")) <= 24576
    return result
