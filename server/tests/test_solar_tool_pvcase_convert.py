"""Frozen wrapper and trusted adapter cases for G33; persisted producer admission is record 4b."""
import ast
import copy
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

import solar_artifacts as artifacts
import solar_design_graph as sdg
import solar_local_graph as local
import solar_local_read as read
import solar_pvcase_conversion as conversion
import solar_pvcase_graph as pvg
import solar_pvcase_outputs as outputs
import solar_pvcase_sources as sources
import solar_solve_results as results
import solar_tools
from test_solar_pvcase_conversion import seed, ground_envelope, canonical
from test_solar_pvcase_graph import membership_variant
from test_w1_design_graph import graph  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ("solar-pvcase-convert", "solar-pvcase-solve", "solar-pvcase-export")
GROUND_SOURCE_SHA = "37aac4a0747c5b780213a7d01edfa25fe9e3dabda057c110a4645f876cf95f64"
GROUND_SOURCE_ID = "0685eb4060d08b54ed79efa71b9ba3c326bdcf937cc8f2a11ba465453b68e705"
GROUND_CONVERT_SHA = "fba0fae6bdb4dc59b31d2534034d1db967a77aabd7a50b16a944de6da785d37b"
GROUND_SOLVE_SHA = "8417b77ae7eb0dd791f73482718a54bbe5ddfef3a577aba5ab04467ecc7a4682"
GROUND_OUTPUT_SHA = "398ace876c0efb2d136f56e269112b77ba17e806b1fb8c51dd9bb19cc8f5d1a8"

EXPECTED_DECLARATIONS = json.loads(r'''[{"schema":"leaf.solar-tool.v1","name":"solar-pvcase-convert","builtin":"builtins/solar_pvcase_convert.py","family":"imports","adapter":"local-graph-commit","entitlement":"run_write","requires_persisted_graph":true,"seedable":false,"invalid_request_code":"INVALID_PVCASE_CONVERT_REQUEST","readiness":{"kind":"hook"},"engine":"server-builtin","interaction":{"mode":"form"},"record_store":"registry","record":{"name":"solar-pvcase-convert","version":"1.0.0","description":"Convert an uploaded G33 capture into panel groups and panels in the shared Solar design","kind":"script","family_id":"imports","engine_op":"solar_pvcase_convert","entry":"builtins/solar_pvcase_convert.py","params":{"type":"object","properties":{"drawing_id":{"type":"string","maxLength":128},"expected_rev":{"type":"integer","minimum":0,"maximum":2147483647},"source_artifact_id":{"type":"string","minLength":64,"maxLength":64,"pattern":"^[0-9a-f]{64}$"}},"required":["expected_rev","source_artifact_id"],"additionalProperties":false},"returns":{"type":"object"},"capabilities":["drawing.write"],"allow_local_fallback":false},"ledger":[],"trusted_inputs":["pvcase_source"],"maturity":"preview","wave":4,"order":40,"scenario":"w4-pvcase"},{"schema":"leaf.solar-tool.v1","name":"solar-pvcase-solve","builtin":"builtins/solar_pvcase_solve.py","family":"stringing","adapter":"local-graph-commit","entitlement":"run_write","requires_persisted_graph":true,"seedable":false,"invalid_request_code":"INVALID_PVCASE_SOLVE_REQUEST","readiness":{"kind":"hook"},"engine":"server-builtin","interaction":{"mode":"form"},"record_store":"registry","record":{"name":"solar-pvcase-solve","version":"1.0.0","description":"Create unassigned strings from the current G33 design using its parity chunk length","kind":"script","family_id":"stringing","engine_op":"solar_pvcase_solve","entry":"builtins/solar_pvcase_solve.py","params":{"type":"object","properties":{"drawing_id":{"type":"string","maxLength":128},"expected_rev":{"type":"integer","minimum":0,"maximum":2147483647}},"required":["expected_rev"],"additionalProperties":false},"returns":{"type":"object"},"capabilities":["drawing.write"],"allow_local_fallback":false},"ledger":[],"trusted_inputs":["pvcase_source"],"maturity":"preview","wave":4,"order":50,"scenario":"w4-pvcase"},{"schema":"leaf.solar-tool.v1","name":"solar-pvcase-export","builtin":"builtins/solar_pvcase_export.py","family":"stringing","adapter":"local-graph-read","entitlement":"run_read","requires_persisted_graph":true,"seedable":false,"invalid_request_code":"INVALID_PVCASE_EXPORT_REQUEST","readiness":{"kind":"hook"},"engine":"server-builtin","interaction":{"mode":"form"},"record_store":"registry","record":{"name":"solar-pvcase-export","version":"1.0.0","description":"Export current G33 string assignments with source handles and graph references","kind":"script","family_id":"stringing","engine_op":"solar_pvcase_export","entry":"builtins/solar_pvcase_export.py","params":{"type":"object","properties":{"drawing_id":{"type":"string","maxLength":128}},"additionalProperties":false},"returns":{"type":"object"},"capabilities":["drawing.read"],"allow_local_fallback":false},"ledger":[],"trusted_inputs":["pvcase_source"],"maturity":"preview","wave":4,"order":60,"scenario":"w4-pvcase"}]''')
REFUSAL_COPY = json.loads(r'''{"invalid_pvcase_convert_request":"Check the G33 source and graph revision in the conversion request","invalid_pvcase_solve_request":"Check the graph revision in the G33 solve request","invalid_pvcase_export_request":"Check the G33 assignment export request","pvcase_empty_target_required":"Start with an empty Solar design before converting a G33 capture","pvcase_conversion_required":"Convert a G33 capture into this Solar design first","pvcase_solve_required":"Run the G33 parity solve before exporting assignments","pvcase_target_in_use":"This G33 step cannot use the existing string or equipment work","pvcase_source_required":"Select an uploaded G33 source for this operation","pvcase_source_unavailable":"The saved G33 source could not be read for this drawing","pvg_invalid_json":"Upload a G33 file containing valid JSON","pvg_input_bytes_exceeded":"Upload a smaller G33 file","pvg_envelope_fields":"Use a G33 file with the required schema and intake fields","pvg_envelope_schema":"Use a supported G33 file version","pvg_invalid_intake":"Check the groups and cells in the G33 capture","pvg_list_limit":"Reduce the number of items in the G33 capture","pvg_depth_limit":"Reduce the nesting in the G33 capture","pvg_node_limit":"Use a smaller G33 capture for this Solar design","pvg_byte_limit":"Use a smaller G33 design or assignment export","pvg_matrix_limit":"Reduce the rows or columns in the G33 groups","pvg_geometry_range":"Move the G33 geometry into the supported coordinate range","pvg_invalid_source":"The G33 source does not match this design","pvg_invalid_target":"Repair the Solar design before using the G33 tools","pvg_target_context":"Match the G33 units and installation to the Solar design","pvg_target_not_empty":"Use a Solar design without conflicting geometry or equipment","pvg_no_panel_groups":"Choose a G33 capture that contains panel groups","pvg_no_usable_panels":"Choose a G33 capture that contains usable panels","pvg_invalid_result":"The G33 operation could not produce a valid result","frame_membership_mismatch":"Restore the imported panel membership before using the G33 tools","matrix_cell_mismatch":"Restore the imported matrix positions before using the G33 tools","frame_sequence_mismatch":"Repair the frame string sequences before exporting assignments","matrix_input_mismatch":"Remove conflicting equipment assignments before exporting G33 assignments"}''')


