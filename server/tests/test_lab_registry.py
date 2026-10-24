"""Effect oracle, extension requirements and bounded latency telemetry."""
import json
import runpy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from time import perf_counter

import pytest

import lab_registry as registry
from lab_validate import ManifestInvalid, validate_manifest, validate_returns_schema
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


@pytest.mark.parametrize("effect,capability,execution,host_source,residency", [
    ("pure", "host.value", "inline", None, "host"),
    ("compute", "host.value", "worker", None, "host"),
    ("pure", "pure.value", "inline", "kashbot.engine", "host"),
    ("read", "read.value", "inline", "mcp.example", "host"),
    ("read", "value", "handoff", None, "host"),
    ("pure", "pure.value", "inline", None, "none"),
    ("pure", "hosted.value", "inline", None, "none"),
    ("read", "read.value", "inline", None, "tenant"),
    ("simulate", "simulate.value", "inline", None, "tenant"),
    ("simulate", "value", "inline", None, "tenant"),
    ("compute", "pure.value", "worker", None, "tenant"),
])
def test_total_residency(effect, capability, execution, host_source, residency):
    assert registry.total_residency(effect, capability, execution, host_source) == residency


def test_seed_residency_agrees_with_table():
    for block in registry._seed_registry().values():
        assert block.residency == registry.total_residency(
            block.effect, block.capability, block.execution_class, block.host_source)


def test_seed_solar_adapter_residency(monkeypatch):
    seed = {"id": "solar-adapter", "capability": "value", "solar_adapter": "simulate",
            "execution_class": "inline"}
    monkeypatch.setattr(registry.catalog, "seed_tools", lambda: [seed])
    block = registry._seed_registry()[("solar-adapter", "1.0.0")]
    assert (block.effect, block.residency) == ("simulate", "tenant")


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
    assert block.residency == "tenant"
    assert block.runnable_in_production
    assert block.returns_schema_digest is None
    with pytest.raises(ManifestInvalid):
        registry.register_extension(extension)


@pytest.mark.parametrize("host_source", ["", [], None, 7, "x" * 129, "host\n"])
def test_J3_extension_host_source_must_be_bounded_printable_text(extension, host_source):
    extension.update(capability="pure.value", residency="none", host_source=host_source)
    before = dict(registry.REGISTRY)
    with pytest.raises(ManifestInvalid) as caught:
        registry.register_extension(extension)
    assert caught.value.to_http() == (422, {
        "detail": "lab_manifest_invalid", "pointer": "/host_source", "block": extension["id"],
    })
    assert registry.REGISTRY == before


def test_J3_seed_host_source_is_validated_at_import(monkeypatch):
    seed = {"id": "bad-host", "capability": "pure.value", "host_source": ""}
    monkeypatch.setattr(registry.catalog, "seed_tools", lambda: [seed])
    with pytest.raises(ManifestInvalid) as caught:
        runpy.run_path(registry.__file__)
    assert caught.value.to_http() == (422, {
        "detail": "lab_manifest_invalid", "pointer": "/host_source", "block": "bad-host",
    })


def test_J3_valid_host_source_requires_host_residency(extension):
    extension.update(capability="pure.value", host_source="h" * 128, residency="none")
    with pytest.raises(ManifestInvalid) as caught:
        registry.register_extension(extension)
    assert caught.value.pointer == "/residency"
    extension["residency"] = "host"
    block = registry.register_extension(extension)
    assert block.residency == "host"
    assert block.host_source == extension["host_source"]


@pytest.mark.parametrize("capability,execution,host_source,residency", [
    ("read.value", "inline", None, "tenant"),
    ("pure.value", "inline", None, "none"),
    ("host.value", "inline", None, "host"),
    ("read.value", "inline", "mcp.example", "host"),
    ("read.value", "handoff", None, "host"),
])
@pytest.mark.parametrize("declare_residency", [False, True])
def test_register_derived_residency(extension, capability, execution, host_source,
                                   residency, declare_residency):
    extension.update(capability=capability, execution_class=execution)
    if host_source is not None:
        extension["host_source"] = host_source
    if declare_residency:
        extension["residency"] = residency
    block = registry.register_extension(extension)
    assert block.residency == residency
    assert block.host_source == host_source
    data = manifest()
    data.update(kind=block.kind, effect=block.effect, residency=residency)
    assert validate_manifest(data) is block


def test_register_solar_adapter_residency(extension):
    extension.update(capability="value", solar_adapter="simulate", execution_class="inline")
    block = registry.register_extension(extension)
    assert (block.effect, block.residency) == ("simulate", "tenant")
    data = manifest()
    data.update(kind=block.kind, effect=block.effect, residency="tenant")
    assert validate_manifest(data) is block
    data["residency"] = "none"
    with pytest.raises(ManifestInvalid) as caught:
        validate_manifest(data)
    assert caught.value.code == "lab_manifest_spoof"
    assert caught.value.pointer == "/residency"
    extension["residency"] = "none"
    before = dict(registry.REGISTRY)
    with pytest.raises(ManifestInvalid) as caught:
        registry.register_extension(extension)
    assert caught.value.code == "lab_manifest_spoof"
    assert caught.value.pointer == "/residency"
    assert registry.REGISTRY == before


