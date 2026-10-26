"""Mirror of web/src/lab/runtime/evaluate.js for pure graphs.

Tool nodes run only server-side through the adapter; the browser mirror never
executes them. The shared pure graph fixtures are unchanged. Change pure
evaluation in both runtimes together and extend the shared fixtures.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, fields
from functools import lru_cache
import json
import math
from pathlib import Path
import time
from types import MappingProxyType

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from lab_canonical import canonical_json
from lab_doc_validators import validate_graph
import lab_registry
from lab_ops import MAX_ROWS, STREAM_ADAPTERS, has_operator, run_operator


class ValueRefused(ValueError):
    pass


class GraphRefused(ValueError):
    def __init__(self, code, block_id=None):
        self.code, self.block_id = code, block_id
        super().__init__(code)


class RunBudgetExceeded(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


def _immutable(*args, **kwargs):
    raise TypeError("immutable runtime data")


class _List(list):
    __setitem__ = __delitem__ = __iadd__ = __imul__ = _immutable
    append = extend = insert = pop = remove = clear = reverse = sort = _immutable


class _Dict(dict):
    __setitem__ = __delitem__ = __ior__ = _immutable
    clear = pop = popitem = setdefault = update = _immutable


def _copy(data, *, bounded=False, frozen=False, flags=None):
    if isinstance(data, list):
        if bounded and len(data) > MAX_ROWS:
            flags["truncated"] = True
        children = [_copy(child, bounded=bounded, frozen=frozen, flags=flags)
                    for child in (data[:MAX_ROWS] if bounded else data)]
        return _List(children) if frozen else children
    if isinstance(data, dict):
        children = {key: _copy(value, bounded=bounded, frozen=frozen, flags=flags)
                    for key, value in data.items()}
        return _Dict(children) if frozen else children
    if type(data) is int and abs(data) > 2 ** 53:
        return float(data)
    return data


@dataclass(frozen=True)
class RuntimeValue:
    data: object
    mode: str = "scalar"
    as_of: float | None = None
    host_tainted: bool = False
    simulated: bool = False
    stale: bool = False
    truncated: bool = False
    error: str | None = None


def runtime_value(data, *, mode="scalar", as_of=None, host_tainted=False,
                  simulated=False, stale=False, truncated=False, error=None):
    try:
        canonical_json(data)
        if mode not in ("scalar", "stream") or (mode == "stream" and not isinstance(data, list)):
            raise ValueError()
        if as_of is not None and (type(as_of) not in (int, float)
                                  or not math.isfinite(as_of) or as_of < 0):
            raise ValueError()
        flags = {"truncated": truncated is True}
        snapshot = _copy(data, bounded=True, frozen=True, flags=flags)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ValueRefused("lab_value_refused") from exc
    if isinstance(error, dict):
        code = error.get("code")
        error = code if isinstance(code, str) and code else "evaluation-failed"
    elif isinstance(error, list):
        # JavaScript arrays are truthy even when empty and have no code.
        error = "evaluation-failed"
    elif isinstance(error, float) and math.isnan(error):
        error = None
    elif error:
        error = error if isinstance(error, str) else "evaluation-failed"
    else:
        error = None
    return RuntimeValue(snapshot, mode, as_of, host_tainted is True, simulated is True,
                        stale is True, flags["truncated"], error)


def _require(value):
    if not isinstance(value, RuntimeValue):
        raise ValueRefused("lab_runtime_value_required")
    return value


def export_value(value):
    _require(value)
    if value.host_tainted:
        raise ValueRefused("lab_host_value_export")
    return {"data": _copy(value.data), "mode": value.mode, "as_of": value.as_of,
            "host_tainted": value.host_tainted, "simulated": value.simulated,
            "stale": value.stale, "truncated": value.truncated,
            "error": {"code": value.error} if value.error else None}


def inherited(values):
    values = list(values)
    dated = [value.as_of for value in values if value.as_of is not None]
    undated_host = any(value.host_tainted and value.as_of is None for value in values)
    return {"as_of": None if undated_host or not dated else min(dated),
            **{key: any(getattr(value, key) for value in values)
               for key in ("host_tainted", "simulated", "stale", "truncated")}}


def evaluate_operation(op_id, inputs=None, settings=None, *, output_mode=None):
    inputs = {} if inputs is None else inputs
    values = [_require(value) for value in inputs.values()]
    metadata = inherited(values)
    try:
        if not has_operator(op_id):
            raise ValueError()
        if any(value.error for value in values):
            return runtime_value(None, **metadata, error="upstream-failed")
        adapter = op_id in STREAM_ADAPTERS
        if any(value.mode == "stream" for value in values) and not adapter and output_mode != "stream":
            return runtime_value(None, **metadata, error="stream-adapter-required")
        data = run_operator(op_id, {key: value.data for key, value in inputs.items()},
                            {} if settings is None else settings)
        return runtime_value(data, **metadata, mode="scalar" if adapter else output_mode or "scalar")
    except Exception:
        # Operator messages and input values never become diagnostics.
        return runtime_value(None, **metadata, error="operator-refused")


@lru_cache(maxsize=1)
def trusted_registry():
    root = Path(__file__).resolve().parents[1] / "contract/fixtures/lab"
    document = json.loads((root / "blocks.json").read_text(encoding="utf-8"))
    if (not isinstance(document, dict)
            or document.get("schema") != "leaf.lab-registry-fixtures.v1"
            or not isinstance(document.get("source"), dict)
            or not isinstance(document.get("manifests"), list)):
        raise ValueError("lab_registry_invalid")
    validator = Draft202012Validator(json.loads(
        (root / "leaf-block-manifest.v1.schema.json").read_text(encoding="utf-8")))
    registry = {}
    for manifest in document["manifests"]:
        validator.validate(manifest)
        canonical_json(manifest)
        key = f'{manifest["id"]}@{manifest["version"]["semver"]}'
        if key in registry:
            raise ValueError("lab_registry_duplicate")
        registry[key] = _copy(manifest, frozen=True)
    return MappingProxyType(registry)


@lru_cache(maxsize=1)
def _contract_validators():
    root = Path(__file__).resolve().parents[1] / "contract"
    names = ("lab-block.v1.schema.json", "lab-pin.v1.schema.json",
             "lab-graph.v1.schema.json", "fixtures/lab/leaf-block-manifest.v1.schema.json")
    documents = {name: json.loads((root / name).read_text(encoding="utf-8")) for name in names}
    resources = Registry()
    for name, document in documents.items():
        resource = Resource.from_contents(document)
        resources = resources.with_resource("https://leaf.internal/contract/" + name, resource)
        resources = resources.with_resource(document["$id"], resource)
    return {name: Draft202012Validator(document, registry=resources)
            for name, document in documents.items()}


def check_embedded(schema):
    vocabulary = {"type", "const", "enum", "required", "properties", "additionalProperties",
                  "items", "minItems", "maxItems", "minimum", "maximum", "minLength",
                  "maxLength", "title", "description"}
    if not isinstance(schema, dict) or "type" not in schema or set(schema) - vocabulary:
        raise ValueError("unsupported schema")
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    if (not types or any(not isinstance(kind, str) or kind not in
                        {"null", "boolean", "number", "integer", "string", "object", "array"}
                        for kind in types) or len(set(types)) != len(types)):
        raise ValueError("schema type required")
    for key in ("minimum", "maximum"):
        if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
            raise ValueError("schema bounds refused")
    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and (type(schema[key]) not in (int, float)
                              or not math.isfinite(schema[key]) or int(schema[key]) != schema[key]
                              or schema[key] < 0):
            raise ValueError("schema bounds refused")
    if "enum" in schema:
        choices = schema["enum"]
        if (not isinstance(choices, list) or not choices
                or len({canonical_json(item) for item in choices}) != len(choices)):
            raise ValueError("schema choices refused")
    if "required" in schema:
        required = schema["required"]
        if (not isinstance(required, list) or any(not isinstance(key, str) for key in required)
                or len(set(required)) != len(required)):
            raise ValueError("required setting refused")
    for key in ("title", "description"):
        if key in schema and not isinstance(schema[key], str):
            raise ValueError("schema text refused")
    if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
        raise ValueError("closed settings required")
    if "properties" in schema:
        if not isinstance(schema["properties"], dict):
            raise ValueError("schema settings refused")
        for child in schema["properties"].values():
            check_embedded(child)
    if "items" in schema:
        check_embedded(schema["items"])
    if "object" in types:
        if not isinstance(schema.get("properties"), dict) or schema.get("additionalProperties") is not False:
            raise ValueError("closed settings required")
        if any(key not in schema["properties"] for key in schema.get("required", [])):
            raise ValueError("required setting missing")
    if "array" in types:
        check_embedded(schema.get("items"))


def compatible(output, input):
    if (output["mode"] != input["mode"] or output["unit"] != input["unit"]
            or (output["nullable"] and not input["nullable"])):
        return False
    a, b = output["schema"], input["schema"]
    if canonical_json(a) == canonical_json(b):
        return True
    if (set(a) | set(b)) - {"type", "minimum", "maximum", "enum"}:
        return False
    # JavaScript compares type arrays by identity, rather than by contents.
    if not (isinstance(a["type"], str) and isinstance(b["type"], str)
            and (a["type"] == b["type"] or (a["type"] == "integer" and b["type"] == "number"))):
        return False
    if "minimum" in b and ("minimum" not in a or a["minimum"] < b["minimum"]):
        return False
    if "maximum" in b and ("maximum" not in a or a["maximum"] > b["maximum"]):
        return False
    if "enum" in b and ("enum" not in a or any(
            canonical_json(value) not in {canonical_json(item) for item in b["enum"]}
            for value in a["enum"])):
        return False
    return True


def _admit_manifest(manifest):
    canonical_json(manifest)
    _contract_validators()["lab-block.v1.schema.json"].validate(manifest)
    check_embedded(manifest["settings_schema"])
    for direction, sample in (("inputs", "sample_input"), ("outputs", "sample_output")):
        pins = manifest[direction]
        properties = {key: {"anyOf": [pins[key]["schema"], {"type": "null"}]}
                      if pins[key]["nullable"] else pins[key]["schema"] for key in pins}
        required = [key for key in pins if direction == "outputs"
                    or (pins[key]["required"] and not pins[key]["has_default"])]
        Draft202012Validator({"type": "object", "additionalProperties": False,
                              "properties": properties, "required": required},
                             registry=Registry()).validate(manifest[sample])
    if manifest["latency_budget_us"]["p50"] > manifest["latency_budget_us"]["p99"]:
        raise ValueError("latency bounds refused")
    for direction in ("inputs", "outputs"):
        for pin in manifest[direction].values():
            check_embedded(pin["schema"])
            types = pin["schema"]["type"]
            if not pin["nullable"] and (types == "null" or isinstance(types, list) and "null" in types):
                raise ValueError("nullability mismatch")
            if pin["has_default"] and not (pin["default"] is None and pin["nullable"]):
                Draft202012Validator(pin["schema"], registry=Registry()).validate(pin["default"])
    if not isinstance(manifest["ui_schema"], dict) or set(manifest["ui_schema"]) - {"order", "labels"}:
        raise ValueError("presentation refused")


def _graph_inputs(graph, registry):
    # Match JSON.parse numbers before any graph or manifest validation.
    return _copy(graph), {key: _copy(manifest) for key, manifest in registry.items()}


def admit_graph(graph, registry):
    """Browser contract.graph admission, before server execution policy."""
    graph, registry = _graph_inputs(graph, registry)
    return _admit_graph(graph, registry)


def _admit_graph(graph, registry):
    try:
        _contract_validators()["lab-graph.v1.schema.json"].validate(graph)
        size = len(canonical_json(graph).encode("utf-8"))
    except Exception as error:
        raise GraphRefused("lab_graph_invalid") from error
    if size > 262144:
        raise GraphRefused("lab_graph_too_large")
    manifests = {}
    for block in graph["blocks"]:
        block_id, ref = block["id"], block["registry"]
        if block_id in manifests:
            raise GraphRefused("lab_graph_duplicate_block", block_id)
        manifest = registry.get(f'{ref["id"]}@{ref["version"]}')
        if manifest is None:
            raise GraphRefused("lab_graph_unknown_block", block_id)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("version"), dict):
            raise GraphRefused("lab_graph_manifest_invalid", block_id)
        if manifest["version"].get("code_sha256") != ref["code_sha256"]:
            raise GraphRefused("lab_graph_unknown_block", block_id)
        try:
            _admit_manifest(manifest)
        except Exception as error:
            raise GraphRefused("lab_graph_manifest_invalid", block_id) from error
        try:
            Draft202012Validator(manifest["settings_schema"], registry=Registry()).validate(block["settings"])
        except Exception as error:
            raise GraphRefused("lab_graph_invalid_settings", block_id) from error
        manifests[block_id] = manifest
    def pin(endpoint, direction):
        pins = manifests.get(endpoint["node"], {}).get(direction, {})
        if endpoint["port"] not in pins:
            raise GraphRefused("lab_graph_bad_port", endpoint["node"])
        return pins[endpoint["port"]]
    attached = set()
    adjacency = {key: [] for key in manifests}
    degrees = dict.fromkeys(manifests, 0)
    for pipe in graph["pipes"]:
        if not compatible(pin(pipe["from"], "outputs"), pin(pipe["to"], "inputs")):
            raise GraphRefused("lab_graph_incompatible_pins", pipe["to"]["node"])
        target = (pipe["to"]["node"], pipe["to"]["port"])
        if target in attached:
            raise GraphRefused("lab_graph_duplicate_input", target[0])
        attached.add(target)
        adjacency[pipe["from"]["node"]].append(target[0])
        degrees[target[0]] += 1
    for block_id, manifest in manifests.items():
        for name, input in manifest["inputs"].items():
            if input["required"] and not input["has_default"] and (block_id, name) not in attached:
                raise GraphRefused("lab_graph_input_required", block_id)
    ready = deque(key for key in manifests if not degrees[key])
    visited = 0
    while ready:
        visited += 1
        for target in adjacency[ready.popleft()]:
            degrees[target] -= 1
            if not degrees[target]:
                ready.append(target)
    if visited != len(manifests):
        raise GraphRefused("lab_graph_cycle")
    for endpoint in graph["outputs"].values():
        pin(endpoint, "outputs")
    return manifests


@dataclass(frozen=True)
class Budget:
    block_cpu_ms: float = 500
    block_wall_ms: float = 1000
    block_output_bytes: float = 4194304
    run_wall_s: float = 600

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and value > 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("lab_budget_invalid")


def evaluate_graph(graph, registry, *, budget=None, clock=time.monotonic,
                   cpu_clock=time.thread_time, cancelled=None, on_block=None,
                   tool_adapter=None, trusted_lookup=None):
    graph, registry = _graph_inputs(graph, registry)
    validate_graph(graph)
    manifests = _admit_graph(graph, registry)
    budget = Budget() if budget is None else budget
    started = clock()
    blocks = {block["id"]: block for block in graph["blocks"]}
    incoming = {key: [] for key in blocks}
    outgoing = {key: [] for key in blocks}
    tool_blocks = {}
    for block_id, block in blocks.items():
        ref = block["registry"]
        manifest = manifests[block_id]
        trusted = (lab_registry.lookup if trusted_lookup is None else trusted_lookup)(
            ref["id"], ref["version"])
        if (manifest["residency"] == "host"
                or manifest["id"] in ("studio-jobs", "studio-capabilities", "host-source")
                or (trusted is not None and trusted.residency == "host")):
            raise GraphRefused("lab_graph_host_refused", block_id)
        if tool_adapter is not None and tool_adapter.admissible(trusted):
            tool_blocks[block_id] = trusted
            continue
        if manifest["effect"] != "pure" or (trusted is not None and trusted.effect != "pure"):
            raise GraphRefused("lab_graph_effect_refused", block_id)
    for pipe in graph["pipes"]:
        incoming[pipe["to"]["node"]].append(pipe)
        outgoing[pipe["from"]["node"]].append(pipe["to"]["node"])

    # Complete the topological check before running even disconnected blocks.
    degrees = {key: len(pipes) for key, pipes in incoming.items()}
    ready = deque(key for key in blocks if not degrees[key])
    order = []
    while ready:
        block_id = ready.popleft()
        order.append(block_id)
        for target in outgoing[block_id]:
            degrees[target] -= 1
            if not degrees[target]:
                ready.append(target)
    if len(order) != len(blocks):
        raise GraphRefused("lab_graph_cycle")

    values = {}
    for block_id in order:
        if cancelled is not None and cancelled():
            raise RunCancelled()
        if clock() - started > budget.run_wall_s:
            raise RunBudgetExceeded()
        wall_started, cpu_started = clock(), cpu_clock()
        block, manifest = blocks[block_id], manifests[block_id]
        inputs = {pipe["to"]["port"]: values[pipe["from"]["node"]][pipe["from"]["port"]]
                  for pipe in incoming[block_id]}
        for port, pin in manifest["inputs"].items():
            if port not in inputs and pin["has_default"]:
                inputs[port] = runtime_value(pin["default"], mode=pin["mode"])
        metadata = inherited(inputs.values())
        remote = block_id in tool_blocks
        if remote:
            remaining_s = budget.run_wall_s - (clock() - started)
            if remaining_s <= 0:
                raise RunBudgetExceeded()
            adapted = tool_adapter(block_id, tool_blocks[block_id], manifest, inputs, block["settings"],
                                   deadline_s=remaining_s)
            if clock() - started >= budget.run_wall_s:
                raise RunBudgetExceeded()
        elif manifest["kind"] == "builtin" and has_operator(manifest["id"]):
            result = evaluate_operation(manifest["id"], inputs, block["settings"],
                                        output_mode=next(iter(manifest["outputs"].values()))["mode"])
        else:
            result = runtime_value(None, **metadata, error="server-execution-refused")
        outputs = {}
        for port, pin in manifest["outputs"].items():
            if remote:
                try:
                    result = (runtime_value(None, **metadata, error=adapted["error"])
                              if "error" in adapted else
                              runtime_value(adapted["outputs"].get(port), mode=pin["mode"], **metadata))
                except Exception:
                    result = runtime_value(None, **metadata, error="output-refused")
            value = result
            try:
                if not value.error:
                    if value.mode != pin["mode"]:
                        raise ValueError()
                    validator = Draft202012Validator(pin["schema"], registry=Registry())
                    for item in value.data if pin["mode"] == "stream" else [value.data]:
                        if item is not None or not pin["nullable"]:
                            validator.validate(item)
            except Exception:
                value = runtime_value(None, **inherited([result]), error="output-refused")
            outputs[port] = value
        size = sum(len(canonical_json(value.data).encode("utf-8"))
                   for value in outputs.values())
        cpu_ms = (cpu_clock() - cpu_started) * 1000
        wall_ms = (clock() - wall_started) * 1000
        if ((not remote and (cpu_ms > budget.block_cpu_ms or wall_ms > budget.block_wall_ms))
                or size > budget.block_output_bytes):
            outputs = {port: runtime_value(None, **inherited([value]), error="budget-exceeded")
                       for port, value in outputs.items()}
        values[block_id] = MappingProxyType(outputs)
        if on_block is not None:
            on_block(block_id, values[block_id])
    finished = clock()
    return {"values": values,
            "outputs": {name: values[endpoint["node"]][endpoint["port"]]
                        for name, endpoint in graph["outputs"].items()},
            "timing": {"started_ns": str(round(started * 1e9)),
                       "finished_ns": str(round(finished * 1e9)),
                       "duration_ms": max(0, (finished - started) * 1000)}}
