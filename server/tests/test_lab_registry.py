"""Effect oracle, extension requirements and bounded latency telemetry."""
import json
from time import perf_counter

import pytest

import lab_registry as registry
from lab_validate import ManifestInvalid, validate_manifest
from test_lab_validate import ROOT, manifest


@pytest.mark.parametrize("capability,adapter,execution,live,effect", [
    ("pure.value", None, "inline", False, "pure"),
    ("read.value", None, "inline", False, "read"),
    ("value", None, "handoff", False, "read"),
    ("simulate.value", None, "inline", False, "simulate"),
    ("value", "simulate", "inline", False, "simulate"),
    ("value", "simulate.solar", "inline", False, "simulate"),
    ("simulate.value", None, "worker", False, "compute"),
    ("read.value", None, "worker", False, "compute"),
    ("drawing.write", None, "inline", False, "refused"),
    ("drawing.write.child", None, "worker", False, "refused"),
    ("solve", None, "inline", False, "refused"),
    ("solve.child", None, "inline", False, "refused"),
    ("cloud-proposal", None, "inline", False, "refused"),
    ("cloud-proposal.child", None, "inline", False, "refused"),
    ("value", None, "inline", True, "refused"),
    ("", None, "inline", False, "refused"),
    (None, None, "inline", False, "refused"),
    ([], None, "inline", False, "refused"),
    ("value", {}, "inline", False, "refused"),
    ("value", None, [], False, "refused"),
    ("value", None, "batch", False, "refused"),
    ("value", None, "inline", 1, "refused"),
])
def test_total_effect(capability, adapter, execution, live, effect):
    assert registry.total_effect(capability, adapter, execution, live) == effect


def test_lookup():
    assert registry.lookup("qa-sleep-count", "1.0.0").effect == "pure"
    for source in registry.KASHBOT_CLASSES:
        block = registry.lookup("kashbot." + source, "1.0.0")
        assert (block.effect, block.residency) == ("read", "host")
    assert registry.lookup("missing", "1.0.0") is None
    assert registry.lookup("qa-sleep-count", "9.0.0") is None
    assert registry.lookup([], {}) is None


@pytest.fixture
def extension(monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY", dict(registry.REGISTRY))
    return {"id": "extension", "kind": "extension.example", "version": "1.0.0",
            "capability": "read.value", "schema": {"version": "1.0.0"},
            "fixtures": [{}], "ui_schema": {}, "samples": [{}],
            "latency": {"p50_ms": 1, "p95_ms": 50}, "migration": {"policy": "none"}}


def test_register(extension):
    block = registry.register_extension(extension)
    assert registry.lookup(block.kind, block.version) is block
    assert block.effect == "read"
    assert block.runnable_in_production
    with pytest.raises(ManifestInvalid):
        registry.register_extension(extension)


@pytest.mark.parametrize("execution,capability,effect", [
    ("worker", "read.value", "compute"), ("inline", "simulate.value", "simulate"),
])
def test_extension_manifest_roundtrip(extension, execution, capability, effect):
    extension.update(execution_class=execution, capability=capability, residency="none")
    block = registry.register_extension(extension)
    data = manifest()
    data.update(kind=block.kind, effect=effect, residency="none")
    assert validate_manifest(data) is block


@pytest.mark.parametrize("field", ["schema", "fixtures", "ui_schema", "samples", "latency", "migration"])
def test_missing_extension_field(extension, field):
    del extension[field]
    with pytest.raises(ManifestInvalid) as caught:
        registry.register_extension(extension)
    assert caught.value.code == "lab_extension_incomplete"
    assert caught.value.pointer == "/" + field


@pytest.mark.parametrize("field,value", [
    ("schema", {}), ("fixtures", []), ("samples", []), ("ui_schema", []),
    ("latency", {"p50_ms": True, "p95_ms": 50}),
    ("latency", {"p50_ms": 0, "p95_ms": 60001}),
    ("migration", {"policy": "unknown"}),
])
def test_incomplete_extension_field(extension, field, value):
    extension[field] = value
    with pytest.raises(ManifestInvalid) as caught:
        registry.register_extension(extension)
    assert caught.value.pointer == "/" + field


@pytest.mark.parametrize("capability", ["money", "money.transfer"])
def test_money(extension, capability):
    extension["capability"] = capability
    with pytest.raises(ManifestInvalid) as caught:
        registry.register_extension(extension)
    assert caught.value.code == "lab_manifest_unregistrable"
    assert caught.value.pointer == "/capability"


def test_pc(extension):
    extension["host"] = "pc"
    assert not registry.register_extension(extension).runnable_in_production


def test_histogram():
    histogram = registry.LatencyHistogram()
    assert histogram.p95() == 0
    for index in range(33):
        histogram.observe(64 * 2 ** index)
    assert len(histogram.counts) == 32
    assert histogram.counts == [1] * 31 + [2]
    assert histogram.p95() == histogram.buckets[-1]
    assert set(vars(histogram)) == {"counts"}
    for invalid in (-1, True, float("inf")):
        with pytest.raises(ValueError):
            histogram.observe(invalid)


def test_validation_benchmark():
    # Port fixture pins exercise real scalar and stream port structures.
    ports = json.loads((ROOT / "contract/fixtures/lab/port-compat.v1.json").read_text(encoding="utf-8"))
    data = manifest()
    data["inputs"] = {"value": ports["cases"][0]["input"]}
    data["outputs"] = {"result": ports["cases"][0]["output"]}
    elapsed = []
    for _ in range(200):
        start = perf_counter()
        validate_manifest(data)
        elapsed.append((perf_counter() - start) * 1000)
    p95 = sorted(elapsed)[189]
    print(f"manifest validation p95={p95:.3f} ms; target=50 ms; CI ceiling=500 ms")
    assert p95 <= 500