def builtin(op):
    tool = "solar-pvcase-" + op
    return read._load_builtin(tool) if op == "export" else local._load_builtin(tool)


def refused(code, operation):
    with pytest.raises(sdg.GraphValidationError) as caught:
        operation()
    error = caught.value
    assert error.code == code and error.path == "<root>"
    assert str(error) == code + ": <root>" and error.__cause__ is None


def context(value, version=1):
    return {"graph": value, "graph_sha256": results.digest(value),
            "project_id": value["project"]["id"], "resolved_version": version,
            "representation": "intake"}


def admitted(monkeypatch, envelope=None, base=None):
    base = seed() if base is None else base
    envelope = ground_envelope() if envelope is None else envelope
    data = {}
    def put(key, value):
        if key in data:
            assert data[key] == value
        data[key] = value
    backend = SimpleNamespace(get=lambda key: data[key], put_if_absent_or_verify=put, data=data)
    with monkeypatch.context() as patch:
        patch.setattr(sources, "resolve_graph_context", lambda *a, **k: context(base))
        patch.setattr(sources.write_loop, "drawing_mutations_refusal", lambda: None)
        receipt = sources.import_pvcase_source(backend, "fixture-tenant", "solar", canonical(envelope))
    meta, content, witness = sources.load_pvcase_source(
        backend, "fixture-tenant", "solar", receipt["source"]["artifact_id"],
        project_id=base["project"]["id"])
    return backend, base, {"meta": meta, "content": content, "envelope": witness}


def chain(monkeypatch):
    backend, base, source = admitted(monkeypatch)
    converted = builtin("convert").run(
        base, {"expected_rev": base["rev"], "source_artifact_id": source["meta"]["artifact_id"]},
        pvcase_source=source)
    solved = builtin("solve").run(converted, {"expected_rev": converted["rev"]}, pvcase_source=source)
    return backend, base, source, converted, solved


def test_pvcase_constants_and_declarations():
    registry = solar_tools.load()
    assert tuple(registry.get(t) for t in TOOLS) == tuple(EXPECTED_DECLARATIONS)
    assert (len(registry.entries()), len(registry.local_graph_tools()),
            len(registry.local_graph_read_tools()), len(registry.registry_records())) == (53, 34, 18, 44)
    assert len(registry.manifest()) == 53
    assert sum(d["readiness"]["kind"] == "hook" for d in registry.entries()) == 16
    assert sum(d["scenario"] == "w1-rooftop" for d in registry.entries()) == 9
    assert solar_tools.TRUSTED_INPUTS == (
        "source_intake", "proposal_candidate", "solaredge_report", "physical_state", "pvcase_source")
    assert tuple(local._TRUSTED_RESOLVERS) == solar_tools.TRUSTED_INPUTS
    assert local._TRUSTED_RESOLVERS["pvcase_source"] is local._pvcase_source
    for op, declaration in zip(("convert", "solve", "export"), EXPECTED_DECLARATIONS):
        assert builtin(op).TOOL == declaration["name"]
        assert builtin(op).INVALID == declaration["invalid_request_code"]
        assert json.loads((ROOT / "server/solar_tools" / ("solar_pvcase_" + op + ".json")).read_text()) == declaration


def test_pvcase_registry_trusted_input_rules():
    server_dir = ROOT / "server"
    def validate(row):
        solar_tools._validate(row, row["name"].replace("-", "_"), server_dir, {"imports", "stringing"})
    for declaration in EXPECTED_DECLARATIONS:
        validate(copy.deepcopy(declaration))
        for updates in (
            {"trusted_inputs": ["pvcase_source", "source_intake"]},
            {"trusted_inputs": ["pvcase_source", "pvcase_source"]},
            {"adapter": "cloud-proposal"},
            {"adapter": "local-graph-read" if declaration["adapter"] == "local-graph-commit" else "local-graph-commit"},
            {"seedable": True}, {"requires_persisted_graph": False},
        ):
            changed = copy.deepcopy(declaration)
            changed.update(updates)
            with pytest.raises(solar_tools.SolarRegistryError):
                validate(changed)
    unrelated = solar_tools.get("solar-select-by-zone")
    unrelated["trusted_inputs"] = ["pvcase_source"]
    with pytest.raises(solar_tools.SolarRegistryError):
        solar_tools._validate(unrelated, "solar_select_by_zone", server_dir, {unrelated["family"]})
    unrelated["trusted_inputs"] = ["untrusted"]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(solar_tools, "TRUSTED_INPUTS", solar_tools.TRUSTED_INPUTS + ("untrusted",))
        with pytest.raises(solar_tools.SolarRegistryError, match="read adapter takes no trusted inputs"):
            solar_tools._validate(unrelated, "solar_select_by_zone", server_dir, {unrelated["family"]})


