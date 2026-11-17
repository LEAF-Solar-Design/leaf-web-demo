"""Bounded failed-Solve attribution through the normal authenticated job read."""
from copy import deepcopy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import deps
import deployment_identity
import leaf_cloud_client as cloud
import platform_link
import solar_project_context as project
import solar_project_graph as graph_api
import solar_project_solve as solve
import solar_sizing_client as sizing
import solar_tools
from leaf_platform import canonical_jobs, project_solve_store as store
from routers import jobs as route
from solar_design_graph import validate_graph
from test_w1_design_graph import app_id, graph  # noqa: F401

ORG, PROJECT, DRAWING, VERSION, JOB, ACTOR = [UUID(int=n) for n in range(1, 7)]
SOURCE = "a" * 40


def native_response(request):
    """Hand-built producer response, with genuine wire echo and visited topology."""
    model = cloud.PieceStringerRequest.model_validate(request)
    final = model.wire_payload()["grid"]
    path = []
    for r, row in enumerate(final["Rows"], 1):
        for c, cell in enumerate(row["Panels"], 1):
            if cell["Code"] == 1:
                path.append([r, c])
                cell["Seq"] = len(path)
    lengths, counts = model.grid.Sequences
    sequence = [length for length, count in zip(lengths, counts) for _ in range(count)]
    info = {name: 0 for name in cloud.StringerInfo.model_fields}
    info.update(steps_taken=len(path), sequence_length=sequence, num_panels=len(path),
                visited_path=path, terminated=True, allow_one_gap_hops=False,
                last_agent_x=path[-1][0], last_agent_y=path[-1][1])
    return {"status": "completed", "job_id": "synthetic-grid", "data": {
        "status": "completed", "best_result": {"last_action": 0, "terminated": True,
            "info": info, "beam_idx": 0, "grid_id": "synthetic-grid", "model_id": "synthetic",
            "gumbel_scale": 0, "string_start_split_count": 0, "model": "synthetic"},
        "final_grid": final, "model_used": "synthetic", "gumbel_scale_used": 0,
        "distance_total": 0, "total_valid_solutions": 1, "gumbel_summary": {},
        "first_pass_best_distance": 0, "improvement": None, "second_pass_triggered": False}}


class ReadStore:
    """Two scoped tables behind the real bounded SELECT projection."""
    def __init__(self, parent, children):
        self.parent = parent
        self.children = children
        self.queries = []
        self.oversized = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, args):
        assert sql.startswith("SELECT ") and "FOR UPDATE" not in sql
        assert args["org_id"] == ORG and args["project_id"] == PROJECT
        assert args["drawing_id"] == DRAWING and args["job_id"] == JOB
        self.queries.append(sql)
        if sql.startswith("SELECT count"):
            rows = [self.parent, *self.children]
            size = sum(len(row[key] or b"") for row in rows for key in (
                "request_bytes", "response_bytes", "candidate_bytes", "proof_bytes"))
            if self.oversized:
                size = 16 * store.MAX_RESPONSE_BYTES + 1
            self.value = {"count": len(rows), "bytes": size}
        elif "project_solve_piece_exchanges" in sql:
            self.value = deepcopy(self.children)
        else:
            self.value = deepcopy(self.parent)

    def fetchone(self):
        return self.value

    def fetchall(self):
        return self.value


