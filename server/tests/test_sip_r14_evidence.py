"""Offline sizing evidence contracts and frozen standalone compatibility."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

import solar_sizing_client as cloud
import solar_project_sizing as project
from leaf_cloud_client import canonical_bytes
from leaf_cloud_grants import CloudError
from solar_design_graph import GraphValidationError, serialize_graph
from test_w1_design_graph import graph as graph_fixture


REPO = Path(__file__).resolve().parents[2]
INTAKE = "b" * 64
MANIFEST = "sha256:" + "c" * 64
SMALL = {
    "cells": 60, "voc": 50, "isc": 10, "pmp": 400, "vmp": 40, "imp": 10,
    "bpmp": 0, "bvoc": -0.2, "alpha_sc": 0, "min_temp": 0,
    "simulation_results": {"standard": {
        "Conditions": "fixture", "max_module_voltage": 55,
        "string_design_voltage": 600, "string_length": 10,
    }},
}
SMALL_DERIVED = {
    "panels_in_sequence": 10,
    "voc_cold": {
        "passes": True, "override_accepted": False, "suggested_string_length": 0,
        "per_module": 52.5, "string_voltage": 525.0, "max_dc_voltage": 600.0,
    },
    "module_power_watts": 400.0,
    "output_graph_sha256": "1847203a6e251c2781bab8cd70eacb71a353c11f4467efe84620033580671941",
}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _response_span(raw):
    text = raw.decode("utf-8")
    decoder = json.JSONDecoder()
    offset = text.index("{") + 1
    while True:
        offset = re.compile(r"\s*").match(text, offset).end()
        key, offset = decoder.raw_decode(text, offset)
        offset = re.compile(r"\s*").match(text, offset).end()
        assert text[offset] == ":"
        start = re.compile(r"\s*").match(text, offset + 1).end()
        _, end = decoder.raw_decode(text, start)
        if key == "response":
            return text[start:end].encode("utf-8")
        offset = re.compile(r"\s*").match(text, end).end()
        assert text[offset] == ","
        offset += 1


@pytest.fixture
def recorded():
    manifest = json.loads((REPO / "scripts/fixtures/string_sizer_replay_manifest.json").read_bytes())
    entry = next(row for row in manifest["fixtures"] if row["id"] == "w1-string-length-recorded")
    raw = (REPO / entry["path"]).read_bytes()
    assert _sha(raw) == entry["fixture_sha256"]
    value = json.loads(raw)
    response = _response_span(raw)
    request = cloud.SizingRequest.model_validate(value["request"])
    assert _sha(cloud.wire_bytes(request)) == entry["request_wire_sha256"]
    assert _sha(response) == entry["response_wire_sha256"]
    return value["request"], response


@pytest.fixture
def source():
    graph = graph_fixture.__wrapped__()
    graph["project"]["zip_code"] = "44224"
    return graph


def _params(graph, request):
    return {"expected_rev": 0, "mode": "global", "requests": {graph["settings"]["id"]: request},
            "grant_ref": "fixture-grant", "confirm": True}


def _small_request(recorded):
    request = copy.deepcopy(recorded[0])
    request.update(module_name="M", full_inverter_name="I", max_voltage="600")
    return request


def _builtin():
    spec = importlib.util.spec_from_file_location(
        "r14_sizing_builtin", REPO / "server/builtins/solar_size_strings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _forbidden(*args, **kwargs):
    raise AssertionError("Pure operation used transport or grant resolution")


def _no_transport(monkeypatch):
    monkeypatch.setattr(cloud, "resolve_grant", _forbidden)
    monkeypatch.setattr(cloud, "post_string_length", _forbidden)
    monkeypatch.setattr(cloud.requests, "post", _forbidden)


@pytest.fixture
def case(source, recorded):
    params = _params(source, _small_request(recorded))
    raw = canonical_bytes(SMALL)
    identity = {"organization_id": "fixture-org", "project_id": "storage-project",
                "drawing_id": "storage-drawing", "job_id": "fixture-job", "actor_id": "fixture-actor",
                "target_settings_id": source["settings"]["id"]}
    producer = {"adapter_version": cloud.ADAPTER_VERSION, "service_identity": cloud.SIZING_URL,
                "http_status": 200, "worker_id": "fixture-worker", "attempt": 1}
    record = project.build_sizing_evidence(
        params, source, INTAKE, MANIFEST, raw, identity=identity,
        parent_version_id="fixture-version", producer=producer, classification="synthetic")
    expected = {"identity": copy.deepcopy(identity), "parent_version_id": "fixture-version",
                "parent_intake_sha256": INTAKE, "parameters": copy.deepcopy(params),
                "tool_manifest_sha256": MANIFEST, "producer": copy.deepcopy(producer),
                "classification": "synthetic", "evidence_sha256": cloud.digest(record)}
    plan = project.plan_sizing_request(params, source, INTAKE, MANIFEST)
    applied = project.apply_project_sizing(source, params, cloud.parse_response_bytes(raw),
                                           organization_id="fixture-org", job_id="fixture-job")
    return {"graph": source, "params": params, "raw": raw, "identity": identity, "producer": producer,
            "record": record, "expected": expected, "request_bytes": plan["wire_bytes"],
            "applied": applied}


def _prove(case, **changes):
    arguments = {"record": case["record"], "request_bytes": case["request_bytes"],
                 "response_bytes": case["raw"], "parent_graph": case["graph"],
                 "expected": case["expected"], "allow_synthetic_for_test": True}
    arguments.update(changes)
    return project.prove_sizing_evidence(**arguments)


def _reject_mutation(case, mutate):
    record, expected = copy.deepcopy((case["record"], case["expected"]))
    mutate(record)
    # Rebinding this test input pin ensures other checks independently catch the mutation.
    expected["evidence_sha256"] = cloud.digest(record)
    with pytest.raises(GraphValidationError, match="^SIP_R14_EVIDENCE_REJECTED:"):
        _prove(case, record=record, expected=expected)


def _derive(case, parsed):
    plan = project.plan_sizing_request(case["params"], case["graph"], INTAKE, MANIFEST)
    request = cloud.SizingRequest.model_validate(plan["normalized_request"])
    return project._derive_project_result(request, parsed)


def test_wire_and_canonical_digests_are_distinct(source, recorded):
    plan = project.plan_sizing_request(_params(source, recorded[0]), source, INTAKE, MANIFEST)
    assert len(plan["wire_bytes"]) == 350
    assert plan["wire_sha256"] == "c2adcc2616b290a64eacbdc9f3857447d5de567a3d297bb23b87d7bdabe269b5"
    assert plan["canonical_sha256"] == "c26514380548f96333945e9cc448eec4ffeb9d38e5c6b88623c197183d43ea8d"
    assert plan["wire_sha256"] != plan["canonical_sha256"]
    parsed = cloud.parse_response_bytes(recorded[1])
    assert len(recorded[1]) == 3862
    assert parsed["wire_sha256"] == "5d44f0bde6ec7ff27873b4902db5e07fb6004e0d5281db92ce00fa57c27b12ed"
    assert parsed["canonical_sha256"] == "456ffce22d3afd0def1c98dfb2ed67e902b7ccba6c814812b51e874f0b506a4e"


def test_stored_response_rederives_graph(case):
    result = _prove(case)
    assert set(result) == {"graph", "derived", "evidence_sha256"}
    assert result["derived"] == SMALL_DERIVED
    assert result["graph"] == case["applied"]["graph"]
    assert cloud.digest(result["graph"]) == SMALL_DERIVED["output_graph_sha256"]
    assert len(case["request_bytes"]) == 306 and len(case["raw"]) == 235
    assert _sha(case["raw"]) == "1f2851c49cb6d2fa2a1d531332cb040216f94234722633ef68294f6d2616b0b1"
    settings = result["graph"]["settings"]
    assert settings["panels_in_sequence"] == 10
    assert settings["voc_cold"] == SMALL_DERIVED["voc_cold"]
    record = settings["extra"]["string_sizing"]["records"][settings["id"]]
    assert record["sizing"]["voc_cold"] == settings["voc_cold"]
    assert result["graph"]["frames"][0]["module_power_watts"] == record["response"]["pmp"] == 400.0
    fractional = copy.deepcopy(SMALL)
    fractional["simulation_results"]["standard"]["string_length"] = 10.9
    applied = project.apply_project_sizing(
        case["graph"], case["params"], cloud.parse_response_bytes(canonical_bytes(fractional)),
        organization_id="fixture-org", job_id="fixture-job")
    assert applied["graph"]["settings"]["panels_in_sequence"] == 10
    # The stored wire bytes are the authority; the cached copy beside them is never trusted.
    parsed = cloud.parse_response_bytes(case["raw"])
    derived = _derive(case, parsed)
    assert derived == {key: value for key, value in SMALL_DERIVED.items()
                       if key != "output_graph_sha256"}
    assert derived["voc_cold"] is not parsed["sizing"]["voc_cold"]
    rewritten = cloud.parse_response_bytes(case["raw"])
    rewritten["response"]["pmp"] = 450
    rewritten["canonical_sha256"] = cloud.digest(rewritten["response"])
    with pytest.raises(GraphValidationError, match="^CLOUD_RESPONSE_INVALID:"):
        _derive(case, rewritten)
    with pytest.raises(GraphValidationError, match="^CLOUD_RESPONSE_INVALID:"):
        project.apply_project_sizing(case["graph"], case["params"], rewritten,
                                     organization_id="fixture-org", job_id="fixture-job")


def test_response_cannot_change_request_voltage_limit(case):
    response = copy.deepcopy(SMALL)
    response["simulation_results"]["standard"]["string_design_voltage"] = 601
    with pytest.raises(GraphValidationError, match="^CLOUD_RESPONSE_INVALID:"):
        project.apply_project_sizing(
            case["graph"], case["params"], cloud.parse_response_bytes(canonical_bytes(response)),
            organization_id="fixture-org", job_id="fixture-job")
    for voltage in ("not-a-voltage", "NaN", "Infinity", "0", "-1", "600.5", "100001"):
        params = copy.deepcopy(case["params"])
        next(iter(params["requests"].values()))["max_voltage"] = voltage
        with pytest.raises(GraphValidationError, match="^CLOUD_REQUEST_INVALID:"):
            project.plan_sizing_request(params, case["graph"], INTAKE, MANIFEST)


def test_project_accepts_shorter_real_plugin_length(case):
    plugin = json.loads((REPO / "server/tests/fixtures/w1_plugin_stringsizer_response.json").read_bytes())
    params = _params(case["graph"], plugin["request"])
    raw = canonical_bytes(plugin["response"])
    parsed = cloud.parse_response_bytes(raw)
    assert parsed["sizing"]["panels_in_sequence"] == 28
    before = copy.deepcopy((case["graph"], params, parsed))

    def build(parameters):
        return project.build_sizing_evidence(
            parameters, case["graph"], INTAKE, MANIFEST, raw, identity=case["identity"],
            parent_version_id="fixture-version", producer=case["producer"], classification="synthetic")

    with pytest.raises(GraphValidationError, match="^COLD_VOLTAGE_FAILED:"):
        build(params)
    length = parsed["sizing"]["voc_cold"]["suggested_string_length"]
    accepted = dict(params, accepted_string_length=length)
    evidence = build(accepted)
    assert evidence["input"]["parameters"]["accepted_string_length"] == length
    assert evidence["derived"]["panels_in_sequence"] == length
    cold = evidence["derived"]["voc_cold"]
    assert cold["passes"] is True and cold["override_accepted"] is False
    assert cold["suggested_string_length"] == 0
    assert cold["per_module"] == parsed["sizing"]["voc_cold"]["per_module"]
    plan = project.plan_sizing_request(accepted, case["graph"], INTAKE, MANIFEST)
    baseline = project.plan_sizing_request(params, case["graph"], INTAKE, MANIFEST)
    assert plan["wire_bytes"] == baseline["wire_bytes"]
    assert plan["canonical_sha256"] == baseline["canonical_sha256"]
    expected = dict(case["expected"], parameters=accepted, evidence_sha256=cloud.digest(evidence))
    proven = project.prove_sizing_evidence(
        evidence, plan["wire_bytes"], raw, case["graph"], expected=expected,
        allow_synthetic_for_test=True)
    graph = proven["graph"]
    target = graph["settings"]["id"]
    stored = cloud.require_sizing(graph)["records"][target]
    assert stored["accepted_string_length"] == length
    assert stored["sizing"]["panels_in_sequence"] == length
    assert stored["response"] == plugin["response"]
    assert stored["request"] == plugin["request"]
    for altered in (length - 1, None, True, "27", 27.0, 4097):
        changed = copy.deepcopy(graph)
        changed["settings"]["extra"]["string_sizing"]["records"][target]["accepted_string_length"] = altered
        with pytest.raises(GraphValidationError, match="^SIZING_CONFIRMATION_REQUIRED:"):
            cloud.require_sizing(changed)
    with pytest.raises(GraphValidationError, match="^CLOUD_RESPONSE_INVALID:"):
        build(dict(params, accepted_string_length=29))
    with pytest.raises(GraphValidationError, match="^COLD_VOLTAGE_FAILED:"):
        build(dict(params, accepted_string_length=28))
    assert (case["graph"], params, parsed) == before


@pytest.mark.parametrize("length", [0, 4097, True, "27", 27.0, None])
def test_project_invalid_accepted_length(case, length):
    params = dict(case["params"], accepted_string_length=length)
    with pytest.raises(GraphValidationError, match="^INVALID_SIZING_REQUEST:"):
        project.plan_sizing_request(params, case["graph"], INTAKE, MANIFEST)
    evidence = copy.deepcopy(case["record"])
    evidence["input"]["parameters"]["accepted_string_length"] = length
    with pytest.raises(GraphValidationError, match="^SIP_R14_EVIDENCE_REJECTED:"):
        project.validate_sizing_evidence(evidence)


@pytest.mark.parametrize("mode", ["zones", "manual-global"])
def test_project_accepted_length_requires_global(case, mode):
    params = dict(case["params"], mode=mode, accepted_string_length=9)
    with pytest.raises(GraphValidationError, match="^INVALID_SIZING_REQUEST:"):
        project.plan_sizing_request(params, case["graph"], INTAKE, MANIFEST)


def test_absent_accepted_length_preserves_normalized_params_and_record(case):
    plan = project.plan_sizing_request(case["params"], case["graph"], INTAKE, MANIFEST)
    original_params = {**case["params"], "cancel": False}
    assert canonical_bytes(plan["normalized_params"]) == canonical_bytes(original_params)
    parsed = cloud.parse_response_bytes(case["raw"])
    request = cloud.SizingRequest.model_validate(plan["normalized_request"])
    original_record = {
        "endpoint": cloud.SIZING_URL, "adapter_version": cloud.ADAPTER_VERSION,
        "tenant_id": "fixture-org", "job_id": "fixture-job", "request": request.wire(),
        "response": parsed["response"], "sizing": parsed["sizing"],
        "request_sha256": cloud.digest(request.wire()),
        "response_sha256": parsed["canonical_sha256"], "wire_response_sha256": parsed["wire_sha256"],
    }
    target = case["graph"]["settings"]["id"]
    record = case["applied"]["records"][target]
    assert canonical_bytes(record) == canonical_bytes(original_record)
    assert cloud.digest({"parameters": plan["normalized_params"], "record": record}) == cloud.digest(
        {"parameters": original_params, "record": original_record})


def test_request_wire_limit(case, monkeypatch):
    real_wire = cloud.wire_bytes
    padded = case["request_bytes"] + b" " * (project.MAX_REQUEST_BYTES - len(case["request_bytes"]))
    with monkeypatch.context() as patch:
        patch.setattr(cloud, "wire_bytes", lambda request: padded)
        plan = project.plan_sizing_request(case["params"], case["graph"], INTAKE, MANIFEST)
        assert plan["wire_bytes"] == padded
    with monkeypatch.context() as patch:
        patch.setattr(cloud, "wire_bytes", lambda request: padded + b" ")
        patch.setattr(cloud, "prepare_graph_sizing", _forbidden)
        with pytest.raises(GraphValidationError, match="^CLOUD_REQUEST_INVALID:"):
            project.plan_sizing_request(case["params"], case["graph"], INTAKE, MANIFEST)
    params = copy.deepcopy(case["params"])
    next(iter(params["requests"].values()))["module_name"] = "x" * 65536
    monkeypatch.setattr(cloud, "_bounded_json", _forbidden)
    monkeypatch.setattr(cloud, "wire_bytes", real_wire)
    with pytest.raises(GraphValidationError, match="^CLOUD_REQUEST_INVALID:"):
        project.plan_sizing_request(params, case["graph"], INTAKE, MANIFEST)


def test_missing_or_invalid_power_refuses(case):
    before = copy.deepcopy(case["graph"])
    for power in (None, 0, -1, 1000001, "400", True):
        response = copy.deepcopy(SMALL)
        if power is None:
            del response["pmp"]
        else:
            response["pmp"] = power
        with pytest.raises(GraphValidationError, match="^CLOUD_RESPONSE_INVALID:"):
            project.apply_project_sizing(
                case["graph"], case["params"], project._parse_response(canonical_bytes(response)),
                organization_id="fixture-org", job_id="fixture-job")
        assert case["graph"] == before
    # Each power the parser admits is refused by the project's own power check.
    for power in (0, -1, 1000001):
        response = copy.deepcopy(SMALL)
        response["pmp"] = power
        parsed = project._parse_response(canonical_bytes(response))
        with pytest.raises(GraphValidationError, match="^CLOUD_RESPONSE_INVALID:"):
            _derive(case, parsed)


def test_manual_and_standalone_unchanged(source, monkeypatch):
    _no_transport(monkeypatch)
    params = {"expected_rev": 0, "mode": "manual-global", "confirm": True}
    before = copy.deepcopy((source, params))
    result = _builtin().size_strings(source, params, tenant_id="fixture-org", job_id="fixture-job")
    assert cloud.digest(result) == "9719226422ab196691d82996ad9c3ac1d0eb89e81a70b25079769c1a621dbeec"
    assert result["records"] == {} and result["confirmed"] is True
    assert result["graph"]["settings"]["voc_cold"] == cloud.manual_voltage()
    assert (source, params) == before


def test_standalone_frozen_capture(source, recorded, monkeypatch):
    captured = []

    class Reply:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_content(self, chunk_size):
            yield recorded[1]

    def post(url, **kwargs):
        captured.append((url, kwargs))
        return Reply()

    monkeypatch.setattr(cloud, "resolve_grant", lambda *args: SimpleNamespace(access_token="fixture-token"))
    monkeypatch.setattr(cloud.requests, "post", post)
    params = _params(source, recorded[0])
    before = copy.deepcopy((source, params))
    result = _builtin().size_strings(source, params, tenant_id="fixture-org", job_id="fixture-job")
    assert len(captured) == 1 and captured[0][0] == cloud.SIZING_URL
    assert _sha(captured[0][1]["data"]) == "c2adcc2616b290a64eacbdc9f3857447d5de567a3d297bb23b87d7bdabe269b5"
    record = result["records"][source["settings"]["id"]]
    assert cloud.digest(record) == "4d56af03ffa1412389d58db8224211e12f086b928698eddc47ba617057fe0fe8"
    assert cloud.digest(result) == "2b3e9acadf244ffd4b67e6aa4623dfef2eb14899f05866969342758d7faa6859"
    assert _sha(serialize_graph(result["graph"]).encode("utf-8")) == "c2aff55d208db02ca4294232649b5c85d9f819656225d1d2436736a1b9e59ac1"
    assert (source, params) == before


def test_duplicate_response_keys_refuse():
    raw = canonical_bytes(SMALL)
    mutations = [raw.replace(b'"pmp":400', b'"pmp":400,"pmp":400'),
                 raw.replace(b'"Conditions":"fixture"', b'"Conditions":"fixture","Conditions":"fixture"')]
    for mutated in mutations:
        assert mutated != raw
        with pytest.raises(CloudError) as error:
            cloud.parse_response_bytes(mutated)
        assert error.value.classification == "cloud_response_invalid"


def test_nonfinite_response_numbers_refuse():
    raw = canonical_bytes(SMALL)
    for value in (b"NaN", b"Infinity", b"-Infinity", b"1e999"):
        with pytest.raises(CloudError) as error:
            cloud.parse_response_bytes(raw.replace(b'"pmp":400', b'"pmp":' + value))
        assert error.value.classification == "cloud_response_invalid"


def test_response_byte_limit(case):
    padded = case["raw"] + b" " * (cloud.MAX_RESPONSE_BYTES - len(case["raw"]))
    parsed = cloud.parse_response_bytes(padded)
    assert parsed["sizing"] == {key: SMALL_DERIVED[key] for key in ("panels_in_sequence", "voc_cold")}
    assert parsed["wire_sha256"] == _sha(padded)
    assert parsed["canonical_sha256"] == _sha(case["raw"])
    result = project.apply_project_sizing(case["graph"], case["params"], parsed,
                                          organization_id="fixture-org", job_id="fixture-job")
    assert result["graph"]["settings"]["voc_cold"] == SMALL_DERIVED["voc_cold"]
    for raw in (padded + b" ", bytearray(case["raw"]), None):
        with pytest.raises(CloudError) as error:
            cloud.parse_response_bytes(raw)
        assert error.value.classification == "cloud_response_invalid"


def test_response_schema_refuses():
    missing, extra, wrong = copy.deepcopy((SMALL, SMALL, SMALL))
    del missing["voc"]
    extra["unknown"] = 1
    wrong["cells"] = "60"
    for response in ({}, missing, extra, wrong):
        with pytest.raises(CloudError) as error:
            cloud.parse_response_bytes(canonical_bytes(response))
        assert error.value.classification == "cloud_response_invalid"


def test_synthetic_evidence_refused_by_default(case):
    with pytest.raises(GraphValidationError, match="^SIP_R14_SYNTHETIC_EVIDENCE_REFUSED:"):
        project.prove_sizing_evidence(case["record"], case["request_bytes"], case["raw"],
                                      case["graph"], expected=case["expected"])


def test_synthetic_evidence_explicit_acceptance(case):
    assert _prove(case)["graph"] == case["applied"]["graph"]
    for flag in ("true", 1, False, None):
        with pytest.raises(GraphValidationError, match="^SIP_R14_SYNTHETIC_EVIDENCE_REFUSED:"):
            _prove(case, allow_synthetic_for_test=flag)


def test_request_bytes_are_exact(case):
    reordered = dict(reversed(list(json.loads(case["request_bytes"]).items())))
    for raw in (json.dumps(reordered, separators=(",", ":")).encode(), case["request_bytes"] + b" "):
        assert raw != case["request_bytes"]
        with pytest.raises(GraphValidationError, match="^SIP_R14_EVIDENCE_REJECTED:"):
            _prove(case, request_bytes=raw)


def test_response_wire_identity_is_exact(case):
    with pytest.raises(GraphValidationError, match="^SIP_R14_EVIDENCE_REJECTED:"):
        _prove(case, response_bytes=case["raw"] + b" ")


def test_all_digest_bindings_are_checked(case):
    for envelope in ("request", "response"):
        for field in ("wire_sha256", "canonical_sha256"):
            _reject_mutation(case, lambda r, e=envelope, f=field: r[e].update({f: "0" * 64}))
    expected = copy.deepcopy(case["expected"])
    expected["evidence_sha256"] = "0" * 64
    with pytest.raises(GraphValidationError, match="^SIP_R14_EVIDENCE_REJECTED:"):
        _prove(case, expected=expected)


def test_exchange_identity_is_bound(case):
    for key in case["identity"]:
        _reject_mutation(case, lambda r, k=key: r["identity"].update({k: "another-identity"}))
    identity = copy.deepcopy(case["identity"])
    identity["job_id"] = "other-job"
    record = project.build_sizing_evidence(
        case["params"], case["graph"], INTAKE, MANIFEST, case["raw"], identity=identity,
        parent_version_id="fixture-version", producer=case["producer"], classification="synthetic")
    expected = copy.deepcopy(case["expected"])
    expected["evidence_sha256"] = cloud.digest(record)
    with pytest.raises(GraphValidationError, match="^SIP_R14_EVIDENCE_REJECTED:"):
        _prove(case, record=record, expected=expected)


def test_input_identity_is_bound(case):
    for key, value in (
            ("parent_version_id", "other-version"), ("parent_intake_sha256", "0" * 64),
            ("graph_sha256", "0" * 64), ("sizing_basis_sha256", "0" * 64),
            ("tool_manifest_sha256", "sha256:" + "0" * 64)):
        _reject_mutation(case, lambda r, k=key, v=value: r["input"].update({k: v}))
    _reject_mutation(case, lambda r: r["input"]["parameters"].update(expected_rev=1))
    _reject_mutation(case, lambda r: next(iter(r["input"]["parameters"]["requests"].values())).update(
        module_name="other-module"))
    graph = copy.deepcopy(case["graph"])
    graph["source_hash"] = "d" * 64
    with pytest.raises(GraphValidationError, match="^SIP_R14_EVIDENCE_REJECTED:"):
        _prove(case, parent_graph=graph)


def test_classification_cannot_be_relabelled(case):
    _reject_mutation(case, lambda r: r.update(classification="live"))
    live = copy.deepcopy(case["record"])
    live["classification"] = "live"
    expected = copy.deepcopy(case["expected"])
    expected.update(classification="live", evidence_sha256=cloud.digest(live))
    assert _prove(case, record=live, expected=expected, allow_synthetic_for_test=False)["graph"] == case["applied"]["graph"]


def test_producer_and_public_grant_are_bound(case):
    for key, value in (
            ("adapter_version", cloud.LEGACY_ADAPTER_VERSION), ("service_identity", "https://other.example"),
            ("http_status", 201), ("worker_id", "other-worker"), ("attempt", 2)):
        _reject_mutation(case, lambda r, k=key, v=value: r["producer"].update({k: v}))
    _reject_mutation(case, lambda r: r["grant"].update(reference="other-grant"))
    def relabel_grant(record):
        record["grant"]["reference"] = "other-grant"
        record["input"]["parameters"]["grant_ref"] = "other-grant"
    _reject_mutation(case, relabel_grant)


def test_derived_record_fields_are_recomputed(case):
    for key, value in (("panels_in_sequence", 11), ("module_power_watts", 401.0),
                       ("output_graph_sha256", "0" * 64)):
        _reject_mutation(case, lambda r, k=key, v=value: r["derived"].update({k: v}))
    for key, value in (("per_module", 53.0), ("string_voltage", 526.0), ("max_dc_voltage", 601.0),
                       ("passes", False), ("override_accepted", True), ("suggested_string_length", 1)):
        _reject_mutation(case, lambda r, k=key, v=value: r["derived"]["voc_cold"].update({k: v}))
    parsed = cloud.parse_response_bytes(case["raw"])
    parsed["sizing"]["panels_in_sequence"] = 9
    with pytest.raises(GraphValidationError, match="^CLOUD_RESPONSE_INVALID:"):
        project.apply_project_sizing(case["graph"], case["params"], parsed,
                                     organization_id="fixture-org", job_id="fixture-job")


def _reject_shape(case, mutate):
    record = copy.deepcopy(case["record"])
    mutate(record)
    # The structural validator itself refuses the malformed record, before any proof runs.
    with pytest.raises(GraphValidationError, match="^SIP_R14_EVIDENCE_REJECTED:"):
        project.validate_sizing_evidence(record)
    _reject_mutation(case, mutate)


def test_evidence_shape_is_strict(case):
    project.validate_sizing_evidence(copy.deepcopy(case["record"]))
    for envelope in (None, "identity", "input", "request", "response", "producer", "grant", "derived"):
        def extra(record, envelope=envelope):
            (record if envelope is None else record[envelope])["access_token"] = "forbidden"
        _reject_shape(case, extra)
        def missing(record, envelope=envelope):
            value = record if envelope is None else record[envelope]
            value.pop(next(iter(value)))
        _reject_shape(case, missing)
    mutations = [
        lambda r: r["producer"].update(http_status=True),
        lambda r: r["producer"].update(attempt=True),
        lambda r: r["input"]["parameters"].update(expected_rev=False),
        lambda r: r["derived"].update(panels_in_sequence=True),
        lambda r: r["derived"].update(module_power_watts=400),
        lambda r: r["derived"]["voc_cold"].update(suggested_string_length=False),
        lambda r: r["derived"]["voc_cold"].update(per_module=True),
        lambda r: r["request"].update(wire_sha256="A" * 64),
        lambda r: r["input"].update(tool_manifest_sha256="c" * 64),
        lambda r: r["identity"].update(actor_id=" actor "),
        lambda r: r["identity"].update(actor_id="actor\u0000"),
        lambda r: r["request"]["normalized"]["racking_params"].update(access_token="forbidden"),
        lambda r: r["input"]["parameters"].update(cancel=0),
    ]
    for mutate in mutations:
        _reject_shape(case, mutate)
    for field in ("identity", "producer", "evidence_sha256"):
        expected = copy.deepcopy(case["expected"])
        del expected[field]
        with pytest.raises(GraphValidationError, match="^SIP_R14_EVIDENCE_REJECTED:"):
            _prove(case, expected=expected)


def test_failed_guard_never_applies(case):
    response = copy.deepcopy(SMALL)
    response["simulation_results"]["standard"]["string_length"] = 12
    before = copy.deepcopy(case["graph"])
    parsed = cloud.parse_response_bytes(canonical_bytes(response))
    assert parsed["sizing"]["voc_cold"]["passes"] is False
    with pytest.raises(GraphValidationError, match="^COLD_VOLTAGE_FAILED:"):
        project.apply_project_sizing(case["graph"], case["params"], parsed,
                                     organization_id="fixture-org", job_id="fixture-job")
    assert case["graph"] == before
    with pytest.raises(GraphValidationError, match="^COLD_VOLTAGE_FAILED:"):
        _derive(case, parsed)


def test_project_mode_and_confirmation_are_frozen(case):
    for mode in ("zones", "manual-global", None):
        params = copy.deepcopy(case["params"])
        if mode is None:
            del params["mode"]
        else:
            params["mode"] = mode
        with pytest.raises(GraphValidationError, match="^INVALID_SIZING_MODE:"):
            project.plan_sizing_request(params, case["graph"], INTAKE, MANIFEST)
    for key, value in (("confirm", False), ("confirm", 1), ("cancel", True), ("cancel", 0),
                       ("unexpected", True)):
        params = copy.deepcopy(case["params"])
        params[key] = value
        with pytest.raises(GraphValidationError, match="^INVALID_SIZING_REQUEST:"):
            project.plan_sizing_request(params, case["graph"], INTAKE, MANIFEST)


def test_project_preconditions_are_reused(case):
    graph, params = copy.deepcopy((case["graph"], case["params"]))
    mutations = [
        ("STALE_GRAPH_REVISION", lambda g, p: p.update(expected_rev=1)),
        ("SIZING_PROJECT_MISMATCH", lambda g, p: g["project"].update(zip_code="90210")),
        ("INVALID_SIZING_COVERAGE", lambda g, p: p.update(requests={})),
        ("UNRESOLVED_UNITS", lambda g, p: g["project"]["units"].update(meters_per_unit=1)),
    ]
    for code, mutate in mutations:
        g, p = copy.deepcopy((graph, params))
        mutate(g, p)
        with pytest.raises(GraphValidationError, match="^" + code + ":"):
            project.plan_sizing_request(p, g, INTAKE, MANIFEST)
    empty = copy.deepcopy(graph)
    for key in ("panels", "frames", "strings", "inverters", "routes", "schedules", "electrical_zones"):
        empty[key] = []
    with pytest.raises(GraphValidationError, match="^MISSING_PANEL:"):
        project.plan_sizing_request(params, empty, INTAKE, MANIFEST)
    params = copy.deepcopy(case["params"])
    params["grant_ref"] = "bad ref!"
    with pytest.raises(GraphValidationError, match="^CLOUD_REQUEST_INVALID:"):
        project.plan_sizing_request(params, graph, INTAKE, MANIFEST)


def test_planning_application_and_proof_never_use_transport(case, monkeypatch):
    _no_transport(monkeypatch)
    monkeypatch.setattr(cloud, "size", _forbidden)
    plan = project.plan_sizing_request(case["params"], case["graph"], INTAKE, MANIFEST)
    parsed = cloud.parse_response_bytes(case["raw"])
    applied = project.apply_project_sizing(case["graph"], case["params"], parsed,
                                          organization_id="fixture-org", job_id="fixture-job")
    record = project.build_sizing_evidence(
        case["params"], case["graph"], INTAKE, MANIFEST, case["raw"], identity=case["identity"],
        parent_version_id="fixture-version", producer=case["producer"], classification="synthetic")
    assert plan["wire_bytes"] == case["request_bytes"]
    assert record == case["record"]
    assert _prove(case)["graph"] == applied["graph"]


def test_inputs_and_returned_values_are_isolated(case):
    before = copy.deepcopy(case)
    plan = project.plan_sizing_request(case["params"], case["graph"], INTAKE, MANIFEST)
    parsed = cloud.parse_response_bytes(case["raw"])
    parsed_before = copy.deepcopy(parsed)
    applied = project.apply_project_sizing(case["graph"], case["params"], parsed,
                                          organization_id="fixture-org", job_id="fixture-job")
    built = project.build_sizing_evidence(
        case["params"], case["graph"], INTAKE, MANIFEST, case["raw"], identity=case["identity"],
        parent_version_id="fixture-version", producer=case["producer"], classification="synthetic")
    validated = project.validate_sizing_evidence(case["record"])
    proven = _prove(case)
    prepared = cloud.prepare_graph_sizing(case["graph"], case["params"])
    prepared_before = copy.deepcopy(prepared)
    legacy = cloud.sizing_record(cloud.SizingRequest.model_validate(plan["normalized_request"]),
                                  parsed, "fixture-org", "fixture-job")
    records = {case["graph"]["settings"]["id"]: legacy}
    records_before = copy.deepcopy(records)
    extracted = cloud.apply_sizing_records(prepared, records)
    assert records == records_before
    plan["normalized_request"]["module_name"] = "changed"
    plan["normalized_params"]["requests"].clear()
    applied["graph"]["settings"]["extra"].clear()
    applied["records"].clear()
    built["identity"]["job_id"] = "changed"
    validated["derived"]["voc_cold"]["per_module"] = 1.0
    proven["graph"]["frames"].clear()
    proven["derived"]["voc_cold"].clear()
    extracted["graph"]["settings"]["extra"].clear()
    extracted["records"].clear()
    assert records == records_before
    legacy["response"].clear()
    assert case == before and parsed == parsed_before and prepared == prepared_before
    # The legacy return is isolated from the parser; records are isolated by application.
    assert records_before[case["graph"]["settings"]["id"]]["response"] == SMALL


def test_unicode_and_optional_request_normalization(source, recorded):
    request = _small_request(recorded)
    request.update(module_name="Mé", module_parameters=None)
    plan = project.plan_sizing_request(_params(source, request), source, INTAKE, MANIFEST)
    assert len(plan["wire_bytes"]) == 308
    assert plan["wire_sha256"] == "22d60c348a1aebac23535f2c51c88e93bfbfe998427875cf7b4aeb97e43b56dc"
    assert plan["canonical_sha256"] == "dc2e8d3af9326d49c92179f1b2f1783bc142ca4f0fb6605f04d27c2d1441ead8"
    assert b"M\xc3\xa9" in plan["wire_bytes"]
    assert "module_parameters" not in plan["normalized_request"]
    assert "module_parameters" not in next(iter(plan["normalized_params"]["requests"].values()))