def test_pvcase_request_shapes(monkeypatch):
    calls = []
    monkeypatch.setattr(sources, "load_pvcase_source", lambda *a, **k: calls.append(1))
    for op in ("convert", "solve", "export"):
        tool = "solar-pvcase-" + op
        good = ({"expected_rev": 0, "source_artifact_id": "b" * 64} if op == "convert"
                else {"expected_rev": 0} if op == "solve" else {})
        schema = Draft202012Validator(solar_tools.get(tool)["record"]["params"])
        assert schema.is_valid(good)
        local.validate_pvcase_request(tool, good)
        local.validate_pvcase_request(tool, dict(good, drawing_id="solar"), adapter=True)
        for drawing in (None, True, [], "s" * 129):
            params = dict(good, drawing_id=drawing)
            assert not schema.is_valid(params)
            code = "INVALID_PVCASE_" + op.upper() + "_REQUEST"
            refused(code, lambda: local.validate_pvcase_request(tool, params, adapter=True))
            if op == "export":
                refused(code, lambda: read.run_local_graph_read(
                    None, "fixture-tenant", tool, params, drawing_id=drawing,
                    source_version=1, job_id="drawing-shape"))
            else:
                refused(code, lambda: local.run_local_graph_commit(
                    None, "fixture-tenant", tool, params, drawing_id=drawing,
                    source_version=1, holder="holder", fence=1, job_id="drawing-shape"))
        invalid = [None, [], dict(good, pvcase_source={}), dict(good, envelope={}),
                   dict(good, source_bytes="private"), dict(good, initialize={}), dict(good, cancel=True)]
        if op != "export":
            invalid += [dict(good, expected_rev=x) for x in (True, False, -1, 2147483648, 0.0, "0", None)]
            invalid.append({k: v for k, v in good.items() if k != "expected_rev"})
            for revision in (0, 2147483647):
                local.validate_pvcase_request(tool, dict(good, expected_rev=revision))
        if op == "convert":
            invalid += [dict(good, source_artifact_id=x) for x in ("B" * 64, "b" * 63, "g" * 64, None, True)]
        elif op == "solve":
            invalid.append(dict(good, source_artifact_id="b" * 64))
        else:
            invalid.append({"expected_rev": 0})
        for params in invalid:
            code = "INVALID_PVCASE_" + op.upper() + "_REQUEST"
            refused(code, lambda: builtin(op).run(seed(), params))
            refused(code, lambda: local._resolve_trusted(
                tool, None, "fixture-tenant", "solar", 1, results.digest(seed()), None, params,
                project_id=seed()["project"]["id"]))
            if op == "export":
                refused(code, lambda: read.run_local_graph_read(
                    None, "fixture-tenant", tool, params, drawing_id="solar", source_version=1, job_id="shape"))
            else:
                refused(code, lambda: local.run_local_graph_commit(
                    None, "fixture-tenant", tool, params, drawing_id="solar", source_version=1,
                    holder="holder", fence=1, job_id="shape"))
        cycle = {}
        cycle["self"] = cycle
        refused("INVALID_PVCASE_" + op.upper() + "_REQUEST", lambda: builtin(op).run(seed(), cycle))
    assert calls == []


def test_pvcase_readiness(monkeypatch, graph):
    _, base, _, converted, solved = chain(monkeypatch)
    expected = {
        "convert": ("pvcase_empty_target_required", None, "pvcase_empty_target_required", "pvcase_empty_target_required"),
        "solve": ("pvcase_conversion_required", "pvcase_conversion_required", None, "pvcase_target_in_use"),
        "export": ("pvcase_conversion_required", "pvcase_conversion_required", "pvcase_solve_required", None),
    }
    for op in expected:
        for value, reason in zip((graph, base, converted, solved), expected[op]):
            assert builtin(op).input_readiness(value) == {"input_ready": reason is None, "input_reason": reason}
    for marker in (None, [], "unknown", {}, {"schema": conversion.INPUT_SCHEMA},
                   {"schema": "wrong", "conversion_schema": conversion.CONVERSION_SCHEMA}):
        changed = copy.deepcopy(base)
        changed["extra"]["pvcase"] = marker
        assert builtin("convert").input_readiness(changed)["input_reason"] == "pvcase_empty_target_required"
        for op in ("solve", "export"):
            assert builtin(op).input_readiness(changed)["input_reason"] == "pvcase_conversion_required"
    for extra in (None, [], "future"):
        changed = copy.deepcopy(base)
        changed["extra"] = extra
        assert builtin("convert").input_readiness(changed)["input_ready"] is True
        assert builtin("solve").input_readiness(changed)["input_reason"] == "pvcase_conversion_required"
        assert builtin("export").input_readiness(changed)["input_reason"] == "pvcase_conversion_required"
    for key in ("strings", "inverters", "routes", "schedules"):
        changed = copy.deepcopy(converted)
        changed[key] = [{}]
        assert builtin("solve").input_readiness(changed)["input_reason"] == "pvcase_target_in_use"
        if key != "strings":
            changed = copy.deepcopy(solved)
            changed[key] = [{}]
            assert builtin("export").input_readiness(changed)["input_reason"] == "pvcase_target_in_use"
    for marker in (None, [], {}, {"schema": "wrong"}):
        changed = copy.deepcopy(solved)
        changed["extra"]["pvcase_solve"] = marker
        changed["inverters"] = [{}]
        assert builtin("export").input_readiness(changed)["input_reason"] == "pvcase_solve_required"
    changed = copy.deepcopy(solved)
    changed["strings"] = []
    assert builtin("export").input_readiness(changed)["input_reason"] == "pvcase_solve_required"


def test_pvcase_trusted_input_required(monkeypatch):
    _, base, _, converted, solved = chain(monkeypatch)
    for op, value, params in (
        ("convert", base, {"expected_rev": 0, "source_artifact_id": GROUND_SOURCE_ID}),
        ("solve", converted, {"expected_rev": 1}), ("export", solved, {}),
    ):
        refused("PVCASE_SOURCE_REQUIRED", lambda: builtin(op).run(value, params))
        for key in ("pvcase_source", "envelope", "content"):
            refused("INVALID_PVCASE_" + op.upper() + "_REQUEST",
                    lambda: builtin(op).run(value, dict(params, **{key: ground_envelope()})))