@pytest.fixture
def world(graph, monkeypatch):
    frame = graph["frames"][0]
    template = graph["panels"][0]
    panels, matrix = [], []
    for r in range(15):
        row = []
        for c in range(14):
            n = r * 14 + c
            ref = app_id("panel", n + 1) if n < 201 else None
            if ref:
                panel = deepcopy(template)
                panel.update(id=ref, centre=[float(c), float(r * 2)], matrix_cell={"row": r, "col": c},
                             assignment={"string_ref": None, "seq": None})
                panels.append(panel)
            row.append({"code": "panel" if ref else "empty", "panel_ref": ref,
                        "seq": None, "inverter_id": None, "string_input_number": None,
                        "x": float(c), "y": float(r * 2), "angle": 0})
        matrix.append(row)
    frame.update(panel_refs=[p["id"] for p in panels], matrix=matrix, module_rows=15,
                 module_columns=14, module_slots=210, sequences=[], electrical_zone_ref=None,
                 panel_assignments=[{"panel_ref": p["id"], "string_ref": None, "seq": None,
                                     "inverter_id": None, "string_input_number": None} for p in panels])
    graph.update(panels=panels, strings=[], inverters=[], electrical_zones=[], routes=[], schedules=[])
    graph["settings"].update(panels_in_sequence=14, voc_cold=sizing.manual_voltage())
    graph["settings"]["extra"]["string_sizing"] = sizing.manual_sizing_evidence(graph)
    validate_graph(graph)
    intake = {"solar_design_graph": graph, "solar_design_graph_sha256": solve.kernel.digest(graph)}
    raw = cloud.canonical_bytes(intake)
    version = SimpleNamespace(org_id=ORG, project_id=PROJECT, drawing_id=DRAWING,
        version_id=VERSION, deleted_at=None, intake_ref="synthetic-private-input",
        created_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
    binding = project.VersionBinding(ORG, PROJECT, DRAWING, VERSION, VERSION, True, version)
    verified = project.VerifiedProjectContext(binding, raw, intake, solve._sha(raw), version.intake_ref)
    context = graph_api.resolve_project_graph_context(verified)
    request = solve.requests.build_stringer_request(graph, frame["id"], max_string_length=14,
                                                    dwgname=graph["project"]["name"], full_frame=True)
    params = {"grant_ref": "synthetic-grant", "request": request}
    plan = solve.plan_request(context, params, {"frame_ref": frame["id"], "expected_rev": 0,
                                              "phase": "initial"}, job_id=JOB)
    job = {"kind": "run", "job_id": JOB, "org_id": ORG, "project_id": PROJECT,
        "input_version_id": VERSION, "request_tenant_id": str(ORG), "tool_name": solve.TOOL_NAME,
        "params": params, "status": "failed", "attempt": 1, "max_attempts": 3, "lease_owner": None,
        "execution_context": {"schema": canonical_jobs.PROJECT_GRAPH_JOB_SCHEMA,
            "authority_mode": "postgres_canonical", "execution_path": "local",
            "drawing_id": str(DRAWING), "actor_binding_id": str(ACTOR), "checkout_fence": "1",
            "tool_manifest_sha256": deps.catalog_tool_digest(solar_tools.trusted_record(solve.TOOL_NAME)),
            "parent_intake_sha256": context.intake_sha256, "initialized": False,
            "solve_binding": plan["solve_binding"]}}
    scope = store.ExchangeScope(ORG, PROJECT, DRAWING, JOB)
    parent = {**solve._reservation(job, context, plan, "live"), **vars(scope), "state": "prepared"}
    for name in ("response", "candidate", "proof"):
        parent[name + "_bytes"] = parent[name + "_sha256"] = None
    children = []
    for i, piece in enumerate(plan["split_binding"]["pieces"]):
        child_scope = solve._piece_scope(scope, plan["split_binding"], i)
        child = {**solve._reservation(job, context, solve._piece_plan(plan, piece), "live"),
                 **vars(child_scope), "state": "completed", "solver_endpoint": cloud.SOLVER_URL,
                 "solver_version": "1.0.0", "response_bytes": cloud.canonical_bytes(native_response(piece["request"]))}
        child["response_sha256"] = solve._sha(child["response_bytes"])
        _, child["candidate_bytes"], child["proof_bytes"] = solve._piece_evidence(job, context, plan, i, child)
        child["candidate_sha256"] = solve._sha(child["candidate_bytes"])
        child["proof_sha256"] = solve._sha(child["proof_bytes"])
        children.append(child)
    db = ReadStore(parent, children)
    monkeypatch.setattr(platform_link, "platform_db", lambda: SimpleNamespace(
        run_transaction=lambda fn, **kwargs: fn(db)))
    monkeypatch.setattr(graph_api, "_stored_job", lambda job_id, conn: deepcopy(job))
    monkeypatch.setattr(canonical_jobs, "_graph_scope", lambda *args: None)
    monkeypatch.setattr(project, "resolve_context", lambda *args, **kwargs: verified)
    monkeypatch.setattr(deployment_identity, "release_source_identity", lambda:
                        {"release_source_sha": SOURCE, "source_identity": "built"})
    principal = {"tenant": deps.TenantContext(str(ORG), subject="synthetic-reader"), "member": True}
    observed = platform_link._canonical_record(canonical_jobs._record(job))
    monkeypatch.setattr(route, "_job_for_tenant", lambda job_id, tenant:
                        deepcopy(observed) if tenant == str(ORG) and job_id == str(JOB) else None)
    def access(tenant, project_id, **kwargs):
        if not principal["member"]:
            raise platform_link.ProjectSessionForbidden()
        assert project_id == PROJECT
        return str(ORG)
    monkeypatch.setattr(platform_link, "require_project_access", access)
    def forbidden(*args, **kwargs):
        pytest.fail("dispatch, grant, write or checkout called by attribution")
    for name in ("post_stringer", "submit_stringer_async", "get_stringer_result", "resolve_grant"):
        monkeypatch.setattr(cloud, name, forbidden)
    for name in ("prepare_exchange", "begin_send", "capture_response", "complete_exchange", "complete_split_manifest"):
        monkeypatch.setattr(store, name, forbidden)
    monkeypatch.setattr(project.graph_store(), "acquire_checkout", forbidden)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[deps.require_tenant] = lambda: principal["tenant"]
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, db=db, parent=parent, children=children,
                              job=job, observed=observed, principal=principal,
                              context=context, plan=plan)


def read(world, version=VERSION):
    return world.client.get(f"/api/jobs/{JOB}?assembly_input_version={version}")


def test_valid_completed_children_rederive_without_mutation(world):
    before = deepcopy((world.parent, world.children, world.job))
    response = read(world)
    assert response.status_code == 200
    result = response.json()["assembly_attribution"]
    assert result["status"] == "validated", result
    assert result["historical_instruction_attributed"] is False
    assert result["implementation_source_sha"] == SOURCE
    assert result["completed_pieces"] == len(world.children)
    assert (world.parent, world.children, world.job) == before
    assert len(world.db.queries) == 3


