"""Shared pure graph parity and server-only admission / resource boundaries."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

import lab_evaluate as evaluator
from lab_doc_validators import InvalidGraph, TooLarge
from lab_evaluate import (Budget, GraphRefused, RunBudgetExceeded, RunCancelled,
                          ValueRefused, evaluate_graph, evaluate_operation,
                          export_value, inherited, runtime_value, trusted_registry)
from lab_ops import MAX_ROWS, UNDEFINED

FIXTURE = json.loads((Path(__file__).parent / "fixtures/lab-graph-cases.v1.json").read_text(encoding="utf-8"))
CASES = FIXTURE["cases"]
FIELDS = ("data", "mode", "error", "truncated", "host_tainted", "simulated", "stale")


def case(name="identity"):
    value = deepcopy(next(item for item in CASES if item["name"] == name))
    return value["graph"], {f'{m["id"]}@{m["version"]["semver"]}': m for m in value["registry"]}


def ticks(*values):
    return iter(values).__next__


@pytest.mark.parametrize("item", CASES, ids=lambda item: item["name"])
def test_shared_case(item):
    before = deepcopy(item)
    registry = {f'{m["id"]}@{m["version"]["semver"]}': m for m in item["registry"]}
    result = evaluate_graph(item["graph"], registry)
    assert {name: {key: getattr(value, key) for key in FIELDS}
            for name, value in result["outputs"].items()} == item["expect"]
    assert item == before
    assert set(result["values"]) == {block["id"] for block in item["graph"]["blocks"]}
    assert int(result["timing"]["finished_ns"]) >= int(result["timing"]["started_ns"])


def test_fixture_contract():
    assert FIXTURE["schema"] == "leaf.lab-graph-cases.v1"
    assert len(CASES) >= 25
    assert len({item["name"] for item in CASES}) == len(CASES)


@pytest.mark.parametrize("source", ["studio-jobs", "studio-capabilities", "host-source"])
def test_read_sources_refused_before_any_block(source):
    graph, registry = case()
    manifest = registry.pop("identity@1.0.1")
    manifest.update(id=source, effect="read")
    registry[f"{source}@1.0.1"] = manifest
    graph["blocks"][1]["registry"]["id"] = source
    seen = []
    with pytest.raises(GraphRefused) as exc:
        evaluate_graph(graph, registry, on_block=lambda *args: seen.append(args))
    assert (exc.value.code, exc.value.block_id) == ("lab_graph_host_refused", "block-1")
    assert seen == []


def test_manifest_host_refused():
    graph, registry = case()
    registry["identity@1.0.1"]["residency"] = "host"
    with pytest.raises(GraphRefused, match="lab_graph_host_refused"):
        evaluate_graph(graph, registry)


@pytest.mark.parametrize("effect,residency,code", [
    ("pure", "host", "lab_graph_host_refused"),
    ("write", "tenant", "lab_graph_effect_refused"),
    ("simulate", "tenant", "lab_graph_effect_refused"),
    ("write", "host", "lab_graph_host_refused"),
])
def test_server_derived_metadata_is_authoritative(monkeypatch, effect, residency, code):
    graph, registry = case()
    monkeypatch.setattr(evaluator.lab_registry, "lookup", lambda kind, version:
                        SimpleNamespace(effect=effect, residency=residency) if kind == "identity" else None)
    seen = []
    with pytest.raises(GraphRefused, match=code):
        evaluate_graph(graph, registry, on_block=lambda *args: seen.append(args))
    assert not seen


@pytest.mark.parametrize("effect", ["write", "simulate", "read", "compute"])
def test_manifest_effect_refused(effect):
    graph, registry = case()
    registry["identity@1.0.1"]["effect"] = effect
    with pytest.raises(GraphRefused, match="lab_graph_effect_refused"):
        evaluate_graph(graph, registry)


@pytest.mark.parametrize("change", ["missing", "hash"])
def test_unknown_block(change):
    graph, registry = case()
    if change == "missing":
        del registry["identity@1.0.1"]
    else:
        graph["blocks"][1]["registry"]["code_sha256"] = "f" * 64
    with pytest.raises(GraphRefused) as exc:
        evaluate_graph(graph, registry)
    assert (exc.value.code, exc.value.block_id) == ("lab_graph_unknown_block", "block-1")


@pytest.mark.parametrize("target", ["from", "to", "output"])
def test_bad_port(target):
    graph, registry = case()
    endpoint = graph["outputs"]["result"] if target == "output" else graph["pipes"][0][target]
    endpoint["port"] = "missing"
    with pytest.raises(GraphRefused) as exc:
        evaluate_graph(graph, registry)
    assert exc.value.code == "lab_graph_bad_port"
    assert exc.value.block_id == endpoint["node"]


def test_cycle_is_refused_before_any_block():
    graph, registry = case()
    graph["pipes"].append({"from": {"node": "block-1", "port": "result"},
                           "to": {"node": "block-1", "port": "value"}})
    seen = []
    with pytest.raises(GraphRefused, match="lab_graph_cycle"):
        evaluate_graph(graph, registry, on_block=lambda *args: seen.append(args))
    assert not seen


@pytest.mark.parametrize("kind", ["cpu", "wall", "bytes"])
def test_block_budget_propagates_upstream_failure(kind):
    graph, registry = case()
    kwargs = {"clock": ticks(0, 0, 0, 0, 0, 0, 0, 0), "cpu_clock": ticks(0, 0, 0, 0)}
    if kind == "cpu":
        kwargs["cpu_clock"] = ticks(0, 1, 1, 1)
    elif kind == "wall":
        kwargs["clock"] = ticks(0, 0, 0, 2, 2, 2, 2, 2)
    else:
        kwargs["budget"] = Budget(block_output_bytes=4)
    seen = []
    result = evaluate_graph(graph, registry, **kwargs, on_block=lambda *args: seen.append(args))
    assert result["values"]["block-0"]["result"].error == "budget-exceeded"
    assert result["outputs"]["result"].error == "upstream-failed"
    assert seen[0][1]["result"].error == "budget-exceeded"


def test_budget_replaces_every_output_and_inherits_metadata():
    graph, registry = case("row-truncation")
    manifest = registry["constant@1.0.0"]
    manifest["outputs"]["other"] = deepcopy(manifest["outputs"]["result"])
    result = evaluate_graph(graph, registry, budget=Budget(block_output_bytes=1))
    for output in result["values"]["block-0"].values():
        assert output.error == "budget-exceeded"
        assert output.truncated


@pytest.mark.parametrize("limit", [10, 25, 26])
def test_output_byte_budget_counts_ports_after_validation(limit):
    graph, registry = case()
    graph["blocks"][0]["settings"]["value"] = "x" * 20
    manifest = registry["constant@1.0.0"]
    manifest["outputs"]["result"]["schema"] = {"type": "number"}
    manifest["outputs"]["other"] = deepcopy(manifest["outputs"]["result"])
    manifest["outputs"]["other"]["schema"] = {"type": "string"}
    result = evaluate_graph(graph, registry, budget=Budget(block_output_bytes=limit))
    outputs = result["values"]["block-0"]
    # Refused data is null (4 bytes); the retained string is 22 UTF-8 bytes.
    if limit < 26:
        assert set(outputs) == {"result", "other"}
        assert all(value.error == "budget-exceeded" and value.data is None
                   for value in outputs.values())
    else:
        assert outputs["result"].error == "output-refused"
        assert outputs["result"].data is None
        assert outputs["other"].error is None
        assert outputs["other"].data == "x" * 20
    assert result["outputs"]["result"].error == "upstream-failed"


@pytest.mark.parametrize("limit", [9, 10, 11])
def test_output_byte_budget_sums_small_ports(limit):
    graph, registry = case()
    manifest = registry["constant@1.0.0"]
    manifest["outputs"]["other"] = deepcopy(manifest["outputs"]["result"])
    result = evaluate_graph(graph, registry, budget=Budget(block_output_bytes=limit))
    outputs = result["values"]["block-0"]
    # Each [1,2] fits individually (5 bytes); together they require 10 bytes.
    assert set(outputs) == {"result", "other"}
    for value in outputs.values():
        assert value.error == ("budget-exceeded" if limit < 10 else None)
        assert value.data == (None if limit < 10 else [1, 2])
    downstream = result["outputs"]["result"]
    assert downstream.error == ("upstream-failed" if limit < 10 else None)
    assert downstream.data == (None if limit < 10 else [1, 2])


def test_run_wall_budget():
    graph, registry = case()
    seen = []
    with pytest.raises(RunBudgetExceeded):
        evaluate_graph(graph, registry, budget=Budget(run_wall_s=1), clock=ticks(0, 2),
                       on_block=lambda *args: seen.append(args))
    assert seen == []


def test_cancellation_between_blocks():
    graph, registry = case()
    seen = []
    with pytest.raises(RunCancelled):
        evaluate_graph(graph, registry, cancelled=lambda: bool(seen),
                       on_block=lambda block_id, outputs: seen.append((block_id, outputs)))
    assert [block_id for block_id, _ in seen] == ["block-0"]
    assert seen[0][1]["result"].data == [1, 2]


def test_callback_exception_propagates():
    graph, registry = case()
    def callback(*args):
        raise LookupError("worker stopped")
    with pytest.raises(LookupError, match="worker stopped"):
        evaluate_graph(graph, registry, on_block=callback)


@pytest.mark.parametrize("field", ["block_cpu_ms", "block_wall_ms", "block_output_bytes", "run_wall_s"])
@pytest.mark.parametrize("value", [0, -1, math.inf, math.nan, True, "1"])
def test_invalid_budget(field, value):
    with pytest.raises(ValueError):
        Budget(**{field: value})


def test_budget_is_frozen():
    with pytest.raises(FrozenInstanceError):
        Budget().block_cpu_ms = 1


def test_trusted_registry_is_complete_cached_and_immutable():
    root = Path(__file__).resolve().parents[2] / "contract/fixtures/lab/blocks.json"
    manifests = json.loads(root.read_text(encoding="utf-8"))["manifests"]
    registry = trusted_registry()
    assert len(registry) == len(manifests)
    assert registry is trusted_registry()
    for manifest in manifests:
        key = f'{manifest["id"]}@{manifest["version"]["semver"]}'
        assert registry[key] == manifest
    with pytest.raises(TypeError):
        registry["new"] = {}
    with pytest.raises(TypeError):
        next(iter(registry.values()))["effect"] = "write"


@pytest.mark.parametrize("malformed", ["missing", "json", "envelope", "manifest", "duplicate"])
def test_trusted_registry_fails_closed(monkeypatch, malformed):
    trusted_registry.cache_clear()
    original = Path.read_text
    def read(path, *args, **kwargs):
        if path.name != "blocks.json":
            return original(path, *args, **kwargs)
        if malformed == "missing":
            raise FileNotFoundError(path)
        if malformed == "json":
            return "{"
        document = json.loads(original(path, *args, **kwargs))
        if malformed == "envelope":
            document["schema"] = "wrong"
        elif malformed == "manifest":
            del document["manifests"][-1]["outputs"]
        else:
            document["manifests"].append(document["manifests"][0])
        return json.dumps(document)
    monkeypatch.setattr(Path, "read_text", read)
    try:
        with pytest.raises(Exception):
            trusted_registry()
        assert trusted_registry.cache_info().currsize == 0
    finally:
        trusted_registry.cache_clear()


def test_export_refuses_host_values_and_copies():
    with pytest.raises(ValueRefused, match="lab_host_value_export"):
        export_value(runtime_value(1, host_tainted=True))
    value = runtime_value({"rows": [1]}, error={"code": "example"})
    exported = export_value(value)
    assert exported == {"data": {"rows": [1]}, "mode": "scalar", "as_of": None,
                        "host_tainted": False, "simulated": False, "stale": False,
                        "truncated": False, "error": {"code": "example"}}
    exported["data"]["rows"].append(2)
    assert value.data == {"rows": [1]}


def test_runtime_snapshot_bounds_and_exact_flags():
    source = {"rows": [[0] * (MAX_ROWS + 1)]}
    value = runtime_value(source, host_tainted=1, simulated="true", stale=True)
    source["rows"][0][0] = 8
    assert value.data["rows"][0][0] == 0
    assert len(value.data["rows"][0]) == MAX_ROWS
    assert (value.host_tainted, value.simulated, value.stale, value.truncated) == (False, False, True, True)
    with pytest.raises(TypeError):
        value.data["rows"].append([])
    with pytest.raises(TypeError):
        value.data["rows"][0][0] = 1
    with pytest.raises(FrozenInstanceError):
        value.mode = "stream"


@pytest.mark.parametrize("data,kwargs", [
    (math.nan, {}), (math.inf, {}), (UNDEFINED, {}), ({1: "x"}, {}),
    (1, {"mode": "stream"}), ([], {"mode": "wrong"}),
    (1, {"as_of": -1}), (1, {"as_of": math.inf}), (1, {"as_of": True}),
])
def test_value_refusals(data, kwargs):
    with pytest.raises(ValueRefused):
        runtime_value(data, **kwargs)


def test_metadata_inheritance_and_sanitized_errors():
    first = runtime_value(2, as_of=20, simulated=True, truncated=True)
    second = runtime_value(1, as_of=10, stale=True, host_tainted=True)
    result = evaluate_operation("add", {"value": first, "auxiliary": second})
    assert result.data == 3
    assert inherited([result]) == {"as_of": 10, "host_tainted": True,
                                   "simulated": True, "stale": True, "truncated": True}
    undated = evaluate_operation("add", {"value": first, "auxiliary": runtime_value(1, host_tainted=True)})
    assert undated.as_of is None
    failure = evaluate_operation("divide", {"value": result, "auxiliary": runtime_value(0)})
    assert failure.error == "operator-refused" and failure.host_tainted
    assert evaluate_operation("identity", {"value": failure}).error == "upstream-failed"
    assert evaluate_operation("unknown", {"value": failure}).error == "operator-refused"
    assert runtime_value(None, error={}).error == "evaluation-failed"
    with pytest.raises(ValueRefused):
        evaluate_operation("identity", {"value": {"data": 1}})


@pytest.mark.parametrize("error,expected", [
    ({}, "evaluation-failed"), ([], "evaluation-failed"),
    (["ignored"], "evaluation-failed"), ({"code": "example"}, "example"),
    (None, None), (False, None), (0, None), (math.nan, None),
])
def test_error_metadata_uses_browser_truthiness(error, expected):
    assert runtime_value(None, error=error).error == expected


@pytest.mark.parametrize("nested", [False, True])
def test_settings_large_integers_use_browser_numbers(nested):
    graph, registry = case("constant-number")
    graph["blocks"][0]["settings"]["value"] = [9007199254740993] if nested else 9007199254740993
    pin = registry["constant@1.0.0"]["outputs"]["result"]
    pin["schema"] = {"type": "array", "items": {"type": "number"}} if nested else {"type": "number"}
    value = evaluate_graph(graph, registry)["outputs"]["result"].data
    number = value[0] if nested else value
    assert type(number) is float and number == 9007199254740992.0
    assert graph["blocks"][0]["settings"]["value"] == ([9007199254740993] if nested else 9007199254740993)


def test_runtime_numbers_and_negative_zero():
    value = runtime_value([2 ** 53, 2 ** 53 + 1, -(2 ** 53 + 1), -0.0])
    assert type(value.data[0]) is int
    assert value.data[1:3] == [9007199254740992.0, -9007199254740992.0]
    assert math.copysign(1, value.data[3]) == -1


def test_long_chain_is_iterative_and_once_per_block():
    graph, registry = case()
    template = graph["blocks"][1]
    graph["blocks"] = [graph["blocks"][0]] + [dict(deepcopy(template), id=f"block-{i}") for i in range(1, 200)]
    graph["pipes"] = [{"from": {"node": f"block-{i - 1}", "port": "result"},
                       "to": {"node": f"block-{i}", "port": "value"}} for i in range(1, 200)]
    graph["outputs"]["result"]["node"] = "block-199"
    seen = []
    result = evaluate_graph(graph, registry, on_block=lambda block_id, outputs: seen.append(block_id))
    assert len(seen) == len(set(seen)) == 200
    assert result["outputs"]["result"].data == [1, 2]


def test_diamond_and_reversed_order_are_cached(monkeypatch):
    graph, registry = case("diamond-cache")
    graph["blocks"].reverse()
    calls = []
    original = evaluator.run_operator
    def run(op_id, inputs, settings):
        calls.append(op_id)
        return original(op_id, inputs, settings)
    monkeypatch.setattr(evaluator, "run_operator", run)
    result = evaluate_graph(graph, registry)
    assert result["outputs"]["result"].data == 8
    assert calls == ["constant", "identity", "add"]


def test_output_pins_validate_independently():
    graph, registry = case("constant-number")
    manifest = registry["constant@1.0.0"]
    wrong = deepcopy(manifest["outputs"]["result"])
    wrong["mode"] = "stream"
    manifest["outputs"]["wrong-mode"] = wrong
    wrong_schema = deepcopy(manifest["outputs"]["result"])
    wrong_schema["schema"] = {"type": "string"}
    manifest["outputs"]["wrong-schema"] = wrong_schema
    result = evaluate_graph(graph, registry)["values"]["block-0"]
    assert result["result"].data == 7
    assert result["wrong-mode"].error == result["wrong-schema"].error == "output-refused"


def test_stream_events_are_validated_individually():
    graph, registry = case("stream-latest")
    graph["blocks"][0]["settings"]["value"] = [1, "wrong"]
    result = evaluate_graph(graph, registry)
    assert result["values"]["block-0"]["result"].error == "output-refused"
    assert result["outputs"]["result"].error == "upstream-failed"


@pytest.mark.parametrize("mutation", ["duplicate", "endpoint", "oversize"])
def test_envelope_validation_precedes_registry_lookup(monkeypatch, mutation):
    graph, registry = case()
    if mutation == "duplicate":
        graph["blocks"].append(deepcopy(graph["blocks"][0]))
    elif mutation == "endpoint":
        graph["outputs"]["result"]["node"] = "absent"
    else:
        graph["blocks"][0]["settings"]["value"] = "x" * 262144
    def unexpected(*args):
        pytest.fail("registry lookup preceded validation")
    monkeypatch.setattr(evaluator.lab_registry, "lookup", unexpected)
    with pytest.raises((InvalidGraph, TooLarge)):
        evaluate_graph(graph, registry)