def test_pvcase_source_resolution(monkeypatch):
    backend, base, source, converted, solved = chain(monkeypatch)
    assert len(source["content"]) == 501
    assert source["meta"]["artifact_id"] == GROUND_SOURCE_ID
    assert hashlib.sha256(source["content"]).hexdigest() == GROUND_SOURCE_SHA
    assert source["envelope"] == sources.inspect_pvcase_source(source["content"])
    calls = []
    real_load = sources.load_pvcase_source
    def spy(*args, **kwargs):
        calls.append((args[1:], kwargs))
        return real_load(*args, **kwargs)
    monkeypatch.setattr(sources, "load_pvcase_source", spy)
    for tool, value, version, params in (
        (TOOLS[0], base, 1, {"expected_rev": 0, "source_artifact_id": GROUND_SOURCE_ID}),
        (TOOLS[1], converted, 2, {"expected_rev": 1}),
        (TOOLS[2], solved, 3, {}),
    ):
        monkeypatch.setattr(local, "resolve_graph_context", lambda *a, **k: context(value, version))
        resolved = local._resolve_trusted(tool, backend, "fixture-tenant", "solar", version,
                                          results.digest(value), None, params,
                                          project_id=value["project"]["id"])
        assert resolved == {"pvcase_source": source}
        assert calls[-1] == (("fixture-tenant", "solar", GROUND_SOURCE_ID),
                             {"project_id": value["project"]["id"]})
    params = {"expected_rev": 0, "source_artifact_id": GROUND_SOURCE_ID}
    monkeypatch.setattr(local, "resolve_graph_context", lambda *a, **k: context(base))
    for tenant, drawing, artifact_id in (
        ("other-tenant", "solar", GROUND_SOURCE_ID),
        ("fixture-tenant", "other-drawing", GROUND_SOURCE_ID),
        ("fixture-tenant", "solar", "c" * 64),
    ):
        refused("PVCASE_SOURCE_UNAVAILABLE", lambda: local._pvcase_source(
            backend, tenant, drawing, 1, results.digest(base), dict(params, source_artifact_id=artifact_id),
            tool=TOOLS[0], project_id=base["project"]["id"]))
    foreign = copy.deepcopy(base)
    foreign["project"]["id"] = "leaf:project:foreign"
    monkeypatch.setattr(local, "resolve_graph_context", lambda *a, **k: context(foreign))
    refused("PVCASE_SOURCE_UNAVAILABLE", lambda: local._pvcase_source(
        backend, "fixture-tenant", "solar", 1, results.digest(foreign), params,
        tool=TOOLS[0], project_id=foreign["project"]["id"]))
    # Wrong-kind artifacts are genuine ArtifactSink products, not source-loader stubs.
    monkeypatch.setattr(sources.write_loop, "drawing_mutations_refusal", lambda: None)
    sink = artifacts.ArtifactSink(backend, "fixture-tenant", "solar", context(base),
                                  "solar-string-data", "d" * 64, False)
    prepared = sink.prepare(artifacts.ArtifactOutput({}, "application/json", "other.json", b"{}"))
    reference = sink.finish(prepared)
    monkeypatch.setattr(local, "resolve_graph_context", lambda *a, **k: context(base))
    refused("PVCASE_SOURCE_UNAVAILABLE", lambda: local._pvcase_source(
        backend, "fixture-tenant", "solar", 1, results.digest(base),
        dict(params, source_artifact_id=reference["artifact_id"]), tool=TOOLS[0]))
    refused("PVG_INVALID_SOURCE", lambda: local._pvcase_source(
        backend, "fixture-tenant", "solar", 1, "e" * 64, params, tool=TOOLS[0]))
    refused("PVCASE_CONVERSION_REQUIRED", lambda: local._pvcase_source(
        backend, "fixture-tenant", "solar", 1, results.digest(base), {"expected_rev": 0}, tool=TOOLS[1]))


def test_pvcase_source_binding(monkeypatch):
    backend, base, source, converted, solved = chain(monkeypatch)
    original_store = copy.deepcopy(backend.data)
    variants = []
    for key in ("content_sha256", "request_sha256", "artifact_id", "project_id"):
        changed = copy.deepcopy(source)
        changed["meta"][key] = "c" * 64
        variants.append(changed)
    changed = copy.deepcopy(source)
    changed["envelope"]["intake"]["panel_groups"][0]["rows"][0][0]["code"] = True
    variants.append(changed)
    variants += [{}, {**source, "extra": True}, {**source, "content": source["content"] + b" "},
                 {**source, "envelope": ground_envelope() | {"schema": "wrong"}}]
    for bad in variants:
        for op, value, params in (
            ("convert", base, {"expected_rev": 0, "source_artifact_id": GROUND_SOURCE_ID}),
            ("solve", converted, {"expected_rev": 1}), ("export", solved, {}),
        ):
            snapshot = copy.deepcopy((value, bad))
            refused("PVG_INVALID_SOURCE", lambda: builtin(op).run(value, params, pvcase_source=bad))
            assert (value, bad) == snapshot
    for key in ("source_artifact_id", "source_sha256"):
        for op, value, params in (("solve", converted, {"expected_rev": 1}), ("export", solved, {})):
            changed = copy.deepcopy(value)
            changed["extra"]["pvcase"][key] = "c" * 64
            refused("PVG_INVALID_SOURCE", lambda: builtin(op).run(changed, params, pvcase_source=source))
        changed = copy.deepcopy(converted)
        changed["frames"][0]["provenance"]["pvcase"][key] = "c" * 64
        refused("PVG_INVALID_SOURCE", lambda: local.check_pvcase_result_source(changed, source))
        changed = copy.deepcopy(converted)
        changed["extra"]["pvcase"][key] = "c" * 64
        refused("PVG_INVALID_SOURCE", lambda: local.check_pvcase_result_source(changed, source))
    # The commit rail must fence a tampered builtin result before publication.
    monkeypatch.setattr(local, "resolve_graph_context", lambda *a, **k: context(base))
    tampered = copy.deepcopy(converted)
    tampered["frames"][0]["provenance"]["pvcase"].pop("source_sha256")
    monkeypatch.setattr(local, "_load_builtin", lambda tool: SimpleNamespace(run=lambda *a, **k: tampered))
    def forbidden(*args, **kwargs):
        raise AssertionError("source refusal must not publish")
    monkeypatch.setattr(local, "publish_version", forbidden)
    refused("PVG_INVALID_SOURCE", lambda: local.run_local_graph_commit(
        backend, "fixture-tenant", TOOLS[0], {"expected_rev": 0, "source_artifact_id": GROUND_SOURCE_ID},
        drawing_id="solar", source_version=1, holder="holder", fence=1, job_id="binding"))
    assert backend.data == original_store