def test_real_assembly_refusal_is_sanitized_current_source_attribution(world):
    # The producer echo/path remain valid, but the native sequence numbers omit
    # every panel from final cut coverage. Recompute genuine stored custody.
    for i, child in enumerate(world.children):
        response = json.loads(child["response_bytes"])
        for row in response["data"]["final_grid"]["Rows"]:
            for cell in row["Panels"]:
                cell["Seq"] = 0
        child["response_bytes"] = cloud.canonical_bytes(response)
        child["response_sha256"] = solve._sha(child["response_bytes"])
        _, child["candidate_bytes"], child["proof_bytes"] = solve._piece_evidence(
            world.job, world.context, world.plan, i, child)
        child["candidate_sha256"] = solve._sha(child["candidate_bytes"])
        child["proof_sha256"] = solve._sha(child["proof_bytes"])
    before = deepcopy((world.parent, world.children, world.job))
    result = read(world).json()["assembly_attribution"]
    assert result == {"schema": "leaf.solar-assembly-attribution.v1",
        "mode": "current-source-read-only-rederivation", "historical_instruction_attributed": False,
        "implementation_source_sha": SOURCE, "job_id": str(JOB), "input_version_id": str(VERSION),
        "status": "attributed", "reason": "INVALID_FINAL_GRID_ORDER", "validation_stage": "split.coverage",
        "completed_pieces": len(world.children), "total_pieces": len(world.children),
        "piece_order": list(range(len(world.children))), "piece_order_truncated": False}
    assert (world.parent, world.children, world.job) == before


def test_byte_limit_refuses_before_loading_evidence(world):
    world.db.oversized = True
    result = read(world).json()["assembly_attribution"]
    assert result["status"] == "unavailable"
    assert result["reason"] == "evidence_integrity_or_limit_refusal"
    assert len(world.db.queries) == 1


def test_unattested_current_source_is_explicitly_unavailable(world, monkeypatch):
    monkeypatch.setattr(deployment_identity, "release_source_identity", lambda:
                        {"release_source_sha": None, "source_identity": "unattested"})
    result = read(world).json()["assembly_attribution"]
    assert result["reason"] == "implementation_source_unavailable"
    assert world.db.queries == []


@pytest.mark.parametrize("corrupt", [False, True])
def test_completed_parent_custody_is_still_checked(world, corrupt):
    manifest = solve._piece_manifest(world.children)
    world.parent.update(response_bytes=manifest, response_sha256=solve._sha(manifest),
                        solver_endpoint=cloud.SOLVER_URL, solver_version="1.0.0")
    candidate, proof, _ = solve._derive_split(world.job, world.context, world.plan,
                                             world.parent, children=world.children)
    if corrupt:
        candidate = b'{"private-candidate":"must-not-leak"}'
    world.parent.update(state="completed", candidate_bytes=candidate, proof_bytes=proof,
                        candidate_sha256=solve._sha(candidate), proof_sha256=solve._sha(proof))
    response = read(world)
    result = response.json()["assembly_attribution"]
    assert result["status"] == ("unavailable" if corrupt else "validated")
    assert "must-not-leak" not in response.text


@pytest.mark.parametrize("fault", ["tenant", "member", "version", "stored_parent", "stored_tenant",
    "missing", "duplicate", "incomplete", "corrupt", "child_version", "child_parent", "proof"])
def test_invalid_authority_or_evidence_fails_closed(world, fault):
    version = VERSION
    if fault == "tenant":
        world.principal["tenant"] = deps.TenantContext(str(UUID(int=99)), subject="other")
    elif fault == "member":
        world.principal["member"] = False
    elif fault == "version":
        version = UUID(int=99)
    elif fault == "stored_parent":
        world.job["input_version_id"] = UUID(int=99)
    elif fault == "stored_tenant":
        world.job["request_tenant_id"] = "other"
    elif fault == "missing":
        world.children.pop()
    elif fault == "duplicate":
        world.children[-1] = deepcopy(world.children[0])
    elif fault == "incomplete":
        world.children[0]["state"] = "responded"
    elif fault == "corrupt":
        world.children[0]["response_sha256"] = "0" * 64
    elif fault == "child_version":
        world.children[0]["input_version_id"] = UUID(int=99)
    elif fault == "child_parent":
        world.children[0]["job_id"] = UUID(int=99)
    elif fault == "proof":
        world.children[0]["proof_bytes"] = b'{"private-response":"must-not-leak"}'
        world.children[0]["proof_sha256"] = solve._sha(world.children[0]["proof_bytes"])
    response = read(world, version)
    if fault in {"tenant", "member"}:
        assert response.status_code in {403, 404}
    else:
        assert response.status_code == 200
        assert response.json()["assembly_attribution"]["status"] == "unavailable"
    assert "must-not-leak" not in response.text
    if fault in {"tenant", "member", "version", "stored_parent", "stored_tenant"}:
        assert world.db.queries == []
