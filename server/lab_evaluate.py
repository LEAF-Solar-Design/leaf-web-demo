"""mirror of web/src/lab/runtime/evaluate.js for pure graphs; host and effectful graphs are refused before evaluation; change both together and extend server/tests/fixtures/lab-graph-cases.v1.json"""
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
from referencing import Registry

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
        children = {key: _copy(child, bounded=bounded, frozen=frozen, flags=flags)
                    for key, child in data.items()}
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
                            {} if settings is None else _copy(settings))
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
                   cpu_clock=time.thread_time, cancelled=None, on_block=None):
    validate_graph(graph)
    budget = Budget() if budget is None else budget
    started = clock()
    manifests = {}
    blocks = {block["id"]: block for block in graph["blocks"]}
    incoming = {key: [] for key in blocks}
    outgoing = {key: [] for key in blocks}
    for block_id, block in blocks.items():
        ref = block["registry"]
        manifest = registry.get(f'{ref["id"]}@{ref["version"]}')
        if manifest is None or manifest["version"]["code_sha256"] != ref["code_sha256"]:
            raise GraphRefused("lab_graph_unknown_block", block_id)
        manifests[block_id] = manifest
        trusted = lab_registry.lookup(ref["id"], ref["version"])
        if (manifest["residency"] == "host"
                or manifest["id"] in ("studio-jobs", "studio-capabilities", "host-source")
                or (trusted is not None and trusted.residency == "host")):
            raise GraphRefused("lab_graph_host_refused", block_id)
        if manifest["effect"] != "pure" or (trusted is not None and trusted.effect != "pure"):
            raise GraphRefused("lab_graph_effect_refused", block_id)
    for pipe in graph["pipes"]:
        for side, direction in (("from", "outputs"), ("to", "inputs")):
            endpoint = pipe[side]
            if endpoint["port"] not in manifests[endpoint["node"]][direction]:
                raise GraphRefused("lab_graph_bad_port", endpoint["node"])
        incoming[pipe["to"]["node"]].append(pipe)
        outgoing[pipe["from"]["node"]].append(pipe["to"]["node"])
    for endpoint in graph["outputs"].values():
        if endpoint["port"] not in manifests[endpoint["node"]]["outputs"]:
            raise GraphRefused("lab_graph_bad_port", endpoint["node"])

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
        if manifest["kind"] == "builtin" and has_operator(manifest["id"]):
            result = evaluate_operation(manifest["id"], inputs, block["settings"],
                                        output_mode=next(iter(manifest["outputs"].values()))["mode"])
        else:
            result = runtime_value(None, **metadata, error="server-execution-refused")
        outputs = {}
        for port, pin in manifest["outputs"].items():
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
        size = len(canonical_json(next(iter(outputs.values())).data).encode("utf-8"))
        cpu_ms = (cpu_clock() - cpu_started) * 1000
        wall_ms = (clock() - wall_started) * 1000
        if (cpu_ms > budget.block_cpu_ms or wall_ms > budget.block_wall_ms
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