def test_pvcase_convert_kernel_and_revision(monkeypatch):
    _, base, source = admitted(monkeypatch)
    params = {"expected_rev": 0, "source_artifact_id": GROUND_SOURCE_ID}
    module = builtin("convert")
    original, calls = module.finish_mutation, []
    def finish(before, after, tool):
        calls.append(tool)
        return original(before, after, tool)
    monkeypatch.setattr(module, "finish_mutation", finish)
    result = module.run(base, params, pvcase_source=source)
    assert calls == [TOOLS[0]]
    expected = results.finish_mutation(base, conversion.convert(
        base, source["envelope"], source_artifact_id=GROUND_SOURCE_ID, source_sha256=GROUND_SOURCE_SHA), TOOLS[0])
    assert result == expected and results.digest(result) == GROUND_CONVERT_SHA
    assert results.digest(base) == "d98fb1771f9de7740e9b09952cf7d0aab90c3aad33b9d7dfdb1bdb4aa45d8fe1"
    assert (result["rev"], result["parent_rev"]) == (1, 0)
    assert tuple(len(result[k]) for k in ("frames", "panels", "strings", "inverters")) == (1, 2, 0, 0)
    frame = result["frames"][0]
    assert (frame["module_rows"], frame["module_columns"], frame["module_slots"]) == (3, 3, 9)
    assert frame["provenance"]["pvcase"]["source_row_lengths"] == [3, 1, 0]
    assert "tracker" not in frame and "ground_slots" not in frame
    assert result["settings"] == base["settings"]


def test_pvcase_convert_refusal_atomicity(monkeypatch):
    _, base, source, converted, _ = chain(monkeypatch)
    params = {"expected_rev": 0, "source_artifact_id": GROUND_SOURCE_ID}
    snapshot = copy.deepcopy((base, source))
    refused("PVCASE_EMPTY_TARGET_REQUIRED", lambda: builtin("convert").run(
        converted, dict(params, expected_rev=1), pvcase_source=source))
    refused("STALE_GRAPH_REVISION", lambda: builtin("convert").run(
        base, dict(params, expected_rev=1), pvcase_source=source))
    unknown = copy.deepcopy(base)
    unknown["project"]["units"]["meters_per_unit"] = 0.3048
    refused("UNRESOLVED_UNITS", lambda: builtin("convert").run(unknown, params, pvcase_source=source))
    refused("PVG_INVALID_TARGET", lambda: builtin("convert").run({}, params, pvcase_source=source))
    for mutate in (
        lambda g: g["project"].update(installation_design="Roof"),
        lambda g: g["project"]["units"].update(crs="EPSG:4326"),
    ):
        changed = copy.deepcopy(base)
        mutate(changed)
        before = copy.deepcopy(changed)
        refused("PVG_TARGET_CONTEXT", lambda: builtin("convert").run(changed, params, pvcase_source=source))
        assert changed == before
    empty = ground_envelope()
    empty["intake"]["panel_groups"][0]["rows"] = [[None]]
    _, value, witness = admitted(monkeypatch, empty)
    refused("PVG_NO_USABLE_PANELS", lambda: builtin("convert").run(
        value, {"expected_rev": 0, "source_artifact_id": witness["meta"]["artifact_id"]}, pvcase_source=witness))
    with monkeypatch.context() as patch:
        patch.setattr(conversion, "MAX_MATRIX_SLOTS", 8)
        refused("PVG_MATRIX_LIMIT", lambda: builtin("convert").run(base, params, pvcase_source=source))
    with monkeypatch.context() as patch:
        def finishing_failure(*args):
            raise sdg.GraphValidationError("PRIVATE_DATA", "payload")
        patch.setattr(builtin("convert"), "finish_mutation", finishing_failure)
        refused("PVG_INVALID_RESULT", lambda: builtin("convert").run(base, params, pvcase_source=source))
    assert (base, source) == snapshot


def test_pvcase_solve_kernel_and_revision(monkeypatch):
    _, _, source, converted, solved = chain(monkeypatch)
    module = builtin("solve")
    original, calls = module.finish_mutation, []
    def finish(before, after, tool):
        calls.append(tool)
        return original(before, after, tool)
    monkeypatch.setattr(module, "finish_mutation", finish)
    value = module.run(converted, {"expected_rev": 1}, pvcase_source=source)
    kernel = pvg.solve_graph(converted, source["envelope"])
    assert tuple(kernel["diagnostics"][k] for k in (
        "panels_assigned", "strings_created", "written", "l2_count")) == (2, 1, 2, 0)
    assert calls == [TOOLS[1]]
    assert value == solved == results.finish_mutation(converted, kernel["graph"], TOOLS[1])
    assert results.digest(value) == GROUND_SOLVE_SHA
    assert (value["rev"], value["parent_rev"]) == (2, 1)
    assert [s["module_count"] for s in value["strings"]] == [2]
    assert value["strings"][0]["id"] == "leaf:string:5fb0e6dd-fa5e-4c7a-9ec7-758c1743e768"
    assert value["strings"][0]["inverter_ref"] is None and not value["inverters"]
    assert value["extra"]["pvcase_solve"]["panels_per_string"] == 12
    assert value["settings"] == converted["settings"]
    assert all(a["inverter_id"] is None and a["string_input_number"] is None
               for a in value["frames"][0]["panel_assignments"])


def test_pvcase_solve_current_geometry(monkeypatch):
    _, _, source, converted, _ = chain(monkeypatch)
    changed = copy.deepcopy(converted)
    changed["panels"][0]["centre"] = [6.0, 8.0]
    old_matrix = copy.deepcopy(changed["frames"][0]["matrix"])
    assert old_matrix[0][0]["x"] != 6.0
    original, calls = pvg.raw.solve, []
    def spy(groups, equipment, length):
        calls.append((groups[0]["panels"][0].x, groups[0]["panels"][0].y, equipment, length))
        return original(groups, equipment, length)
    monkeypatch.setattr(pvg.raw, "solve", spy)
    before = copy.deepcopy((changed, source))
    solved = builtin("solve").run(changed, {"expected_rev": 1}, pvcase_source=source)
    assert calls == [(6.0, 8.0, [], 12)]
    assert solved["strings"][0]["route"][0] == [6.0, 8.0]
    assert (changed, source) == before
    assert changed["frames"][0]["matrix"] == old_matrix