def test_register_returns_schema(extension):
    extension["returns_schema"] = {"type": "object", "properties": {"value": {"type": "string"}}}
    block = registry.register_extension(extension)
    assert registry.lookup(block.kind, block.version) is block
    assert block.returns_schema_digest == validate_returns_schema(extension["returns_schema"])


def test_register_rejects_remote_returns_schema_ref(extension):
    extension["returns_schema"] = {"$ref": "https://example.invalid"}
    before = dict(registry.REGISTRY)
    with pytest.raises(ManifestInvalid) as caught:
        registry.register_extension(extension)
    assert caught.value.code == "lab_returns_schema_bounds"
    assert caught.value.pointer == "/returns_schema/$ref"
    assert caught.value.block_id == extension["id"]
    assert caught.value.to_http()[0] == 422
    assert registry.lookup(extension["kind"], extension["version"]) is None
    assert registry.REGISTRY == before


@pytest.mark.parametrize("execution,capability,effect", [
    ("worker", "read.value", "compute"), ("inline", "simulate.value", "simulate"),
])
def test_extension_manifest_roundtrip(extension, execution, capability, effect):
    extension.update(execution_class=execution, capability=capability, residency="tenant")
    block = registry.register_extension(extension)
    data = manifest()
    data.update(kind=block.kind, effect=effect, residency="tenant")
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
    assert set(vars(histogram)) == {"counts", "dropped"}
    for invalid in (-1, True, float("inf")):
        histogram.observe(invalid)
    assert histogram.dropped == 3


def test_A1_concurrent_registration_refuses_duplicate(extension, monkeypatch):
    entered = Event()
    second_check = Event()
    start = Barrier(2)

    class ContendedRegistry(dict):
        def __contains__(self, key):
            exists = super().__contains__(key)
            if not entered.is_set():
                entered.set()
                # Without the registration lock both writers see the absent key.
                second_check.wait(timeout=1)
            else:
                second_check.set()
            return exists

    monkeypatch.setattr(registry, "REGISTRY", ContendedRegistry(registry.REGISTRY))

    def register(_):
        start.wait(timeout=5)
        try:
            return registry.register_extension(extension)
        except ManifestInvalid as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, range(2)))
    blocks = [value for value in results if isinstance(value, registry.TrustedBlock)]
    errors = [value for value in results if isinstance(value, ManifestInvalid)]
    assert len(blocks) == len(errors) == 1
    assert errors[0].to_http() == (422, {
        "detail": "lab_manifest_duplicate", "pointer": "/kind", "block": extension["id"],
    })
    assert registry.lookup(extension["kind"], extension["version"]) is blocks[0]


@pytest.mark.parametrize("field,value", [
    ("effect", "compute"), ("residency", "unknown"), ("residency", "host"),
    ("residency", "none"),
])
def test_A2_extension_declarations_cannot_spoof(extension, field, value):
    extension[field] = value
    before = dict(registry.REGISTRY)
    with pytest.raises(ManifestInvalid) as caught:
        registry.register_extension(extension)
    assert caught.value.to_http() == (422, {
        "detail": "lab_manifest_spoof", "pointer": "/" + field, "block": extension["id"],
    })
    assert registry.REGISTRY == before
    extension.update(effect="read", residency="tenant")
    block = registry.register_extension(extension)
    data = manifest()
    data.update(kind=block.kind, effect=block.effect, residency=block.residency)
    assert validate_manifest(data) is block
    data[field] = value
    with pytest.raises(ManifestInvalid) as caught:
        validate_manifest(data)
    assert caught.value.code == "lab_manifest_spoof"
    assert caught.value.pointer == "/" + field
    assert caught.value.block_id == data["id"]


@pytest.mark.parametrize("field,value", [
    ("id", None), ("id", "Bad"), ("id", "has_underscore"),
    ("id", "a" * 65), ("id", "a--b"), ("id", "a\n"),
    ("version", None), ("version", "1.0"), ("version", "01.0.0"),
    ("version", "1.0.0-01"), ("version", {"semver": "bad"}),
])
def test_A3_extension_identity_uses_pinned_grammar(extension, field, value):
    if value is None:
        del extension[field]
    else:
        extension[field] = value
    before = dict(registry.REGISTRY)
    with pytest.raises(ManifestInvalid) as caught:
        registry.register_extension(extension)
    assert caught.value.to_http() == (422, {
        "detail": "lab_manifest_invalid", "pointer": "/" + field, "block": extension.get("id"),
    })
    assert registry.REGISTRY == before


def test_A4_duplicate_seed_keys_are_refused(monkeypatch):
    seed = {"id": "duplicate", "version": "1.0.0", "capability": "read.value"}
    monkeypatch.setattr(registry.catalog, "seed_tools", lambda: [seed, dict(seed)])
    with pytest.raises(RuntimeError, match=r"duplicate.*1\.0\.0"):
        registry._seed_registry()


def test_A5_histogram_clamps_huge_integers_and_counts_drops():
    histogram = registry.LatencyHistogram()
    for value in (1 << 2000, 10 ** 10000, float("1e300")):
        histogram.observe(value)
    assert histogram.counts == [0] * 31 + [3]
    assert histogram.p95() == histogram.buckets[-1]
    for value in (None, "1", [], {}, object(), True, -1, float("nan"), float("inf"), -float("inf")):
        histogram.observe(value)
    assert histogram.dropped == 10
    assert sum(histogram.counts) == 3


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
