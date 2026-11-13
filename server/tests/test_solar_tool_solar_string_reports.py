"""String rebuild and string data (W2) through Studio's registry, read adapter, artifact store and broker."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

import broker_client
import catalog
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_artifacts
import solar_local_read as local
import solar_rooftop_chain as chain_module
import solar_tools
import store
import write_loop
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError, validate_graph
from solar_sizing_client import digest
from solar_solve_results import sync_assignments
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_solve_commit import seed

ROOT = Path(__file__).resolve().parents[2]
TENANT = "fixture-tenant"
REBUILD, DATA = "solar-string-rebuild", "solar-string-data"
S1, S2 = app_id("string", 1), app_id("string", 2)
P1, P2, P3 = (app_id("panel", n) for n in (1, 2, 3))
F1, F2 = app_id("frame", 1), app_id("frame", 2)
INV = app_id("inverter", 1)


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def builtin(tool):
    return local._load_builtin(tool)


def two_frames(g):
    f1 = g["frames"][0]
    f2 = copy.deepcopy(f1)
    f2.update(id=F2, name="Attic", panel_refs=[P2, P3], module_columns=2, module_slots=2)
    f1.update(panel_refs=[P1], module_columns=1, module_slots=1)
    f1["matrix"] = [[c for c in f1["matrix"][0] if c["panel_ref"] == P1]]
    f2["matrix"] = [[c for c in f2["matrix"][0] if c["panel_ref"] != P1]]
    f1["panel_assignments"] = [a for a in f1["panel_assignments"] if a["panel_ref"] == P1]
    f2["panel_assignments"] = [a for a in f2["panel_assignments"] if a["panel_ref"] != P1]
    for p in g["panels"]:
        if p["id"] != P1:
            p["frame_ref"] = F2
            p["matrix_cell"] = {"row": 0, "col": p["matrix_cell"]["col"] - 1}
    g["frames"].append(f2)
    sync_assignments(g)
    return g


def variant(g, name):
    g = copy.deepcopy(g)
    if name == "TF":
        g = two_frames(g)
    elif name == "E":
        g["strings"][1]["route"] = []
    elif name == "R":
        g["strings"][0]["route"] = [[0.0254, 0.0508], [0.5, 0.25, 3.0], [1.27, -0.254, 1.0]]
    elif name == "Z":
        g.update(strings=[], routes=[], schedules=[])
        g["inverters"][0]["input_assignments"] = []
        sync_assignments(g)
    elif name == "B":
        g["strings"][0]["route"] = [[1e11, 0], [1, 0]]
    elif name == "N":
        g["frames"][0]["name"] = "x" * 1025
    g = validate_graph(g)
    assert sha(g) == BASE[name]
    return g


BASE = {"G": "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3",
        "TF": "8290c6abae1868aa60ff2d42f60f10545c7f8693cd0390262b6dd13b87a0e42e",
        "E": "56ccfa4ee7877eae7e6228db56c51a0ce790c09428a6157ecbf0ea89c36573cf",
        "R": "29cc7dde8d8daa0eb64c9e34a2787b7fd26119e1242c670b094621137f6fe5d6",
        "Z": "a61ee741762699d8a71552b512dc36246b218862667574cf3cb3cef584cb32a8",
        "B": "a1e3fcbcdc10b3b04cc864e62b0d5d8e728df7513c3615cbcd41262671e816be",
        "N": "a5e33bc7b155b4493044fd39de9a36b72af1d1ba311d1ca22957b66dbde52137"}


def test_string_report_rebuild_kernel_projection():
    sent = [{"handle": "1", "panels": ["1", "2"], "label": {}}, {"handle": "2", "panels": ["1"], "label": {}}]
    rebuilt, count = chain_module.string_rebuild(copy.deepcopy(sent))
    assert rebuilt == sent and count == 2
    assert sha(rebuilt) == "54a0aaed3e0059835022e7a59b82ca1aa20fc12f2b0a4f7f4c01ba73ae585fb7"


def test_string_report_receipts_replay(graph):
    intake = json.loads((ROOT / "docs/parity/evidence/rooftop/chain/intake.json").read_text(encoding="utf-8"))
    r10 = json.loads((ROOT / "docs/parity/receipts/string-data/rooftop-chain-c10.json").read_text(encoding="utf-8"))
    r11 = json.loads((ROOT / "docs/parity/receipts/string-rebuild/rooftop-chain-c11.json").read_text(encoding="utf-8"))
    assert (r10["capability"], r10["comparator"]["verdict"]) == ("string-data", "pass")
    assert (r11["capability"], r11["comparator"]["verdict"]) == ("string-rebuild", "pass")
    rebuilt, count = chain_module.string_rebuild(intake["strings"])
    assert count == 173
    for side in ("plugin", "studio"):
        assert r11["comparison"][side]["after"]["rows"][0]["value"] == 173
    g = copy.deepcopy(graph)
    g["strings"] = [{"id": app_id("string", n + 1), "ordered_panel_refs": list(s["panels"])}
                    for n, s in enumerate(intake["strings"])]
    assert builtin(REBUILD).run(g, {}) == {
        "status": "rebuilt", "rebuilt_strings": 173,
        "message": "Rebuilt panel associations for 173 string(s)"}
    by_handle = {chain_module.validate_string(s)["handle"]: s for s in intake["strings"]}
    text = chain_module.string_data(intake["panel_groups"], [by_handle["A912"], by_handle["A90E"]])
    for side in ("plugin", "studio"):
        row = r10["comparison"][side]["after"]["rows"][0]
        assert "".join(row["chunks"]) == text.replace("\r\n", "\n") and row["lines"] == 82
    recorded = r10["comparison"]["plugin"]["provenance"]["file_sha256"]["string-data"]
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == recorded
    assert recorded == "3b6db8459596f3be9d339269c055f036c099f0ec4649e9ca1efd9923028e744c"
    assert builtin(DATA).render(json.loads(text)) == text
    assert text.count("\r\n") + 1 == 82


@pytest.mark.parametrize("name,count,size,digest_hex", [
    ("G", 2, 95, "5d4756f4d560961cf395e8d0991ea550a235239019389763a86b04faa9dba33a"),
    ("Z", 0, 95, "a6b651ef89e25aa7ac164e9d2454568a5f8aad9c596094e12b1f54fedd5e912f"),
])
def test_string_report_rebuild_outputs(graph, name, count, size, digest_hex):
    g = graph if name == "G" else variant(graph, name)
    before = copy.deepcopy(g)
    out = builtin(REBUILD).run(g, {})
    assert out == {"status": "rebuilt", "rebuilt_strings": count,
                   "message": f"Rebuilt panel associations for {count} string(s)"}
    assert len(canonical_bytes(out)) == size and digest(out) == digest_hex
    assert g == before


FILES = [
    ("G", {}, {"selected_strings": 2, "groups": 1, "grouped_strings": 2, "lines": 32}, 973,
     "791aabb46edd97af6ea989b8368767649317263820b463fb36d215e090993da3"),
    ("G", {"string_refs": [S2, S1]}, {"selected_strings": 2, "groups": 1, "grouped_strings": 2, "lines": 32}, 973,
     "967c7fa3e5ad782a98cedd94e11573a33e70d7f018c36555ff9c2e4df9e963b0"),
    ("G", {"string_refs": [S1]}, {"selected_strings": 1, "groups": 1, "grouped_strings": 1, "lines": 21}, 568,
     "5722c5f06d2e693d498a067d2b33f44e3230bf23b76b0a90a56c7c7bc35aea62"),
    ("TF", {}, {"selected_strings": 2, "groups": 2, "grouped_strings": 1, "lines": 26}, 696,
     "bc477a21ebd11e9c65fc2c7ee704b37aaa636d9cd1000b4431597880a5bcc2cf"),
    ("R", {"string_refs": [S1]}, {"selected_strings": 1, "groups": 1, "grouped_strings": 1, "lines": 21}, 570,
     "7de08641c43f32979bd92b4650308eb6c5e64d569f03a519ecbf41d7dd3cf4ea"),
    ("E", {"string_refs": [S1]}, {"selected_strings": 1, "groups": 1, "grouped_strings": 1, "lines": 21}, 568,
     "5722c5f06d2e693d498a067d2b33f44e3230bf23b76b0a90a56c7c7bc35aea62"),
]


@pytest.mark.parametrize("name,params,summary,size,content_sha", FILES)
def test_string_report_data_files(graph, name, params, summary, size, content_sha):
    g = graph if name == "G" else variant(graph, name)
    before = copy.deepcopy(g)
    out = builtin(DATA).run(g, copy.deepcopy(params))
    assert type(out) is solar_artifacts.ArtifactOutput
    assert out.summary == {"status": "written", **summary}
    assert (out.media_type, out.filename) == ("application/json", "StringData.json")
    assert len(out.content) == size and hashlib.sha256(out.content).hexdigest() == content_sha
    text = out.content.decode("utf-8")
    assert text.count("\r\n") == text.count("\n") == summary["lines"] - 1 and text.endswith("}")
    assert builtin(DATA).render(json.loads(text)) == text
    assert g == before
    data = json.loads(text)
    if name == "TF":
        assert [(grp["handle"], grp["name"], [s["handle"] for s in grp["strings"]]) for grp in data["groups"]] == [
            (F2, "Attic", [S2]), (F1, "Roof group", [])]
    if name == "R":
        row = data["groups"][0]["strings"][0]
        assert (row["startPoint"]["coordinate"], row["endPoint"]["coordinate"]) == ("1.00,2.00", "50.00,-10.00")


@pytest.mark.parametrize("name", ["Roof group", "RÃ¸of"])
def test_string_report_data_full_file(graph, name):
    g = variant(graph, "G")
    g["frames"][0]["name"] = name
    out = builtin(DATA).run(validate_graph(g), {})

    def row(string, start):
        return {"handle": string, "startPoint": {"handle": start, "coordinate": "0.00,0.00"},
                "endPoint": {"handle": INV, "coordinate": "39.37,0.00"}}
    expected = {"groups": [{"handle": F1, "name": name, "strings": [row(S1, P1), row(S2, P3)]}]}
    assert json.loads(out.content) == expected
    assert out.content == builtin(DATA).render(expected).encode("utf-8")
    if name == "RÃ¸of":
        assert "RÃ¸of".encode("utf-8") in out.content
        assert b"\\u00f8" not in out.content


@pytest.mark.parametrize("name,out,digest_hex", [
    ("E", {"status": "no-file", "reason": "missing-end-markers", "selected_strings": 2, "groups": 1},
     "e55c53644929c39927fd4489795ac49e39b21efe370163ac8e699c12cc582754"),
    ("NF", {"status": "no-file", "reason": "no-groups", "selected_strings": 2, "groups": 0},
     "c5b54517a07a8b2b07b4e21ba6d2441ca3b6f32aa36102d91164ca8a425380c7"),
    ("Z", {"status": "no-file", "reason": "no-strings", "selected_strings": 0, "groups": 1},
     "c74b4756e0cf2e14749e545e887c5c203316782e8a4c6f4d85824795fa99b436"),
])
def test_string_report_data_no_file(graph, name, out, digest_hex):
    if name == "NF":
        g = copy.deepcopy(graph)
        g["frames"] = []
    else:
        g = variant(graph, name)
    result = builtin(DATA).run(g, {})
    assert result == out and digest(result) == digest_hex


def _refusal(graph, monkeypatch, case):
    g = copy.deepcopy(graph)
    params = {}
    if case == "data-missing":
        tool, params = DATA, {"string_refs": [app_id("string", 99)]}
    elif case == "data-units":
        tool = DATA
        g["project"]["units"]["meters_per_unit"] *= 2
    elif case == "data-coordinate":
        tool, g = DATA, variant(graph, "B")
    elif case == "data-coordinate-overflow":
        tool = DATA
        g["strings"][0]["route"][0] = [1e308, 0]
        g = validate_graph(g)
    elif case == "data-name":
        tool, g = DATA, variant(graph, "N")
    elif case == "data-unknown-panel":
        tool = DATA
        g["strings"][0]["ordered_panel_refs"] = [P1, app_id("panel", 99)]
    elif case == "data-selection-cap":
        tool = DATA
        monkeypatch.setattr(builtin(DATA), "MAX_SELECTED", 1)
    elif case == "data-panels-per-string":
        tool = DATA
        monkeypatch.setattr(builtin(DATA).chain, "MAX_PANELS_PER_STRING", 1)
    elif case == "rebuild-strings":
        tool = REBUILD
        monkeypatch.setattr(builtin(REBUILD).chain, "MAX_STRINGS", 1)
    else:
        tool = REBUILD
        monkeypatch.setattr(builtin(REBUILD).chain, "MAX_PANELS_PER_STRING", 1)
    return tool, g, params


@pytest.mark.parametrize("case,code", [
    ("data-missing", "MISSING_STRING"), ("data-units", "UNRESOLVED_UNITS"),
    ("data-coordinate", "STRING_EDIT_BOUNDS_EXCEEDED"), ("data-name", "STRING_EDIT_BOUNDS_EXCEEDED"),
    ("data-coordinate-overflow", "STRING_EDIT_BOUNDS_EXCEEDED"),
    ("data-unknown-panel", "STRING_EDIT_MAPPING_FAILED"), ("data-selection-cap", "STRING_EDIT_BOUNDS_EXCEEDED"),
    ("data-panels-per-string", "STRING_EDIT_BOUNDS_EXCEEDED"), ("rebuild-strings", "STRING_EDIT_BOUNDS_EXCEEDED"),
    ("rebuild-panels-per-string", "STRING_EDIT_BOUNDS_EXCEEDED"),
])
def test_string_report_refusals(graph, monkeypatch, case, code):
    tool, g, params = _refusal(graph, monkeypatch, case)
    before, before_params = copy.deepcopy(g), copy.deepcopy(params)
    with pytest.raises(GraphValidationError) as error:
        builtin(tool).run(g, params)
    assert error.value.code == code
    assert g == before and params == before_params
    if case == "data-name":
        ok = copy.deepcopy(graph)
        ok["frames"][0]["name"] = "x" * 1024
        assert builtin(DATA).run(ok, {}).summary["status"] == "written"


@pytest.mark.parametrize("tool,params", [
    (REBUILD, None), (REBUILD, []), (REBUILD, {"x": 1}), (REBUILD, {"drawing_id": "solar"}),
    (REBUILD, {"string_refs": [S1]}),
    (DATA, None), (DATA, []), (DATA, {"x": 1}), (DATA, {"drawing_id": "solar"}),
    (DATA, {"string_refs": []}), (DATA, {"string_refs": S1}), (DATA, {"string_refs": [S1, S1]}),
    (DATA, {"string_refs": [S1, 7]}), (DATA, {"string_refs": [""]}), (DATA, {"string_refs": ["x" * 129]}),
])
def test_string_report_request_shape_fails_closed(graph, tool, params):
    code = "INVALID_STRING_REBUILD_REQUEST" if tool == REBUILD else "INVALID_STRING_DATA_REQUEST"
    with pytest.raises(GraphValidationError) as error:
        builtin(tool).run(copy.deepcopy(graph), params)
    assert error.value.code == code


@pytest.mark.parametrize("case", ["rebuild-panels", "rebuild-count", "data-handle", "data-none", "data-duplicate-group"])
def test_string_report_kernel_answer_is_checked(graph, monkeypatch, case):
    tool = REBUILD if case.startswith("rebuild") else DATA
    chain = builtin(tool).chain
    g = copy.deepcopy(graph)
    if case == "rebuild-panels":
        monkeypatch.setattr(chain, "string_rebuild",
                            lambda strings: ([dict(s, panels=s["panels"][::-1]) for s in strings], len(strings)))
    elif case == "rebuild-count":
        monkeypatch.setattr(chain, "string_rebuild", lambda strings: (list(strings), len(strings) + 1))
    elif case == "data-handle":
        real = chain.string_data
        monkeypatch.setattr(chain, "string_data",
                            lambda groups, strings: real(groups, strings).replace('"handle": "1"', '"handle": "9"', 1))
    elif case == "data-duplicate-group":
        g = validate_graph(two_frames(g))
        real = chain.string_data

        def duplicate_group(groups, strings):
            document = json.loads(real(groups, strings))
            first, second = document["groups"]
            second["handle"] = first["handle"]
            second["name"] = first["name"]
            return builtin(DATA).render(document)

        monkeypatch.setattr(chain, "string_data", duplicate_group)
    else:
        monkeypatch.setattr(chain, "string_data", lambda groups, strings: None)
    with pytest.raises(GraphValidationError) as error:
        builtin(tool).run(g, {})
    assert error.value.code == "STRING_EDIT_MAPPING_FAILED"


DECLARATIONS = {
    REBUILD: {"order": 76, "code": "INVALID_STRING_REBUILD_REQUEST", "facets": ["strings"],
              "ledger": ["string-rebuild"], "engine_op": "solar_string_rebuild",
              "properties": {"drawing_id": {"type": "string", "maxLength": 128}}},
    DATA: {"order": 78, "code": "INVALID_STRING_DATA_REQUEST", "facets": ["strings", "frames"],
           "ledger": ["string-data"], "engine_op": "solar_string_data",
           "properties": {"drawing_id": {"type": "string", "maxLength": 128},
                          "format": {"type": "string", "enum": ["string-data", "design-json"],
                                     "description": "String Data keeps the endpoint report. Full design JSON exports every current panel, group, ordered string, route and design metadata."},
                          "string_refs": {"type": "array", "minItems": 1, "maxItems": 10000, "uniqueItems": True,
                                          "items": {"type": "string", "minLength": 1, "maxLength": 128}}}},
}


def expected(tool):
    d = DECLARATIONS[tool]
    stem = d["engine_op"]
    return {
        "schema": "leaf.solar-tool.v1", "name": tool, "builtin": f"builtins/{stem}.py",
        "family": "stringing", "adapter": "local-graph-read", "entitlement": "run_read",
        "requires_persisted_graph": True, "seedable": False, "invalid_request_code": d["code"],
        "readiness": {"kind": "facets", "facets": d["facets"]}, "engine": "server-builtin",
        "interaction": {"mode": "form"}, "record_store": "registry",
        "record": {"name": tool, "version": "1.0.0", "kind": "script", "family_id": "stringing",
                   "engine_op": stem, "entry": f"builtins/{stem}.py",
                   "params": {"type": "object", "properties": d["properties"], "additionalProperties": False},
                   "returns": {"type": "object"}, "capabilities": ["drawing.read"], "allow_local_fallback": False},
        "ledger": d["ledger"], "trusted_inputs": [], "maturity": "preview", "wave": 2, "order": d["order"],
        "scenario": "w2-rooftop"}


@pytest.mark.parametrize("tool", [REBUILD, DATA])
def test_string_report_registry_and_catalog(monkeypatch, tool):
    declaration = solar_tools.get(tool)
    actual = copy.deepcopy(declaration)
    description = actual["record"].pop("description")
    assert type(description) is str and description.strip() and len(description) <= 1024
    assert actual == expected(tool)
    assert tool in solar_tools.local_graph_read_tools() and tool not in solar_tools.local_graph_tools()
    assert availability.SOLAR_CAPABILITIES[tool]["adapter"] == "local-graph-read"
    assert tool not in availability.W1_CAPABILITIES and len(availability.W1_CAPABILITIES) == 9
    assert entitlements.tool_required_capability(solar_tools.trusted_record(tool)) == "run_read"
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    families = catalog.build_catalog(deps.all_tools(TENANT))
    found = [(family, row) for family in families for row in family["capabilities"] if row["name"] == tool]
    assert len(found) == 1
    family, row = found[0]
    assert family["family_id"] == "stringing"
    assert row["solar"] == solar_tools.catalog_view(declaration["record"])
    assert row["params_schema"] == declaration["record"]["params"]


def test_string_report_params_schema():
    rebuild = Draft7Validator(solar_tools.trusted_record(REBUILD)["params"])
    assert rebuild.is_valid({}) and rebuild.is_valid({"drawing_id": "solar"})
    assert not rebuild.is_valid({"x": 1}) and not rebuild.is_valid({"drawing_id": "x" * 129})
    data = Draft7Validator(solar_tools.trusted_record(DATA)["params"])
    assert data.is_valid({}) and data.is_valid({"string_refs": [S1, S2]})
    for bad in ({"string_refs": []}, {"string_refs": [S1, S1]}, {"string_refs": [""]},
                {"string_refs": ["x" * 129]}, {"string_refs": [S1] + [f"s{n}" for n in range(10000)]},
                {"x": 1}):
        assert not data.is_valid(bad)


@pytest.mark.parametrize("tool", [REBUILD, DATA])
def test_string_report_readiness(graph, tool):
    assert availability.w1_graph_readiness(graph)[tool] == {"input_ready": True, "input_reason": None}
    assert availability.w1_graph_readiness(variant(graph, "Z"))[tool] == {
        "input_ready": False, "input_reason": "strings_required"}
    frameless = copy.deepcopy(graph)
    frameless["frames"] = []
    assert availability.w1_local_commit_inputs(frameless)[tool] == (
        {"input_ready": True, "input_reason": None} if tool == REBUILD
        else {"input_ready": False, "input_reason": "frames_required"})
    unresolved = copy.deepcopy(graph)
    unresolved["project"]["units"]["meters_per_unit"] *= 2
    assert availability.w1_local_commit_inputs(unresolved)[tool] == {
        "input_ready": False, "input_reason": "unresolved_units"}


@pytest.fixture
def api(isolated_jobs, no_network, graph, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import jobs as route

    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setenv("BROKER_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("BROKER_TENANTS", str(tmp_path / "tenants.json"))
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    import broker

    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "_tenants", {TENANT: {"tier": "demo", "disabled": False}})
    monkeypatch.setattr(broker, "_cap_preflight", lambda *a: None)
    monkeypatch.setattr(broker, "_emit_aps_metric", lambda *a: None)
    monkeypatch.setattr(broker, "_get_da", lambda: pytest.fail("APS must not execute"))
    monkeypatch.setattr(broker, "run_tool_dynamic", lambda *a, **k: pytest.fail("dynamic dispatch"))
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.setattr(deps, "AUTHORED_STORE", tmp_path / "absent-authored.json")
    records = {tool: deps.find_tool(tool, TENANT) for tool in (REBUILD, DATA)}
    assert all(records[tool] == solar_tools.trusted_record(tool) for tool in records)

    class InlineExecutor:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    monkeypatch.setattr(jobs, "_executors", {jobs.lane_for(records[DATA], False): InlineExecutor()})
    monkeypatch.setattr(route.deps, "backedge_run_identity", lambda tenant, *a: tenant)
    monkeypatch.setattr(route.deps, "auth_live", lambda: False)
    monkeypatch.setattr(jobs.platform_link, "resolve_submission_context", lambda *a: None)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    monkeypatch.setattr(route.catalog, "live_aps_runtime_authorized", lambda *a, **k: True)
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    monkeypatch.delenv("LEAF_EXACT_WRITE_PINS_REQUIRED", raising=False)
    requests = []

    def transport(url, *, json, headers, timeout):
        assert url.endswith("/broker/run")
        assert json["aps_live"] is False
        requests.append(copy.deepcopy(json))
        response = broker._broker_run(broker.BrokerRunRequest(**json))

        class Reply:
            status_code = response.status_code

            def json(self):
                return __import__("json").loads(response.body)

        return Reply()

    monkeypatch.setattr(broker_client.requests, "post", transport)
    app = FastAPI()
    app.include_router(route.router)
    tenant = route.deps.TenantContext(
        TENANT, tier="demo", subject="fixture-subject", authority_resolved=True)
    app.dependency_overrides[route.deps.require_tenant] = lambda: tenant
    with TestClient(app) as client:
        yield client, backend, records, tenant, requests


def body(api, tool, params):
    return {"tool": tool, "dwg": "solar", "params": copy.deepcopy(params),
            "catalog_digest": deps.catalog_tool_digest(api[2][tool])}


def keys(backend):
    return set(backend.drawing_object_keys(TENANT, "solar"))


def run(api, tool, params):
    response = api[0].post("/api/run?wait=1", json=body(api, tool, params))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    assert env["execution_provenance"]["execution_path"] == "local"
    rec = jobs.get_job(env["result"]["job_id"])
    assert rec["status"] == "complete" and rec["dwg_version"] == 1
    return env["result"]


def test_string_report_api_rebuild(api):
    before = keys(api[1])
    result = run(api, REBUILD, {})
    assert result["output"] == {"status": "rebuilt", "rebuilt_strings": 2,
                                "message": "Rebuilt panel associations for 2 string(s)"}
    assert result["output_sha256"] == "5d4756f4d560961cf395e8d0991ea550a235239019389763a86b04faa9dba33a"
    assert result["output_bytes"] == 95
    assert len(api[4]) == 1
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1
    assert keys(api[1]) == before


@pytest.mark.parametrize("params,content_sha,output_sha", [
    ({}, "791aabb46edd97af6ea989b8368767649317263820b463fb36d215e090993da3",
     "93a6cdbd3784415af8b9576715915f42d0244206496e831a3b6d88d7ece1d4cf"),
    ({"string_refs": [S2, S1]}, "967c7fa3e5ad782a98cedd94e11573a33e70d7f018c36555ff9c2e4df9e963b0",
     "3a33f13c2c92886e4ce7ac21e74a73b8b419223227ef285df04a4929c62da3c4"),
])
def test_string_report_api_data_writes_one_artifact(api, graph, params, content_sha, output_sha):
    before = keys(api[1])
    result = run(api, DATA, params)
    output = result["output"]
    assert set(output) == {"summary", "artifact"}
    assert output["summary"] == {"status": "written", "selected_strings": 2, "groups": 1,
                                 "grouped_strings": 2, "lines": 32}
    ref = output["artifact"]
    assert (ref["schema"], ref["media_type"], ref["filename"], ref["byte_length"], ref["content_sha256"],
            ref["source_version"]) == ("leaf.solar-artifact-ref.v1", "application/json", "StringData.json",
                                       973, content_sha, 1)
    assert ref["download"] == "/api/drawings/solar/artifacts/" + ref["artifact_id"]
    assert result["output_sha256"] == output_sha and result["output_bytes"] == 517
    meta, content = solar_artifacts.read_artifact(api[1], TENANT, "solar", ref["artifact_id"])
    assert content == builtin(DATA).run(graph, params).content and meta["tool"] == DATA
    added = keys(api[1]) - before
    assert len(added) == 2
    assert run(api, DATA, params)["output"] == output and keys(api[1]) - before == added
    assert len(api[4]) == 2
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1


@pytest.mark.parametrize("tool,params,reason", [
    (DATA, {"string_refs": []}, "tool_params_invalid"),
    (DATA, {"string_refs": [S1, S1]}, "tool_params_invalid"),
    (DATA, {"x": 1}, "tool_params_invalid"),
    (REBUILD, {"x": 1}, "tool_params_invalid"),
    (DATA, {"string_refs": [app_id("string", 99)]}, "MISSING_STRING"),
])
def test_string_report_api_refusals(api, tool, params, reason):
    before = keys(api[1])
    response = api[0].post("/api/run?wait=1", json=body(api, tool, params))
    assert response.status_code == 400
    env = response.json()
    assert env.get("ok") is not True
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert records and all(rec["status"] == "failed" for rec in records)
    assert [(rec.get("error") or {}).get("reason_code") for rec in records] == [reason]
    assert len(api[4]) == 1
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1
    assert keys(api[1]) == before


# S03 synthetic split/correction/export cases, kept on this registered suite.
from copy import deepcopy
from types import SimpleNamespace

import pytest

import leaf_cloud_client as cloud
import solar_project_solve as project_solve
import solar_sizing_client as sizing
import solar_solve_request as requests
from product_capability_availability import w1_graph_readiness
from solar_design_graph import validate_graph
from test_w1_design_graph import entity, graph  # noqa: F401


def manual_large_graph(base):
    value = deepcopy(base)
    frame = value["frames"][0]
    panels, matrix = [], []
    for row in range(22):
        cells = []
        for col in range(12):
            panel = entity("panel", len(panels) + 1, frame_ref=frame["id"],
                matrix_cell={"row": row, "col": col}, centre=[2.0 * col, -3.0 * row],
                angle=0.0, assignment={"string_ref": None, "seq": None})
            panels.append(panel)
            cells.append({"code": "panel", "panel_ref": panel["id"], "seq": None,
                "inverter_id": None, "string_input_number": None,
                "x": panel["centre"][0], "y": panel["centre"][1], "angle": 0.0})
        matrix.append(cells)
    frame.update(panel_refs=[p["id"] for p in panels], matrix=matrix,
        module_rows=22, module_columns=12, module_slots=264, module_power_watts=0,
        extra={"grouping_basis": "geometry-only", "module_power_status": "unknown",
               "sizing_evidence_mode": "manual-global"}, sequences=[],
        panel_assignments=[{"panel_ref": p["id"], "string_ref": None, "seq": None,
            "inverter_id": None, "string_input_number": None} for p in panels])
    value.update(panels=panels, strings=[], inverters=[], routes=[], schedules=[])
    value["electrical_zones"][0]["panel_refs"] = frame["panel_refs"][:]
    settings = value["settings"]
    settings.update(panels_in_sequence=14, voc_cold=sizing.manual_voltage(),
                    global_string_sizing_confirmed=True)
    settings["extra"]["string_sizing"] = sizing.manual_sizing_evidence(value)
    return validate_graph(value)


def test_manual_unknown_power_split_admission(graph, monkeypatch):
    def no_io(*args, **kwargs):
        pytest.fail("split planning must stay offline")
    monkeypatch.setattr(cloud, "resolve_grant", no_io)
    monkeypatch.setattr(cloud.requests.sessions.Session, "request", no_io)
    value = manual_large_graph(graph)
    before = deepcopy(value)
    frame = value["frames"][0]
    assert w1_graph_readiness(value)["solar-solve-proposal"]["input_ready"] is True
    assert requests.needs_split_solve(value, frame["id"], max_string_length=14)
    request = requests.build_stringer_request(value, frame["id"], max_string_length=14,
        dwgname="synthetic-manual-split", full_frame=True)
    plan = project_solve.plan_request(SimpleNamespace(graph=value,
        organization_id="synthetic-tenant"), {"grant_ref": "synthetic-no-access", "request": request},
        {"frame_ref": frame["id"], "expected_rev": value["rev"], "phase": "initial"})
    binding = plan["split_binding"]
    assert len(binding["pieces"]) > 1
    refs = [cell["Id"] for piece in binding["pieces"]
        for row in piece["request"]["grid"]["Rows"] for cell in row["Panels"] if cell["Code"] == 1]
    assert len(refs) == len(set(refs)) == 264
    assert set(refs) == set(frame["panel_refs"])
    assert plan["max_string_length"] == 14
    for piece in binding["pieces"]:
        parsed = cloud.PieceStringerRequest.model_validate(piece["request"])
        lengths, counts = parsed.grid.Sequences
        assert max(lengths) <= 14
        occupied = sum(cell.Code == 1 for row in parsed.grid.Rows for cell in row.Panels)
        assert sum(length * count for length, count in zip(lengths, counts)) == occupied
    assert value == before
    assert value["frames"][0]["module_power_watts"] == 0
    assert value["settings"]["extra"]["string_sizing"]["records"] == {}


def extra_panel_graph(base):
    value = manual_large_graph(base)
    frame = value["frames"][0]
    row = frame["module_rows"]
    panel = entity("panel", 265, frame_ref=frame["id"], matrix_cell={"row": row, "col": 0},
        centre=[0.0, -3.0 * row], angle=0.0, assignment={"string_ref": None, "seq": None})
    value["panels"].append(panel)
    frame["panel_refs"].append(panel["id"])
    frame["panel_assignments"].append({"panel_ref": panel["id"], "string_ref": None,
        "seq": None, "inverter_id": None, "string_input_number": None})
    frame["matrix"].append([{"code": "panel" if col == 0 else "empty",
        "panel_ref": panel["id"] if col == 0 else None, "seq": None,
        "inverter_id": None, "string_input_number": None,
        "x": 0.0, "y": panel["centre"][1] if col == 0 else 0.0, "angle": 0.0}
        for col in range(frame["module_columns"])])
    frame["module_rows"] += 1
    frame["module_slots"] += frame["module_columns"]
    value["electrical_zones"][0]["panel_refs"].append(panel["id"])
    value["settings"]["extra"]["string_sizing"] = sizing.manual_sizing_evidence(value)
    return validate_graph(value)


def synthetic_piece(piece, tenant, child_job, monkeypatch, *, partial):
    from leaf_cloud_grants import CloudGrant

    request = cloud.PieceStringerRequest.model_validate(piece["request"])
    final = request.wire_payload()["grid"]
    path = []
    for row, cells in enumerate(final["Rows"]):
        for col, cell in enumerate(cells["Panels"]):
            if cell["Code"] == 1:
                path.append([row + 1, col + 1])
                cell["Seq"] = len(path)
    data = {"status": "completed", "best_result": {"info": {"distance_total": 0.0}},
        "final_grid": final, "model_used": "synthetic-offline", "gumbel_scale_used": 0.0,
        "distance_total": 0.0, "total_valid_solutions": 1}
    if partial:
        data["message"] = "synthetic partial beam"
    else:
        lengths, counts = request.grid.Sequences
        sizes = [length for length, count in zip(lengths, counts) for _ in range(count)]
        info = dict(steps_taken=len(path), sequence_length=sizes, num_panels=len(path),
            visited_path=path, steps_taken_in_string=0, horizontal_movements=0,
            vertical_movements=0, invalid_vertical_movements=0, distance_total=0.0,
            vertical_sequential=0, diagonal_movements=0, allow_one_gap_hops=False,
            max_gap_hop=0, gap_hop_movements=0, vertical_temp=-1, remaining_string_length=0,
            sequence_length_index=0, str_length_state_index=0, terminated=True,
            last_agent_x=path[-1][0], last_agent_y=path[-1][1])
        data["best_result"] = dict(last_action=0, terminated=True, info=info, beam_idx=0,
            grid_id=child_job, model_id="synthetic-offline", gumbel_scale=0.0,
            string_start_split_count=0, model="synthetic-offline")
        data.update(gumbel_summary={}, first_pass_best_distance=0.0, improvement=None,
                    second_pass_triggered=False)
    raw = cloud.canonical_bytes({"status": "completed", "job_id": child_job, "data": data})
    # Exercise the real receipt producer with a synthetic transport, never a provider.
    monkeypatch.setattr(cloud, "resolve_grant", lambda ref, owner:
        CloudGrant(owner, "synthetic-not-a-credential"))
    monkeypatch.setattr(cloud, "post_stringer", lambda sent, grant: raw)
    return cloud.piece_proposal({"grant_ref": "synthetic-offline", "request": piece["request"]},
        tenant, child_job)


@pytest.mark.parametrize("all_partial", [False, True])
def test_manual_unknown_power_native_restitch_and_cut(graph, monkeypatch, all_partial):
    import solar_solve_results as solve
    from solar_design_graph import deserialize_graph, serialize_graph

    def no_network(*args, **kwargs):
        pytest.fail("offline synthetic fixture attempted a network call")
    monkeypatch.setattr(cloud.requests.sessions.Session, "request", no_network)
    value = extra_panel_graph(graph)
    before = deepcopy(value)
    frame = value["frames"][0]
    pieces = requests.build_split_requests(value, frame["id"], max_string_length=14,
        dwgname="synthetic-offline-restitch")
    assert len(pieces) > 1
    binding = solve.bind_split_request(value, pieces, expected_rev=value["rev"],
        frame_ref=frame["id"], tenant_id="synthetic-tenant", job_id="synthetic-parent",
        split_state={"jogs": 1, "depth": 10})
    proposals = [synthetic_piece(piece, binding["tenant_id"],
        solve.piece_job_id(binding["job_id"], index), monkeypatch,
        partial=all_partial or index > 0) for index, piece in enumerate(pieces)]
    candidate = solve.complete_split_search(value, binding, proposals)
    assert candidate["accepted"] is False
    solved = solve.accept_split_candidate(value, candidate, expected_rev=value["rev"])
    if all_partial:
        prefix, carried = [], 6  # Native fallback when no piece reports lengths.
    else:
        lengths, counts = cloud.PieceStringerRequest.model_validate(pieces[0]["request"]).grid.Sequences
        prefix = [length for length, count in zip(lengths, counts) for _ in range(count)]
        carried = prefix[-1]
    count, remainder = divmod(265 - sum(prefix), carried)
    assert remainder > 0  # This synthetic shape exercises the final partial cut.
    expected_sizes = prefix + [carried] * count + [remainder]
    assert [string["module_count"] for string in solved["strings"]] == expected_sizes
    refs = [ref for string in solved["strings"] for ref in string["ordered_panel_refs"]]
    assert len(refs) == len(set(refs)) == 265
    assert set(refs) == set(frame["panel_refs"])
    assert all(1 <= string["module_count"] <= 14 for string in solved["strings"])
    for string in solved["strings"]:
        assert string["extra"]["polarity"]["negative_panel_ref"] == string["ordered_panel_refs"][0]
        assert string["extra"]["polarity"]["positive_panel_ref"] == string["ordered_panel_refs"][-1]
        points = {panel["id"]: panel["centre"] for panel in solved["panels"]}
        assert string["route"] == [points[ref] for ref in string["ordered_panel_refs"]]
    assert solved["frames"][0]["module_power_watts"] == 0
    assert solved["frames"][0]["extra"]["module_power_status"] == "unknown"
    assert solved["settings"]["extra"]["string_sizing"]["records"] == {}
    assert len(solved["frames"][0]["extra"]["solve"]["split"]["pieces"]) == len(pieces)
    assert not any(solve.coverage(solved).values())
    assert deserialize_graph(serialize_graph(solved)) == solved
    assert value == before
    stale = deepcopy(value)
    stale["rev"] += 1
    stale["parent_rev"] = value["rev"]
    with pytest.raises(solve.GraphValidationError, match="STALE_SOLVE_RESULT"):
        solve.accept_split_candidate(stale, candidate, expected_rev=stale["rev"])


def test_native_membership_correction_refreshes_derived_geometry(graph, monkeypatch):
    import math
    import solar_solve_results as solve

    value = extra_panel_graph(graph)
    frame = value["frames"][0]
    pieces = requests.build_split_requests(value, frame["id"], max_string_length=14,
        dwgname="synthetic-correction")
    binding = solve.bind_split_request(value, pieces, expected_rev=value["rev"],
        frame_ref=frame["id"], tenant_id="synthetic-tenant", job_id="synthetic-correction",
        split_state={"jogs": 1, "depth": 10})
    proposals = [synthetic_piece(piece, binding["tenant_id"],
        solve.piece_job_id(binding["job_id"], index), monkeypatch, partial=True)
        for index, piece in enumerate(pieces)]
    before = solve.accept_split_candidate(value, solve.complete_split_search(value, binding, proposals),
        expected_rev=value["rev"])
    saved = deepcopy(before)
    first, second = before["strings"][:2]
    updates = [first["ordered_panel_refs"][:-1],
        first["ordered_panel_refs"][-1:] + second["ordered_panel_refs"]]
    params = {"expected_rev": before["rev"], "memberships": [
        {"string_ref": string["id"], "ordered_panel_refs": refs}
        for string, refs in zip((first, second), updates)]}
    corrected = solve.correct_graph(before, params)
    points = {panel["id"]: panel["centre"] for panel in corrected["panels"]}
    for string in corrected["strings"][:2]:
        refs = string["ordered_panel_refs"]
        route = [points[ref] for ref in refs]
        assert string["route"] == route
        assert string["length_ft"] == pytest.approx(sum(math.dist(a, b)
            for a, b in zip(route, route[1:])) / .3048)
        polarity = string["extra"]["polarity"]
        assert polarity["negative_panel_ref"] == refs[0]
        assert polarity["positive_panel_ref"] == refs[-1]
        assert polarity["source_rev"] == before["rev"]
        assert "response_sha256" not in polarity
        assert string["validity"]["state"] == "stale"
    for old, current in zip(before["strings"][2:], corrected["strings"][2:]):
        assert current["ordered_panel_refs"] == old["ordered_panel_refs"]
        assert current["route"] == old["route"]
        assert current["extra"]["polarity"] == old["extra"]["polarity"]
    assert before == saved
    assert corrected["frames"][0]["module_power_watts"] == 0
    assert not any(solve.coverage(corrected).values())
    with pytest.raises(solve.GraphValidationError, match="SOLAR_OUTPUT_NOT_CURRENT"):
        solve.require_current_export(corrected)
    restore = {"expected_rev": corrected["rev"], "memberships": [
        {"string_ref": string["id"], "ordered_panel_refs": string["ordered_panel_refs"]}
        for string in before["strings"][:2]]}
    restored = solve.correct_graph(corrected, restore)
    for old, current in zip(before["strings"], restored["strings"]):
        assert current["ordered_panel_refs"] == old["ordered_panel_refs"]
        assert current["route"] == old["route"]
        assert current["length_ft"] == old["length_ft"]
        assert current["extra"]["polarity"]["negative_panel_ref"] == old["ordered_panel_refs"][0]
        assert current["extra"]["polarity"]["positive_panel_ref"] == old["ordered_panel_refs"][-1]
    assert not any(solve.coverage(restored).values())


def test_legacy_manual_route_stays_stale_after_correction(graph):
    import solar_solve_results as solve

    before = deepcopy(graph)
    string = graph["strings"][0]
    corrected = solve.correct_graph(graph, {"expected_rev": graph["rev"], "memberships": [
        {"string_ref": string["id"], "ordered_panel_refs": string["ordered_panel_refs"][::-1]}]})
    assert corrected["strings"][0]["route"] == string["route"]
    assert corrected["strings"][0]["length_ft"] == string["length_ft"]
    assert corrected["strings"][0]["validity"]["state"] == "stale"
    assert graph == before


# S03 synthetic split/correction/export cases, kept on this registered suite.
from copy import deepcopy
from hashlib import sha256
import json

import pytest
from jsonschema import Draft7Validator

import solar_local_read as local
import solar_sizing_client as sizing
import solar_tools
from solar_design_graph import GraphValidationError
from test_w1_design_graph import graph  # noqa: F401

TOOL = "solar-string-data"


def manual_graph(graph):
    value = deepcopy(graph)
    value["frames"][0]["module_power_watts"] = 0
    value["frames"][0]["extra"] = {"grouping_basis": "geometry-only",
        "module_power_status": "unknown", "sizing_evidence_mode": "manual-global"}
    value["strings"][0]["extra"] = {"correction": {"note": "synthetic operator change"}}
    value["settings"].update(voc_cold=sizing.manual_voltage())
    value["settings"]["extra"]["string_sizing"] = sizing.manual_sizing_evidence(value)
    return value


def test_full_design_preserves_geometry_membership_and_unknown_power(graph):
    value = manual_graph(graph)
    before = deepcopy(value)
    result = local._load_builtin(TOOL).run(value, {"format": "design-json"})
    payload = json.loads(result.content)
    assert payload == {"schema": "leaf.solar-design-export.v1",
        "electrical_verification": "not-evaluated", "graph": value}
    assert result.filename == "SolarDesign.json"
    assert result.media_type == "application/json"
    assert result.summary["panels"] == 3
    assert result.summary["groups"] == 1
    assert result.summary["strings"] == 2
    assert value == before
    assert payload["graph"]["frames"][0]["module_power_watts"] == 0
    assert payload["graph"]["strings"][0]["ordered_panel_refs"] == value["strings"][0]["ordered_panel_refs"]
    assert payload["graph"]["strings"][0]["extra"]["correction"] == {"note": "synthetic operator change"}
    assert local._load_builtin(TOOL).run(value, {"format": "design-json"}).content == result.content


def test_legacy_string_data_unchanged(graph):
    module = local._load_builtin(TOOL)
    assert module.run(graph, {}) == module.run(graph, {"format": "string-data"})
    assert module.run(graph, {}).filename == "StringData.json"


@pytest.mark.parametrize("params", [{"format": True}, {"format": "other"},
    {"format": []}, {"format": "design-json", "string_refs": []}])
def test_design_export_refuses_ambiguous_or_invalid_format(graph, params):
    with pytest.raises(GraphValidationError, match="INVALID_STRING_DATA_REQUEST"):
        local._load_builtin(TOOL).run(manual_graph(graph), params)


def test_design_export_refuses_stale_design(graph):
    value = manual_graph(graph)
    value["panels"][0]["validity"] = {"state": "stale", "reasons": ["source_changed"]}
    with pytest.raises(GraphValidationError, match="SOLAR_OUTPUT_NOT_CURRENT"):
        local._load_builtin(TOOL).run(value, {"format": "design-json"})


def test_design_export_requires_current_manual_evidence(graph):
    value = manual_graph(graph)
    value["settings"]["extra"]["string_sizing"]["basis_sha256"] = "0" * 64
    with pytest.raises(GraphValidationError, match="SIZING_CONFIRMATION_REQUIRED"):
        local._load_builtin(TOOL).run(value, {"format": "design-json"})


def test_design_export_declaration_is_additive():
    params = solar_tools.trusted_record(TOOL)["params"]
    validator = Draft7Validator(params)
    assert validator.is_valid({})
    assert validator.is_valid({"format": "design-json"})
    assert not validator.is_valid({"format": "other"})


def test_full_design_uses_canonical_bound_artifact_download(graph, monkeypatch):
    import solar_project_artifacts as artifacts
    import test_sip_r6_read as reads

    # Start at a fresh imported source so the memory head names this graph,
    # rather than the last publication of an unrelated partial workflow.
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    lane = reads.tools_cases.memory.__wrapped__(monkeypatch)
    lane.set_parent(reads.tools_cases.embedded(manual_graph(graph)))
    lane.ctx = reads.graph_adapter.resolve_project_graph_context(lane.context())
    lane.tenant = reads.deps.TenantContext(str(lane.org), org_id=str(lane.org),
        tier="hosted_pro", subject="reader")
    monkeypatch.setattr(reads.deps, "find_tool", lambda name, tenant: solar_tools.trusted_record(name))
    monkeypatch.setattr(reads.read.stored, "stored_job_entitlement_verdict", lambda org, kind: (None, None))
    monkeypatch.setattr(artifacts.write_loop, "drawing_mutations_refusal", lambda: None)
    before = deepcopy(lane.versions)
    result = reads.run(lane, TOOL, {"format": "design-json"}, current=True)["result"]
    ref = result["output"]["artifact"]
    meta, content = artifacts.read_project_artifact(lane.tenant, lane.project, lane.drawing,
        ref["artifact_id"], current=True)
    assert json.loads(content)["graph"] == lane.ctx.graph
    assert ref["project_id"] == str(lane.project)
    assert ref["drawing_id"] == str(lane.drawing)
    assert ref["source_version_id"] == str(lane.head)
    assert meta["source_intake_sha256"] == lane.ctx.intake_sha256
    assert meta["graph_sha256"] == lane.ctx.graph_sha256
    assert meta["content_sha256"] == sha256(content).hexdigest()
    assert lane.versions == before
    assert result["drawing_changed"] is False


def test_design_export_refuses_incomplete_panel_coverage(graph):
    from solar_solve_results import sync_assignments

    value = manual_graph(graph)
    value["strings"].pop()
    value["inverters"][0]["input_assignments"].pop()
    value["schedules"] = []
    sync_assignments(value)
    with pytest.raises(GraphValidationError, match="SOLAR_OUTPUT_NOT_CURRENT"):
        local._load_builtin(TOOL).run(value, {"format": "design-json"})


def test_design_export_refuses_empty_design(graph):
    value = manual_graph(graph)
    for key in ("panels", "frames", "strings", "electrical_zones", "inverters", "routes", "schedules"):
        value[key] = []
    with pytest.raises(GraphValidationError, match="SOLAR_OUTPUT_NOT_CURRENT"):
        local._load_builtin(TOOL).run(value, {"format": "design-json"})


@pytest.mark.parametrize("all_partial", [False, True])
def test_restitched_design_canonical_export(graph, monkeypatch, all_partial):
    import solar_project_artifacts as artifacts
    import solar_solve_request as requests
    import solar_solve_results as solve
    import test_sip_r6_read as reads

    value = extra_panel_graph(graph)
    frame = value["frames"][0]
    pieces = requests.build_split_requests(value, frame["id"], max_string_length=14,
        dwgname="synthetic-restitched-export")
    binding = solve.bind_split_request(value, pieces, expected_rev=value["rev"],
        frame_ref=frame["id"], tenant_id="synthetic-tenant", job_id="synthetic-export",
        split_state={"jogs": 1, "depth": 10})
    proposals = [synthetic_piece(piece, binding["tenant_id"],
        solve.piece_job_id(binding["job_id"], index), monkeypatch,
        partial=all_partial or index > 0) for index, piece in enumerate(pieces)]
    solved = solve.accept_split_candidate(value, solve.complete_split_search(value, binding, proposals),
        expected_rev=value["rev"])
    original = deepcopy(solved)
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    lane = reads.tools_cases.memory.__wrapped__(monkeypatch)
    lane.set_parent(reads.tools_cases.embedded(solved))
    lane.ctx = reads.graph_adapter.resolve_project_graph_context(lane.context())
    lane.tenant = reads.deps.TenantContext(str(lane.org), org_id=str(lane.org),
        tier="hosted_pro", subject="reader")
    monkeypatch.setattr(reads.deps, "find_tool", lambda name, tenant: solar_tools.trusted_record(name))
    monkeypatch.setattr(reads.read.stored, "stored_job_entitlement_verdict", lambda org, kind: (None, None))
    monkeypatch.setattr(artifacts.write_loop, "drawing_mutations_refusal", lambda: None)
    result = reads.run(lane, TOOL, {"format": "design-json"}, current=True)["result"]
    ref = result["output"]["artifact"]
    meta, content = artifacts.read_project_artifact(lane.tenant, lane.project, lane.drawing,
        ref["artifact_id"], current=True)
    payload = json.loads(content)
    assert payload["graph"] == solved
    assert payload["electrical_verification"] == "not-evaluated"
    assert meta["project_id"] == str(lane.project)
    assert meta["drawing_id"] == str(lane.drawing)
    assert meta["source_version_id"] == str(lane.head)
    assert meta["source_intake_sha256"] == lane.ctx.intake_sha256
    assert meta["graph_sha256"] == solve.digest(solved)
    assert meta["content_sha256"] == sha256(content).hexdigest()
    assert result["output"]["summary"]["panels"] == 265
    exported = payload["graph"]
    refs = [ref for string in exported["strings"] for ref in string["ordered_panel_refs"]]
    assert len(refs) == len(set(refs)) == 265
    assert set(refs) == set(frame["panel_refs"])
    assert all(1 <= string["module_count"] <= 14 for string in exported["strings"])
    assert exported["frames"][0]["module_power_watts"] == 0
    assert exported["frames"][0]["extra"]["module_power_status"] == "unknown"
    assert len(exported["frames"][0]["extra"]["solve"]["split"]["pieces"]) == len(pieces)
    assert solved == original
    target = solved["strings"][0]
    corrected = solve.correct_graph(solved, {"expected_rev": solved["rev"], "memberships": [
        {"string_ref": target["id"], "ordered_panel_refs": target["ordered_panel_refs"][::-1]}]})
    with pytest.raises(GraphValidationError, match="SOLAR_OUTPUT_NOT_CURRENT"):
        local._load_builtin(TOOL).run(corrected, {"format": "design-json"})