def test_pvcase_solve_membership_refusals(monkeypatch):
    _, _, source, converted, _ = chain(monkeypatch)
    for kind, code in (("remove", "FRAME_MEMBERSHIP_MISMATCH"),
                       ("add", "FRAME_MEMBERSHIP_MISMATCH"), ("reorder", "MATRIX_CELL_MISMATCH")):
        changed = membership_variant(converted, kind)
        before = copy.deepcopy((changed, source))
        refused(code, lambda: builtin("solve").run(changed, {"expected_rev": 1}, pvcase_source=source))
        assert (changed, source) == before


def test_pvcase_solve_refuses_existing_engineering(monkeypatch, graph):
    _, _, source, converted, solved = chain(monkeypatch)
    refused("PVCASE_TARGET_IN_USE", lambda: builtin("solve").run(
        solved, {"expected_rev": 2}, pvcase_source=source))
    for key in ("inverters", "routes", "schedules"):
        changed = copy.deepcopy(converted)
        item = copy.deepcopy(graph[key][0])
        if key == "inverters":
            item["input_assignments"] = []
        elif key == "routes":
            item.update(from_ref=None, to_ref=None)
        else:
            item["source_refs"] = []
        changed[key].append(item)
        assert sdg.validate_graph(changed) == changed
        before = copy.deepcopy(changed)
        refused("PVCASE_TARGET_IN_USE", lambda: builtin("solve").run(
            changed, {"expected_rev": 1}, pvcase_source=source))
        assert changed == before
    refused("PVCASE_CONVERSION_REQUIRED", lambda: builtin("solve").run(seed(), {"expected_rev": 0}))
    with monkeypatch.context() as patch:
        def finishing_failure(*args):
            raise sdg.GraphValidationError("PRIVATE_DATA")
        patch.setattr(builtin("solve"), "finish_mutation", finishing_failure)
        refused("PVG_INVALID_RESULT", lambda: builtin("solve").run(
            converted, {"expected_rev": 1}, pvcase_source=source))


def test_pvcase_export_kernel_and_artifact(monkeypatch):
    _, _, source, _, solved = chain(monkeypatch)
    before = copy.deepcopy((solved, source))
    output = builtin("export").run(solved, {}, pvcase_source=source)
    assert type(output) is artifacts.ArtifactOutput
    assert output.summary == {
        "schema": "leaf.pvcase-g33-export.v1", "panels": 2, "strings": 1,
        "electrical_sizing": "not-evaluated",
        "source": {"artifact_id": GROUND_SOURCE_ID, "content_sha256": GROUND_SOURCE_SHA}}
    assert (output.media_type, output.filename) == ("application/json", "PVcaseAssignments.json")
    assert output.content == outputs.assignment_export(solved, source["envelope"])
    assert len(output.content) == 624 and hashlib.sha256(output.content).hexdigest() == GROUND_OUTPUT_SHA
    assert json.loads(output.content)["rows"][0][:3] == ["001", 1, 1]
    assert (solved, source) == before
    string_data = read._load_builtin("solar-string-data").run(solved, {})
    assert len(string_data.content) == 562
    assert hashlib.sha256(string_data.content).hexdigest() == "3d262fdc17814fdc77747f4318d6d01f3ae1f3186789f5d5a374280b2880345f"


def test_pvcase_export_requires_solve(monkeypatch):
    _, _, source, converted, solved = chain(monkeypatch)
    refused("PVCASE_SOLVE_REQUIRED", lambda: builtin("export").run(converted, {}, pvcase_source=source))
    for mutate in (
        lambda g: g["strings"][0]["extra"].pop("pvcase"),
        lambda g: g["strings"][0]["extra"]["pvcase"].update(parity_l2_number=2),
        lambda g: g["extra"]["pvcase_solve"].update(written=1),
    ):
        changed = copy.deepcopy(solved)
        mutate(changed)
        before = copy.deepcopy(changed)
        refused("PVG_INVALID_RESULT", lambda: builtin("export").run(changed, {}, pvcase_source=source))
        assert changed == before
    # Redundant assignment drift retains the output kernel's refusal at the wrapper boundary.
    changed = copy.deepcopy(solved)
    changed["frames"][0]["panel_assignments"][0]["string_input_number"] = 1
    changed["frames"][0]["matrix"][0][0]["string_input_number"] = 1
    before = copy.deepcopy(changed)
    refused("MATRIX_INPUT_MISMATCH", lambda: builtin("export").run(changed, {}, pvcase_source=source))
    assert changed == before


def test_pvcase_export_never_solves(monkeypatch):
    _, _, source, _, solved = chain(monkeypatch)
    def forbidden(*args, **kwargs):
        raise AssertionError("export must not solve or convert")
    monkeypatch.setattr(pvg.raw, "solve", forbidden)
    monkeypatch.setattr(pvg, "solve_graph", forbidden)
    monkeypatch.setattr(conversion, "convert", forbidden)
    assert hashlib.sha256(builtin("export").run(solved, {}, pvcase_source=source).content).hexdigest() == GROUND_OUTPUT_SHA
    changed = copy.deepcopy(solved)
    changed["panels"][0]["centre"] = [6.0, 8.0]
    route = copy.deepcopy(changed["strings"][0]["route"])
    assert json.loads(builtin("export").run(changed, {}, pvcase_source=source).content)["rows"][0][9] == [6.0, 8.0]
    assert changed["strings"][0]["route"] == route


def test_pvcase_isolation_and_determinism(monkeypatch):
    _, base, source = admitted(monkeypatch)
    base["future"] = {"keep": [1, 2]}
    original = copy.deepcopy((base, source))
    params = {"expected_rev": 0, "source_artifact_id": GROUND_SOURCE_ID}
    first = builtin("convert").run(base, params, pvcase_source=source)
    second = builtin("convert").run(base, params, pvcase_source=source)
    assert first == second and first["future"] == base["future"]
    converted_before = copy.deepcopy(first)
    a = builtin("solve").run(first, {"expected_rev": 1}, pvcase_source=source)
    b = builtin("solve").run(first, {"expected_rev": 1}, pvcase_source=source)
    assert a == b and first == converted_before
    solved_before = copy.deepcopy(a)
    x = builtin("export").run(a, {}, pvcase_source=source)
    y = builtin("export").run(a, {}, pvcase_source=source)
    assert x == y and a == solved_before
    first["future"]["keep"].append(3)
    a["strings"][0]["route"][0][0] = 999
    x.summary["source"]["artifact_id"] = "c" * 64
    assert second == converted_before and b == solved_before
    assert (base, source) == original and y.summary["source"]["artifact_id"] == GROUND_SOURCE_ID


