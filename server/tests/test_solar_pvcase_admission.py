"""G33 producer admission through real filesystem publication and terminal proofs."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

import solar_artifacts as artifacts
import solar_design_graph as sdg
import solar_local_graph as local
import solar_local_read as read
import solar_pvcase_sources as sources
import solar_solve_results as results
import store
from test_solar_pvcase_conversion import canonical, ground_envelope, seed
from test_solar_tool_pvcase_convert import PROOF_TENANT, REJECTED, commit, prove
from test_w1_solve_commit import publish, seed as store_seed


PINS = {
    "Roof": {
        "seed": "6a9d88c3a678a6f1d57cb86b0f2aed5ab8a5770112df1a67c46df5fbfa4b7044",
        "source_length": 274972,
        "source_sha": "557c8d941e1b35d9544149279c8f203bca288cf28f94aed5e40548842745502f",
        "source_id": "ef1b2c8262205b3340a966cef5260f2dac10c64a1cb9fa6616bd80cb46f48372",
        "convert": "7009fbd42b3b3963843461f7ca44d9730cd62260194d34621381e0b9c1604587",
        "solve": "607031e3eebfb00f0fe97bb55844c0b67b61e4bd403317aaba842252ba70a63c",
        "strings": 88,
        "first_string": "leaf:string:048a715f-5705-4548-aa9d-a09bf1b01a59",
        "output": "6629cd725d8063fd3162f25abebe1cd77b4ce2482b9416f5e4340d149cbe077e",
        "export_length": 475691,
        "export_sha": "5ad8c994799081dd33dabc25dd50772feeb946f2f3e3caf56219400c51cf209a",
        "string_length": 37338,
        "string_sha": "4d5e8e068326032db0b663f5db0a0c9ec89e23e835ac7ead26b356553cd5b09e",
        "summary": {"status": "written", "selected_strings": 88, "groups": 11,
                    "grouped_strings": 88, "lines": 1038},
    },
    "Ground": {
        "seed": "d98fb1771f9de7740e9b09952cf7d0aab90c3aad33b9d7dfdb1bdb4aa45d8fe1",
        "source_length": 501,
        "source_sha": "37aac4a0747c5b780213a7d01edfa25fe9e3dabda057c110a4645f876cf95f64",
        "source_id": "0685eb4060d08b54ed79efa71b9ba3c326bdcf937cc8f2a11ba465453b68e705",
        "convert": "fba0fae6bdb4dc59b31d2534034d1db967a77aabd7a50b16a944de6da785d37b",
        "solve": "8417b77ae7eb0dd791f73482718a54bbe5ddfef3a577aba5ab04467ecc7a4682",
        "strings": 1,
        "first_string": "leaf:string:5fb0e6dd-fa5e-4c7a-9ec7-758c1743e768",
        "output": "326443d84a6eb1bea4d96bdcdbc0bb4ccd968a753dbaf5b93f9056f9a6080d9e",
        "export_length": 624,
        "export_sha": "398ace876c0efb2d136f56e269112b77ba17e806b1fb8c51dd9bb19cc8f5d1a8",
        "string_length": 562,
        "string_sha": "3d262fdc17814fdc77747f4318d6d01f3ae1f3186789f5d5a374280b2880345f",
        "summary": {"status": "written", "selected_strings": 1, "groups": 1,
                    "grouped_strings": 1, "lines": 21},
    },
}


def versions(backend):
    return [entry["v"] for entry in store.load_manifest(backend, PROOF_TENANT, "solar")["versions"]]


def artifact_keys(backend):
    keys = backend.drawing_object_keys(PROOF_TENANT, "solar")
    assert keys is not None
    prefix = store.drawing_prefix(PROOF_TENANT, "solar") + "/artifacts/"
    return {key for key in keys if key.startswith(prefix)}


def reopen(backend, version="head"):
    # Reconstruct the adapter from its filesystem root, rather than retaining graph objects.
    return local.resolve_graph_context(
        store.FilesystemBackend(backend.root), PROOF_TENANT, "solar", version)


def admitted_chain(tmp_path, monkeypatch, design="Ground"):
    base = seed("in", "Roof") if design == "Roof" else seed("m", "Ground")
    if design == "Roof":
        intake_path = Path(__file__).resolve().parents[2] / "docs/parity/evidence/rooftop/pvcase/intake.json"
        envelope = {"schema": "leaf.pvcase-g33.v1",
                    "intake": json.loads(intake_path.read_text(encoding="utf-8"))}
    else:
        envelope = ground_envelope()
    data = canonical(envelope)
    backend, _ = store_seed(tmp_path, monkeypatch, base)
    assert results.digest(base) == PINS[design]["seed"]
    receipt = sources.import_pvcase_source(backend, PROOF_TENANT, "solar", data)
    assert versions(backend) == [1]
    assert reopen(backend)["graph_sha256"] == results.digest(base)
    convert_params = {"expected_rev": base["rev"],
                      "source_artifact_id": receipt["source"]["artifact_id"]}
    converted = commit(backend, "solar-pvcase-convert", convert_params, 1, "convert-job")
    solve_params = {"expected_rev": converted["after_rev"]}
    solved = commit(backend, "solar-pvcase-solve", solve_params, 2, "solve-job")
    return backend, receipt, data, convert_params, converted, solve_params, solved


def read_output(backend, tool, version=3, params=None, job="read-job"):
    params = {"drawing_id": "solar"} if params is None else params
    result = read.run_local_graph_read(
        backend, PROOF_TENANT, tool, params, drawing_id="solar", source_version=version, job_id=job)
    proof = read.graph_read_provenance(
        result, params, PROOF_TENANT, job, tool, version,
        backend=store.FilesystemBackend(backend.root))
    assert sorted(proof) == ["adapter", "execution_mode", "graph_sha256", "output_sha256",
                             "request_sha256", "source_version"]
    assert proof["source_version"] == version
    for key in ("graph_sha256", "output_sha256", "request_sha256"):
        assert proof[key] == result[key]
    return result


def artifact_bytes(backend, reference, filename, length, sha, version):
    assert reference["filename"] == filename
    assert reference["media_type"] == "application/json"
    assert reference["byte_length"] == length
    assert reference["content_sha256"] == sha
    assert reference["source_version"] == version
    assert reference["schema"] == artifacts.REF_SCHEMA
    assert reference["download"] == "/api/drawings/solar/artifacts/" + reference["artifact_id"]
    meta, data = artifacts.read_artifact(
        store.FilesystemBackend(backend.root), PROOF_TENANT, "solar", reference["artifact_id"])
    for key in ("artifact_id", "filename", "media_type", "byte_length", "content_sha256", "source_version"):
        assert meta[key] == reference[key]
    assert meta["tenant_id"] == PROOF_TENANT and meta["drawing_id"] == "solar"
    assert len(data) == length and hashlib.sha256(data).hexdigest() == sha
    return data


def assert_chain_pins(chain, design):
    backend, receipt, data, cp, converted, sp, solved = chain
    pin = PINS[design]
    assert receipt["source"]["artifact_id"] == pin["source_id"]
    assert len(data) == pin["source_length"]
    assert hashlib.sha256(data).hexdigest() == pin["source_sha"]
    source_ref = receipt["source"]
    assert artifact_bytes(backend, source_ref, "pvcase-g33-source.json", pin["source_length"],
                          pin["source_sha"], 1) == data
    for result, params, tool, version, job, sha in (
        (converted, cp, "solar-pvcase-convert", 1, "convert-job", pin["convert"]),
        (solved, sp, "solar-pvcase-solve", 2, "solve-job", pin["solve"]),
    ):
        assert (result["graph_sha256"], result["before_rev"], result["after_rev"],
                result["new_version"]["version"]) == (sha, version - 1, version, version + 1)
        assert prove(store.FilesystemBackend(backend.root), result, tool, params, version, job)["new_version"] == version + 1
        assert reopen(backend, version + 1)["graph_sha256"] == sha
    graph = reopen(backend, 3)["graph"]
    assert (len(graph["strings"]), graph["strings"][0]["id"]) == (pin["strings"], pin["first_string"])
    exported = read_output(backend, "solar-pvcase-export")
    assert exported["graph_sha256"] == pin["solve"]
    assert sorted(exported["output"]) == ["artifact", "summary"]
    assert exported["output_sha256"] == pin["output"]
    refs = artifacts.artifact_references(exported["output"])
    assert len(refs) == 1
    artifact_bytes(backend, refs[0], "PVcaseAssignments.json", pin["export_length"], pin["export_sha"], 3)
    string_data = read_output(backend, "solar-string-data", job="string-data-job")
    assert string_data["graph_sha256"] == pin["solve"]
    assert string_data["output"]["summary"] == pin["summary"]
    refs = artifacts.artifact_references(string_data["output"])
    assert len(refs) == 1
    content = artifact_bytes(backend, refs[0], "StringData.json", pin["string_length"], pin["string_sha"], 3)
    explicit = read_output(backend, "solar-string-data", params={
        "drawing_id": "solar", "string_refs": [s["id"] for s in graph["strings"]]}, job="explicit-job")
    assert explicit["graph_sha256"] == pin["solve"]
    assert explicit["output"]["summary"] == pin["summary"]
    explicit_refs = artifacts.artifact_references(explicit["output"])
    assert len(explicit_refs) == 1
    # Request-bound artifact ids differ; the exported content and media metadata are identical.
    assert artifact_bytes(backend, explicit_refs[0], "StringData.json", pin["string_length"],
                          pin["string_sha"], 3) == content
    assert versions(backend) == [1, 2, 3]


def second_source(backend):
    envelope = ground_envelope()
    envelope["intake"]["panel_groups"][0]["rows"][0][0]["x"] = 2
    return sources.import_pvcase_source(backend, PROOF_TENANT, "solar", canonical(envelope))


def refuses(code, operation):
    with pytest.raises(sdg.GraphValidationError) as caught:
        operation()
    assert caught.value.code == code


def test_pvcase_capture_convert_solve_export_roof(tmp_path, monkeypatch):
    assert_chain_pins(admitted_chain(tmp_path, monkeypatch, "Roof"), "Roof")


def test_pvcase_capture_convert_solve_export_ground(tmp_path, monkeypatch):
    chain = admitted_chain(tmp_path, monkeypatch)
    assert_chain_pins(chain, "Ground")
    backend = chain[0]
    converted = local.resolve_graph_context(backend, PROOF_TENANT, "solar", 2)["graph"]
    assert len(converted["frames"]) == 1 and len(converted["panels"]) == 2
    assert all("tracker" not in frame and "ground_slots" not in frame for frame in converted["frames"])
    assert converted["inverters"] == []
    solved = reopen(backend, 3)["graph"]
    assert solved["inverters"] == []
    assert solved["strings"][0]["module_count"] == 2


def test_pvcase_conversion_reopen_and_historical_proof(tmp_path, monkeypatch):
    backend, receipt, _, cp, converted, sp, solved = admitted_chain(tmp_path, monkeypatch)
    second = second_source(backend)
    assert second["source"]["artifact_id"] != receipt["source"]["artifact_id"]
    backend = store.FilesystemBackend(backend.root)
    assert prove(backend, converted, "solar-pvcase-convert", cp, 1, "convert-job")["new_version"] == 2
    assert prove(backend, solved, "solar-pvcase-solve", sp, 2, "solve-job")["new_version"] == 3
    with pytest.raises(ValueError, match=REJECTED):
        prove(backend, dict(converted, graph_sha256=solved["graph_sha256"]),
              "solar-pvcase-convert", cp, 1, "convert-job")
    with pytest.raises(ValueError, match=REJECTED):
        prove(backend, converted, "solar-pvcase-convert",
              dict(cp, source_artifact_id=second["source"]["artifact_id"]), 1, "convert-job")
    assert versions(backend) == [1, 2, 3]


def test_pvcase_conversion_refuses_second_import(tmp_path, monkeypatch):
    backend, _, _, cp, converted, _, _ = admitted_chain(tmp_path, monkeypatch)
    snapshot = (versions(backend), reopen(backend)["graph_sha256"], artifact_keys(backend))
    refuses("PVCASE_EMPTY_TARGET_REQUIRED", lambda: commit(
        backend, "solar-pvcase-convert", dict(cp, expected_rev=2), 3, "second-convert-job"))
    assert (versions(backend), reopen(backend)["graph_sha256"], artifact_keys(backend)) == snapshot
    assert versions(backend) == [1, 2, 3]
    replay = commit(backend, "solar-pvcase-convert", cp, 1, "convert-job")
    assert {k: v for k, v in replay.items() if k != "replayed"} == {
        k: v for k, v in converted.items() if k != "replayed"}
    assert versions(backend) == [1, 2, 3]


def test_pvcase_outputs_follow_current_graph(tmp_path, monkeypatch):
    backend, receipt, data, _, _, _, _ = admitted_chain(tmp_path, monkeypatch)
    before = reopen(backend, 3)["graph"]
    route = copy.deepcopy(before["strings"][0]["route"])
    after = copy.deepcopy(before)
    after["panels"][0]["centre"] = [6.0, 8.0]
    after = results.finish_mutation(before, after, "fixture-panel-move")
    # The registry has no panel move tool (equipment-move only moves inverters).
    # test_w1_solve_commit.publish calls the real fenced results.publish_version.
    publication = publish(backend, 3, before, after, job_id="panel-move-job")
    assert publication["version"] == 4 and versions(backend) == [1, 2, 3, 4]
    exported = read_output(backend, "solar-pvcase-export", 4)
    ref = artifacts.artifact_references(exported["output"])[0]
    _, content = artifacts.read_artifact(backend, PROOF_TENANT, "solar", ref["artifact_id"])
    assignments = json.loads(content)
    assert assignments["rows"][0][assignments["columns"].index("centre_m")] == [6.0, 8.0]
    _, source_data = artifacts.read_artifact(backend, PROOF_TENANT, "solar", receipt["source"]["artifact_id"])
    assert source_data == data
    assert reopen(backend, 4)["graph"]["strings"][0]["route"] == route


def test_pvcase_rejects_source_substitution(tmp_path, monkeypatch):
    base = seed("m", "Ground")
    backend, _ = store_seed(tmp_path, monkeypatch, base)
    receipt = sources.import_pvcase_source(backend, PROOF_TENANT, "solar", canonical(ground_envelope()))
    store.ingest_drawing(backend, PROOF_TENANT, str(tmp_path / "seed.json"), drawing_id="other")
    foreign = sources.import_pvcase_source(backend, PROOF_TENANT, "other", canonical(ground_envelope()))
    original_keys = artifact_keys(backend)
    for index, (params, code) in enumerate((
        ({"expected_rev": 0, "source_artifact_id": "0" * 64}, "PVCASE_SOURCE_UNAVAILABLE"),
        ({"expected_rev": 0, "source_artifact_id": foreign["source"]["artifact_id"]}, "PVCASE_SOURCE_UNAVAILABLE"),
        ({"expected_rev": 0, "source_artifact_id": receipt["source"]["artifact_id"],
          "pvcase_source": ground_envelope()}, "INVALID_PVCASE_CONVERT_REQUEST"),
    )):
        refuses(code, lambda: commit(backend, "solar-pvcase-convert", params, 1, f"substitution-{index}"))
        assert versions(backend) == [1]
        assert artifact_keys(backend) == original_keys
        assert reopen(backend)["graph_sha256"] == results.digest(base)
    for key in original_keys:
        if key.endswith(".json"):
            assert json.loads(backend.get(key))["filename"] != "PVcaseAssignments.json"


def test_pvcase_export_reopen_and_historical_proof(tmp_path, monkeypatch):
    backend, receipt, _, _, _, _, _ = admitted_chain(tmp_path, monkeypatch)
    exported = read_output(backend, "solar-pvcase-export", job="export-job")
    second = second_source(backend)
    assert second["source"]["artifact_id"] != receipt["source"]["artifact_id"]
    later = commit(backend, "solar-settings", {
        "expected_rev": 2, "changes": {"panels_in_sequence": 3}}, 3, "settings-job")
    assert later["new_version"]["version"] == 4
    backend = store.FilesystemBackend(backend.root)
    params = {"drawing_id": "solar"}
    proof = read.graph_read_provenance(
        exported, params, PROOF_TENANT, "export-job", "solar-pvcase-export", 3, backend=backend)
    assert proof["source_version"] == 3 and proof["output_sha256"] == exported["output_sha256"]
    for key in ("output_sha256", "request_sha256"):
        with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
            read.graph_read_provenance(dict(exported, **{key: "0" * 64}), params, PROOF_TENANT,
                                       "export-job", "solar-pvcase-export", 3, backend=backend)
    assert versions(backend) == [1, 2, 3, 4]


def test_pvcase_refusals_are_atomic(tmp_path, monkeypatch):
    base = seed("m", "Ground")
    backend, _ = store_seed(tmp_path, monkeypatch, base)
    receipt = sources.import_pvcase_source(backend, PROOF_TENANT, "solar", canonical(ground_envelope()))
    snapshot = (versions(backend), reopen(backend)["graph_sha256"], artifact_keys(backend))
    operations = (
        ("STALE_GRAPH_REVISION", lambda: commit(backend, "solar-pvcase-convert", {
            "expected_rev": base["rev"] + 1, "source_artifact_id": receipt["source"]["artifact_id"]},
            1, "stale-job")),
        ("PVCASE_CONVERSION_REQUIRED", lambda: commit(
            backend, "solar-pvcase-solve", {"expected_rev": base["rev"]}, 1, "premature-solve-job")),
        ("PVCASE_CONVERSION_REQUIRED", lambda: read_output(backend, "solar-pvcase-export", 1)),
    )
    for code, operation in operations:
        refuses(code, operation)
        assert (versions(backend), reopen(backend)["graph_sha256"], artifact_keys(backend)) == snapshot