def test_pvcase_refusal_copy_contract(monkeypatch):
    assert len(REFUSAL_COPY) == 31
    text = (ROOT / "web/src/lib/ribbonClusters.js").read_text(encoding="utf-8")
    for key, sentence in REFUSAL_COPY.items():
        assert key + ": '" + sentence + "'," in text
    kernel_codes = {node.value for node in ast.walk(ast.parse(inspect.getsource(conversion)))
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node.value.startswith("PVG_")}
    kernel_codes.update(outputs.CODES)
    assert len(kernel_codes) == 22
    assert {code.lower() for code in kernel_codes} <= REFUSAL_COPY.keys()
    reasons = set()
    for op in ("convert", "solve", "export"):
        tree = ast.parse(inspect.getsource(builtin(op).input_readiness))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "input_reason" and isinstance(value, ast.Constant):
                        if value.value is not None:
                            reasons.add(value.value)
    assert reasons == {"pvcase_empty_target_required", "pvcase_conversion_required",
                       "pvcase_solve_required", "pvcase_target_in_use"}
    assert reasons <= REFUSAL_COPY.keys()
    _, base, source = admitted(monkeypatch)
    def broken(*args, **kwargs):
        raise sources.PvcaseSourceError("PVS_SOURCE_CORRUPT")
    monkeypatch.setattr(sources, "load_pvcase_source", broken)
    monkeypatch.setattr(local, "resolve_graph_context", lambda *a, **k: context(base))
    refused("PVCASE_SOURCE_UNAVAILABLE", lambda: local._pvcase_source(
        None, "fixture-tenant", "solar", 1, results.digest(base),
        {"expected_rev": 0, "source_artifact_id": GROUND_SOURCE_ID}, tool=TOOLS[0]))
    source["content"] = b"PRIVATE_SOURCE_PAYLOAD"
    refused("PVG_INVALID_SOURCE", lambda: builtin("convert").run(
        base, {"expected_rev": 0, "source_artifact_id": GROUND_SOURCE_ID}, pvcase_source=source))


# ---------------------------------------------------------------- the commit proof binds stored content
#
# Publication replaces the design graph and keeps every other key of the parent's intake, and it never
# changes the project. So a stored version whose other content differs from its parent's, or a receipt that
# names another project, was not produced by the request it claims. Each forgery below recomputes every
# hash the proof reads (the version bytes, the manifest entry and the receipt's intake digest), which is why
# the shared proof compares content, not hashes. The rows run on solar-settings (a tool the proof does not
# re-derive) and on the G33 convert and solve.

import store  # noqa: E402
from test_w1_local_graph_adapter import held  # noqa: E402
from test_w1_solve_commit import seed as store_seed  # noqa: E402

PROOF_TENANT = "fixture-tenant"
REJECTED = "^graph commit terminal proof rejected$"
OTHER_PROJECT = "leaf:project:00000000-0000-4000-8000-000000000999"


def commit(backend, tool, params, version, job):
    with held(backend) as fence:
        return local.run_local_graph_commit(
            backend, PROOF_TENANT, tool, dict(params, drawing_id="solar"), drawing_id="solar",
            source_version=version, holder="fixture-owner", fence=fence, job_id=job)


def prove(backend, result, tool, params, version, job):
    return local.graph_commit_provenance(result, dict(params, drawing_id="solar"), PROOF_TENANT, job,
                                         tool, version, backend=backend)


def forge_version(backend, version, mutate, *, serialize=None):
    """Rewrite one stored version's intake and recompute its manifest digest and length. The bytes are written
    the way the publisher writes them (canonical), unless the row is about serialization."""
    _, key, entry = store.resolve_version_entry(backend, PROOF_TENANT, "solar", version)
    old = backend.get(key)
    intake = json.loads(old)
    mutate(intake)
    new = (serialize or results.canonical_bytes)(intake)
    backend.put(key, new)
    manifest = store.load_manifest(backend, PROOF_TENANT, "solar")
    rows = [row for row in manifest["versions"] if int(row["v"]) == version]
    assert len(rows) == 1
    for field, value in list(rows[0].items()):
        if value == entry["sha256"]:
            rows[0][field] = hashlib.sha256(new).hexdigest()
        elif value == len(old):
            rows[0][field] = len(new)
    store.save_manifest(backend, PROOF_TENANT, "solar", manifest)
    assert store.resolve_version_entry(backend, PROOF_TENANT, "solar", version)[2]["sha256"] == hashlib.sha256(new).hexdigest()
    return hashlib.sha256(new).hexdigest()


def forge_stored(backend, result, mutate, *, serialize=None):
    """Rewrite the published version's stored intake and recompute every hash the proof reads."""
    sha = forge_version(backend, result["new_version"]["version"], mutate, serialize=serialize)
    return dict(result, intake_sha256=sha)


def settings_commit(tmp_path, monkeypatch, graph):
    backend, _ = store_seed(tmp_path, monkeypatch, graph)
    params = {"expected_rev": graph["rev"], "changes": {"panels_in_sequence": 3}}
    return backend, params, commit(backend, "solar-settings", params, 1, "settings-job")


def pvcase_chain(tmp_path, monkeypatch):
    base = seed()
    backend, _ = store_seed(tmp_path, monkeypatch, base)
    receipt = sources.import_pvcase_source(backend, PROOF_TENANT, "solar", canonical(ground_envelope()))
    convert = {"expected_rev": base["rev"], "source_artifact_id": receipt["source"]["artifact_id"]}
    converted = commit(backend, "solar-pvcase-convert", convert, 1, "convert-job")
    solve = {"expected_rev": converted["after_rev"]}
    solved = commit(backend, "solar-pvcase-solve", solve, 2, "solve-job")
    return backend, [("solar-pvcase-convert", convert, 1, "convert-job", converted),
                     ("solar-pvcase-solve", solve, 2, "solve-job", solved)]


def test_pvcase_proof_content_genuine_publication_keeps_parent_content(tmp_path, monkeypatch, graph):
    backend, params, result = settings_commit(tmp_path, monkeypatch, graph)
    proof = prove(backend, result, "solar-settings", params, 1, "settings-job")
    assert proof["new_version"] == 2 and proof["source_version"] == 1
    child = local._source_intake(backend, PROOF_TENANT, "solar", 2, result["graph_sha256"])
    parent = local._source_intake(backend, PROOF_TENANT, "solar", 1, result["before_graph_sha256"])
    assert child == parent and "solar_design_graph" not in child
    assert result["project_id"] == graph["project"]["id"]


@pytest.mark.parametrize("mutate", [
    lambda intake: intake.__setitem__("layers", ["FORGED_LAYER"]),
    lambda intake: intake.__setitem__("forged_key", {"value": 1}),
    lambda intake: intake.pop("blockdefs"),
], ids=["changed-key", "added-key", "removed-key"])
def test_pvcase_proof_content_settings_refuses_stored_content_forgery(tmp_path, monkeypatch, graph, mutate):
    backend, params, result = settings_commit(tmp_path, monkeypatch, graph)
    forged = forge_stored(backend, result, mutate)
    with pytest.raises(ValueError, match=REJECTED):
        prove(backend, forged, "solar-settings", params, 1, "settings-job")


def test_pvcase_proof_content_settings_refuses_foreign_project_receipt(tmp_path, monkeypatch, graph):
    backend, params, result = settings_commit(tmp_path, monkeypatch, graph)
    with pytest.raises(ValueError, match=REJECTED):
        prove(backend, dict(result, project_id=OTHER_PROJECT), "solar-settings", params, 1,
              "settings-job")
    assert prove(backend, result, "solar-settings", params, 1, "settings-job")["new_version"] == 2


def test_pvcase_proof_content_pvcase_convert_and_solve_bind_stored_content(tmp_path, monkeypatch):
    backend, steps = pvcase_chain(tmp_path, monkeypatch)
    for tool, params, version, job, result in steps:
        assert prove(backend, result, tool, params, version, job)["new_version"] == version + 1
    for tool, params, version, job, result in reversed(steps):
        with pytest.raises(ValueError, match=REJECTED):
            prove(backend, dict(result, project_id=OTHER_PROJECT), tool, params, version, job)
        forged = forge_stored(backend, result, lambda intake: intake.__setitem__("layers", ["FORGED_LAYER"]))
        with pytest.raises(ValueError, match=REJECTED):
            prove(backend, forged, tool, params, version, job)


def test_pvcase_proof_content_settings_refuses_foreign_project_graph(tmp_path, monkeypatch, graph):
    # A stored graph rewritten to another project, with every digest and the receipt recomputed to match,
    # is refused because publication never changes the project (no re-derivation runs for this tool).
    backend, params, result = settings_commit(tmp_path, monkeypatch, graph)

    def rehome(intake):
        intake["solar_design_graph"]["project"]["id"] = OTHER_PROJECT
        intake["solar_design_graph_sha256"] = results.digest(intake["solar_design_graph"])

    forged = forge_stored(backend, result, rehome)
    stored = local.resolve_graph_context(backend, PROOF_TENANT, "solar", 2)
    forged = dict(forged, project_id=OTHER_PROJECT, graph_sha256=stored["graph_sha256"])
    with pytest.raises(ValueError, match=REJECTED):
        prove(backend, forged, "solar-settings", params, 1, "settings-job")


# Correction 2: the proof rebuilds the publisher's exact bytes, canonical_bytes(version_companion(parent intake,
# before, after)), and compares their digest with the stored version's (the publisher's own replay check).

@pytest.mark.parametrize("value, refused", [(2 ** 64, True), (7, False)], ids=["outside-bounds", "within-bounds"])
def test_pvcase_proof_content_settings_refuses_value_the_publisher_refuses(tmp_path, monkeypatch, graph, value,
                                                                           refused):
    # The same extra key written into the parent and the published version, every digest recomputed. The publisher
    # refuses a number outside its bounds (version_companion raises NUMBER_LIMIT_EXCEEDED), so the proof refuses the
    # stored version too; a value inside the bounds is the declared limit: the proof binds the version to the stored
    # parent, not to the parent's original content.
    backend, params, result = settings_commit(tmp_path, monkeypatch, graph)
    add = lambda intake: intake.__setitem__("not_written_by_publisher", value)  # noqa: E731
    forge_version(backend, 1, add)
    forged = forge_stored(backend, result, add)
    if refused:
        with pytest.raises(ValueError, match=REJECTED):
            prove(backend, forged, "solar-settings", params, 1, "settings-job")
    else:
        assert prove(backend, forged, "solar-settings", params, 1, "settings-job")["new_version"] == 2


@pytest.mark.parametrize("retyped", [1.0, True], ids=["float", "bool"])
def test_pvcase_proof_content_settings_refuses_retyped_preserved_value(tmp_path, monkeypatch, graph, retyped):
    # The parent and the published version both carry the integer 1; the published copy is then retyped to a value
    # Python calls equal (1.0, True). Publication preserves the parent's JSON exactly, so the retyped copy is refused.
    backend, params, result = settings_commit(tmp_path, monkeypatch, graph)
    keep = lambda intake: intake.__setitem__("preserved", 1)  # noqa: E731
    forge_version(backend, 1, keep)
    kept = forge_stored(backend, result, keep)
    assert prove(backend, kept, "solar-settings", params, 1, "settings-job")["new_version"] == 2
    forged = forge_stored(backend, result, lambda intake: intake.__setitem__("preserved", retyped))
    with pytest.raises(ValueError, match=REJECTED):
        prove(backend, forged, "solar-settings", params, 1, "settings-job")


def test_pvcase_proof_content_settings_refuses_noncanonical_serialization(tmp_path, monkeypatch, graph):
    # The same content written with other bytes (indented, keys unsorted), every digest recomputed. The publisher
    # writes canonical bytes only, so a version stored any other way was not written by this request.
    backend, params, result = settings_commit(tmp_path, monkeypatch, graph)
    forged = forge_stored(backend, result, lambda intake: None,
                          serialize=lambda intake: json.dumps(intake, indent=1).encode("utf-8"))
    with pytest.raises(ValueError, match=REJECTED):
        prove(backend, forged, "solar-settings", params, 1, "settings-job")
