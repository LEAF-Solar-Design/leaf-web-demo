"""Durable graph orchestration seams; PostgreSQL authority is tested separately."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import inspect
import json
import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import UUID

import pytest

import canonical_worker as worker
import platform_link
import solar_project_jobs as service
import solar_project_graph as graph
import solar_project_context as project
import solar_tools
import tool_validate
from leaf_platform import canonical_jobs as jobs, entitlements
from solar_design_graph import GraphValidationError
from test_sip_r3a_graph import UNITS, prepare as seed, publish, refused
from test_sip_r3b_tools import chain, memory, manifest, prepare  # noqa: F401
from test_sip_r3b_chain import NINE, S, E, rev4, commit, through_string
from test_w1_design_graph import graph as graph_fixture  # noqa: F401
from test_w1_equipment import case, equipment, licensed  # noqa: F401
from test_w2_string_add import free_panels, solved  # noqa: F401

SIMPLE = {"expected_rev": 1}
CLEAN = {
    "solar-assign-equipment": SIMPLE, "solar-feeders": SIMPLE, "solar-homeruns": SIMPLE,
    "solar-panels-from-drawing": SIMPLE, "solar-schedule": SIMPLE,
    "solar-settings": {"expected_rev": 1, "changes": {"num_mppt": 2}},
    "solar-size-strings": {"expected_rev": 3, "mode": "manual-global", "confirm": True},
    "solar-combiners": {"expected_rev": 1, "hardware": {"model": "fixture", "max_dc_voltage": 1500,
                                                       "max_ac_power_kw": 100}},
    "solar-string-add": S,
}


def forbidden(*args, **kwargs):
    raise AssertionError("unexpected content, admission, or terminal work")


def record(s, prepared):
    r = prepared.request
    return {"job_id": r["job_id"], "org_id": r["organization_id"],
        "project_id": r["project_id"], "input_version_id": r["parent_version_id"],
        "request_tenant_id": str(s.org), "kind": "run", "tool_name": r["tool"],
        "params": r["parameters"], "status": "running", "attempt": r["attempt"],
        "max_attempts": 3, "lease_owner": "owner", "lease_expires_at": s.lease.expires_at,
        "execution_context": {"schema": jobs.PROJECT_GRAPH_JOB_SCHEMA,
            "authority_mode": "postgres_canonical", "execution_path": "local",
            **{k: r[k] for k in ("drawing_id", "actor_binding_id", "checkout_fence",
                "tool_manifest_sha256", "parent_intake_sha256", "initialized")}},
        "result": None, "output_version_id": None, "deleted_at": None}


class Connection:
    def __init__(self, s, row):
        self.s, self.row, self.selected = s, row, None

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, args):
        if sql.startswith("SELECT * FROM jobs"):
            self.selected = "job"
        else:
            self.selected = "fingerprint"
            self.s.execute(sql, args)

    def fetchone(self):
        return self.row if self.selected == "job" else self.s.fetchone()


@pytest.fixture
def admission(memory, monkeypatch):
    events, accepted = [], []
    monkeypatch.setattr(platform_link.platform_db(), "run_transaction", lambda op: op(memory))
    monkeypatch.setattr(jobs, "get_project_graph_job_by_key", lambda *a, **k: None)
    original_access = project._access
    monkeypatch.setattr(project, "_access", lambda *a, **k:
                        (events.append("access"), original_access(*a, **k))[1])
    monkeypatch.setattr(project, "verify_at_admission", lambda *a, **k:
        (events.append(("admission", a, k)), project.AdmissionContext(memory.context(), memory.lease))[1])
    monkeypatch.setattr(entitlements, "stored_job_entitlement_verdict", lambda *a:
        (events.append("entitlement"), (None, SimpleNamespace(tier="hosted_pro")))[1])

    @contextmanager
    def guard():
        events.append("guard")
        yield None
    monkeypatch.setattr(project.write_loop, "drawing_mutation_refusal_guard", guard)

    def insert(org, proj, tenant, tool, params, key, **kwargs):
        events.append("insert")
        accepted.append((org, proj, tenant, tool, deepcopy(params), key, kwargs))
        return {"job_id": str(memory.job), "kind": "run", "status": "queued",
                "attempt": 0, "max_attempts": 3, "result": None, "output_version_id": None,
                "lease_owner": None, "lease_expires_at": None}
    monkeypatch.setattr(jobs, "submit_project_graph_job", insert)
    return SimpleNamespace(s=memory, events=events, accepted=accepted)


def submit(a, **changes):
    s = a.s
    args = {"tenant": str(s.org), "project_id": s.project, "drawing_id": s.drawing,
        "input_version_id": s.parent.version_id, "tool_name": "solar-settings",
        "tool_manifest_sha256": manifest("solar-settings"),
        "checkout_capability": "private-capability", "idempotency_key": " key "}
    args.update(changes)
    if "params" not in args:
        args["params"] = seed(s).request["parameters"]
    return service.submit_project_graph_job(**args)


def test_sip_r4_membership_and_worker_choice(monkeypatch):
    assert graph.SUPPORTED_TOOLS == NINE
    assert set(worker.ADAPTERS) == {worker.autofill.TOOL_NAME, worker.arlo_design.TOOL_NAME}
    assert inspect.signature(worker.run_once).parameters["tool_name"].default == worker.autofill.TOOL_NAME
    source = inspect.getsource(worker.main)
    assert "choices=sorted(set(ADAPTERS) | solar_project_graph.SUPPORTED_TOOLS)" in source
    parsers, parse_args = [], worker.argparse.ArgumentParser.parse_args
    def capture(self, *a, **k):
        parsers.append(self)
        return parse_args(self, *a, **k)
    with monkeypatch.context() as patch:
        patch.setattr(worker.argparse.ArgumentParser, "parse_args", capture)
        patch.setattr(sys, "argv", ["canonical_worker", "--help"])
        with pytest.raises(SystemExit) as exc:
            worker.main()
        assert exc.value.code == 0
    choice = next(action for action in parsers[0]._actions if action.dest == "tool")
    assert choice.choices == sorted(set(worker.ADAPTERS) | NINE)
    assert choice.default == worker.autofill.TOOL_NAME
    calls = []
    monkeypatch.setattr(service, "run_once", lambda *a, **k: calls.append((a, k)) or False)
    for tool in NINE:
        assert worker.run_once("owner", tool_name=tool) is False
        assert calls[-1][1]["tool_name"] == tool
    with pytest.raises(ValueError, match="no canonical solver adapter"):
        worker.run_once("owner", tool_name="solar-string-multi-add")


def test_sip_r4_submission_context(admission, monkeypatch):
    a = admission
    result = submit(a, params={**seed(a.s).request["parameters"], "drawing_id": str(a.s.drawing)})
    assert (result["kind"], result["status"], result["attempt"], result["max_attempts"]) == ("run", "queued", 0, 3)
    assert a.events[0] == "access"
    assert [x if isinstance(x, str) else x[0] for x in a.events] == [
        "access", "admission", "entitlement", "guard", "insert"]
    call = a.accepted[0]
    assert call[:2] == (a.s.org, a.s.project) and call[5] == " key "
    assert "drawing_id" not in call[4]
    assert call[6]["execution_context"]["actor_binding_id"] == str(a.s.actor)
    assert call[6]["execution_context"]["checkout_fence"] == str(a.s.lease.fence)
    assert a.events[1][2]["checkout_capability"] == "private-capability"
    refused("SIP_R1_INVALID_BINDING", lambda: submit(a, drawing_id=True))
    refused("SIP_R4_IDEMPOTENCY_KEY_REQUIRED", lambda: submit(a, idempotency_key=" "))
    refused("SIP_R3_TOOL_MANIFEST_MISMATCH", lambda: submit(a, tool_manifest_sha256="bad"))
    refused("INVALID_SEED_REQUEST", lambda: submit(a, tool_name="solar-feeders",
                                                  tool_manifest_sha256=manifest("solar-feeders")))
    with monkeypatch.context() as patch:
        def inaccessible(*a, **k):
            project.refuse("PROJECT_FORBIDDEN")
        patch.setattr(project, "_access", inaccessible)
        refused("SIP_R1_PROJECT_FORBIDDEN", lambda: submit(a, drawing_id=True, idempotency_key=None))
    with monkeypatch.context() as patch:
        def stale(*a, **k):
            project.refuse("STALE_VERSION")
        patch.setattr(project, "verify_at_admission", stale)
        refused("SIP_R1_STALE_VERSION", lambda: submit(a, tool_manifest_sha256="bad"))


def test_sip_r4_submission_no_secrets(admission, monkeypatch):
    submit(admission)
    accepted = admission.accepted[0]
    kwargs = accepted[6]
    context = kwargs["execution_context"]
    assert set(context) == {"schema", "authority_mode", "execution_path", "drawing_id",
        "actor_binding_id", "checkout_fence", "tool_manifest_sha256", "parent_intake_sha256", "initialized"}
    # Every argument the insert receives is read, the positional parameters included.
    persisted = json.dumps([accepted[:6], {k: v for k, v in kwargs.items() if k != "conn"}],
                           default=str)
    assert "private-capability" not in persisted and "Bearer" not in persisted
    assert context["authority_mode"] == "postgres_canonical" and context["execution_path"] == "local"
    assert "storage" not in context and "request" not in context
    # A field the tool's closed schema does not declare never reaches the insert.
    for extra in ({"storage_ref": "caller/chosen/path"},
                  {"authorization": "Bearer synthetic-marker"},
                  {"storage_ref": "caller/chosen/path", "authorization": "Bearer synthetic-marker"}):
        with pytest.raises(GraphValidationError, match="INVALID_SETTINGS_REQUEST"):
            submit(admission, params=dict(seed(admission.s).request["parameters"], **extra))
    assert len(admission.accepted) == 1 and admission.events.count("insert") == 1

    calls = []
    original = service._validate_new_project_graph_params
    def spy(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "_validate_new_project_graph_params", spy)
    assert set(CLEAN) == NINE
    for name in sorted(NINE):
        clean = deepcopy(CLEAN[name])
        assert not tool_validate.validate_params(solar_tools.trusted_record(name), clean)
        inserts, entitlements_read, preparations = (
            len(admission.accepted), admission.events.count("entitlement"), len(calls))
        with pytest.raises(GraphValidationError, match="INVALID_SETTINGS_REQUEST"):
            submit(admission, params={**clean, "surprise": 1}, tool_name=name,
                   tool_manifest_sha256=manifest(name))
        assert len(admission.accepted) == inserts
        assert admission.events.count("entitlement") == entitlements_read
        assert len(calls) == preparations


def at(a, monkeypatch, ctx, tool):
    s = a.s
    monkeypatch.setattr(project, "verify_at_admission", lambda *args, **k:
        project.AdmissionContext(s.context(ctx.parent_version_id), s.lease))
    return {"input_version_id": ctx.parent_version_id, "tool_name": tool,
            "tool_manifest_sha256": manifest(tool)}


def check_admission_refusal(a, code, params, *, error=GraphValidationError, **kwargs):
    before = deepcopy(params)
    counts = len(a.accepted), a.events.count("insert"), a.events.count("entitlement")
    with pytest.raises(error, match=code):
        submit(a, params=params, **kwargs)
    assert (len(a.accepted), a.events.count("insert"), a.events.count("entitlement")) == counts
    assert params == before


def check_admitted(a, params, **kwargs):
    before = deepcopy(params)
    counts = len(a.accepted), a.events.count("insert"), a.events.count("entitlement")
    submit(a, params=params, **kwargs)
    assert (len(a.accepted), a.events.count("insert"), a.events.count("entitlement")) == tuple(
        n + 1 for n in counts)
    assert params == before
    return a.accepted[-1][4]


def test_sip_r4_nested_parameter_admission(admission, monkeypatch):
    a = admission
    params = seed(a.s).request["parameters"]
    params["changes"]["surprise"] = 1
    check_admission_refusal(a, "INVALID_SETTINGS_REQUEST", params)
    _, _, ctx5 = through_string(a.s)
    kw = at(a, monkeypatch, chain(a.s, 1)[-1][2], "solar-settings")
    params = {"expected_rev": 1, "changes": {
        "num_mppt": 2, "panel_layer_contains": "Panel", "surprise": 1}}
    check_admission_refusal(a, "INVALID_SETTINGS_REQUEST", params, **kw)
    kw = at(a, monkeypatch, ctx5, "solar-assign-equipment")
    for node, code in (("equipment", "INVALID_EQUIPMENT_CONFIGURATION"),
                       ("assignments", "INVALID_EQUIPMENT_ASSIGNMENT")):
        params = E()
        params[node][0]["surprise"] = 1
        check_admission_refusal(a, code, params, **kw)
    kw = at(a, monkeypatch, chain(a.s, 3)[-1][2], "solar-size-strings")
    check_admission_refusal(a, "INVALID_SIZING_REQUEST", {
        "expected_rev": 3, "mode": "manual-global", "confirm": True,
        "requests": {"surprise": 1}}, **kw)
    assert not a.accepted and a.events.count("entitlement") == a.events.count("insert") == 0


def test_sip_r4_request_value_parity(admission, monkeypatch):
    a = admission
    _, _, ctx5 = through_string(a.s)
    kw = at(a, monkeypatch, chain(a.s, 1)[-1][2], "solar-settings")
    settings = {"expected_rev": 1, "changes": {"num_mppt": 2, "panel_layer_contains": "Panel"}}
    params = deepcopy(settings)
    params["changes"]["num_mppt"] = "two"
    check_admission_refusal(a, "INVALID_GRAPH_SCHEMA", params, **kw)
    params = deepcopy(settings)
    params["changes"]["num_mppt"] = 2.0
    stored = check_admitted(a, params, **kw)
    assert stored["changes"]["num_mppt"] == 2.0
    assert type(stored["changes"]["num_mppt"]) is float
    check_admission_refusal(a, "STALE_GRAPH_REVISION", {**settings, "expected_rev": 7}, **kw)
    for injected in (False, True):
        params = deepcopy(settings)
        params["cancel"] = True
        if injected:
            params["changes"]["surprise"] = 1
        check_admission_refusal(a, "GRAPH_COMMIT_CANCELLED", params, **kw)
    kw = at(a, monkeypatch, ctx5, "solar-assign-equipment")
    for node, field, code in (("equipment", "number", "INVALID_EQUIPMENT_CONFIGURATION"),
                              ("assignments", "input_number", "INVALID_EQUIPMENT_ASSIGNMENT")):
        params = E()
        params[node][0][field] = True
        check_admission_refusal(a, code, params, **kw)
    params = E()
    params["cancel"] = True
    params["equipment"][0]["surprise"] = 1
    check_admission_refusal(a, "GRAPH_COMMIT_CANCELLED", params, **kw)
    params = E()
    params["assignments"][0]["input_number"] = 5
    assert check_admitted(a, params, **kw) == params
    kw = at(a, monkeypatch, chain(a.s, 3)[-1][2], "solar-size-strings")
    sizing = {"expected_rev": 3, "mode": "manual-global", "confirm": True}
    check_admission_refusal(a, "INVALID_SIZING_REQUEST", {**sizing, "requests": {}}, **kw)
    check_admission_refusal(a, "INVALID_SIZING_REQUEST", {
        "expected_rev": 3, "mode": "manual-global"}, **kw)
    check_admission_refusal(a, "SIP_R3_SERVICE_EVIDENCE_REQUIRED", {
        "expected_rev": 3, "mode": "global", "confirm": True,
        "requests": {"surprise": 1}, "grant_ref": "g1"},
        error=project.ProjectContextError, **kw)


def test_sip_r4_valid_admission_preparation(admission, monkeypatch):
    a, s = admission, admission.s
    check_admitted(a, seed(s).request["parameters"])
    _, _, ctx5 = through_string(s)
    kw = at(a, monkeypatch, chain(s, 1)[-1][2], "solar-settings")
    check_admitted(a, {"expected_rev": 1, "changes": {
        "num_mppt": 2, "panel_layer_contains": "Panel"}}, **kw)
    ctx = chain(s, 3)[-1][2]
    sizing = {"expected_rev": 3, "mode": "manual-global", "confirm": True}
    check_admitted(a, sizing, **at(a, monkeypatch, ctx, "solar-size-strings"))
    check_admitted(a, E(), **at(a, monkeypatch, ctx5, "solar-assign-equipment"))
    check_admitted(a, deepcopy(S), **at(a, monkeypatch, rev4(s), "solar-string-add"))
    assert len(a.accepted) == a.events.count("insert") == a.events.count("entitlement") == 5
    for real, zero in ((seed(s), seed(s, job_id=UUID(int=0))),
                       (prepare(s, "solar-size-strings", sizing, ctx),
                        prepare(s, "solar-size-strings", sizing, ctx, job_id=UUID(int=0)))):
        assert real.output_intake_bytes == zero.output_intake_bytes
        assert real.request["parameters"] == zero.request["parameters"]


def test_sip_r4_seed_parameter_admission(admission, monkeypatch):
    a = admission
    base = seed(a.s).request["parameters"]
    params = deepcopy(base)
    params["initialize"]["units"]["surprise"] = 1
    check_admission_refusal(a, "INVALID_SEED_REQUEST", params)
    params = deepcopy(base)
    params["changes"]["surprise"] = 1
    check_admission_refusal(a, "INVALID_SETTINGS_REQUEST", params)
    check_admission_refusal(a, "INVALID_SEED_REQUEST", deepcopy(base), tool_name="solar-feeders",
                            tool_manifest_sha256=manifest("solar-feeders"))
    params = deepcopy(base)
    params["initialize"]["source_intake_sha256"] = "0" * 64
    check_admission_refusal(a, "SOURCE_HASH_MISMATCH", params)
    params = deepcopy(base)
    del params["initialize"]
    check_admission_refusal(a, "GRAPH_NOT_EMBEDDED", params)
    ctx = chain(a.s, 1)[-1][2]
    params = {"expected_rev": 1, "changes": {"num_mppt": 2}, "initialize": {
        "schema_version": 1, "source_intake_sha256": ctx.intake_sha256, "units": deepcopy(UNITS)}}
    check_admission_refusal(a, "GRAPH_ALREADY_EMBEDDED", params,
                            **at(a, monkeypatch, ctx, "solar-settings"))
    assert not a.accepted and a.events.count("insert") == a.events.count("entitlement") == 0


def test_sip_r4_every_tool_prepared_at_admission(admission, monkeypatch):
    # No tool reaches the job table without the worker's own preparation: with schema-valid parameters each
    # of the nine refuses a graphless parent and a stale revision before the insert.
    a = admission
    for tool in sorted(NINE):
        check_admission_refusal(a, "GRAPH_NOT_EMBEDDED", deepcopy(CLEAN[tool]), tool_name=tool,
                                tool_manifest_sha256=manifest(tool))
    ctx = chain(a.s, 1)[-1][2]
    for tool in sorted(NINE):
        params = deepcopy(CLEAN[tool])
        params["expected_rev"] = 97
        check_admission_refusal(a, "STALE_GRAPH_REVISION", params, **at(a, monkeypatch, ctx, tool))
    assert not a.accepted and a.events.count("insert") == a.events.count("entitlement") == 0


PREREQUISITE_REFUSALS = {
    "solar-assign-equipment": "SIZING_CONFIRMATION_REQUIRED", "solar-combiners": "VALID_SETTINGS_REQUIRED",
    "solar-feeders": "VALID_SETTINGS_REQUIRED", "solar-homeruns": "EQUIPMENT_ASSIGNMENT_REQUIRED",
    "solar-schedule": "INVALID_ROUTE_POINT", "solar-size-strings": "MISSING_PANEL",
    "solar-string-add": "MISSING_PANEL",
}


def test_sip_r4_prerequisite_refusals_at_admission(admission, monkeypatch):
    # On a seeded parent at the right revision the tool's own preparation decides admission, not only the
    # context and revision checks: seven tools refuse with the worker's own code before the insert and the
    # entitlement read, and the two with no unmet prerequisite are admitted.
    a = admission
    ctx = chain(a.s, 1)[-1][2]
    for tool, code in sorted(PREREQUISITE_REFUSALS.items()):
        params = deepcopy(CLEAN[tool])
        params["expected_rev"] = 1
        check_admission_refusal(a, f"^{code}:", params, **at(a, monkeypatch, ctx, tool))
    assert not a.accepted and a.events.count("insert") == a.events.count("entitlement") == 0
    for tool in sorted(set(NINE) - set(PREREQUISITE_REFUSALS)):
        params = deepcopy(CLEAN[tool])
        params["expected_rev"] = 1
        check_admitted(a, params, **at(a, monkeypatch, ctx, tool))
    assert [row[3] for row in a.accepted] == ["solar-panels-from-drawing", "solar-settings"]



def test_sip_r4_validated_params_persisted(admission, monkeypatch):
    a, s = admission, admission.s
    values = []
    original = service._validate_new_project_graph_params
    def capture(*args, **kwargs):
        value = original(*args, **kwargs)
        values.append(deepcopy(value))
        return value
    monkeypatch.setattr(service, "_validate_new_project_graph_params", capture)
    params = {**seed(s).request["parameters"], "drawing_id": str(s.drawing)}
    stored = check_admitted(a, params)
    assert stored == values[-1] and "drawing_id" not in stored and "initialize" in stored
    ctx = chain(s, 1)[-1][2]
    kw = at(a, monkeypatch, ctx, "solar-settings")
    params = {"expected_rev": 1, "changes": {"num_mppt": 2}, "drawing_id": str(s.drawing)}
    stored = check_admitted(a, params, **kw)
    assert stored == values[-1] and "drawing_id" not in stored
    assert len(values) == 2
    def marked(*args, **kwargs):
        value = deepcopy(original(*args, **kwargs))
        value["changes"]["panel_layer_contains"] = "Marked"
        values.append(deepcopy(value))
        return value
    monkeypatch.setattr(service, "_validate_new_project_graph_params", marked)
    stored = check_admitted(a, params, **kw)
    assert stored == values[-1] and stored["changes"]["panel_layer_contains"] == "Marked"
    assert stored != {k: v for k, v in params.items() if k != "drawing_id"}
    assert a.accepted[-1][6]["submission_fingerprint"] == service._submission_fingerprint(
        s.org, s.project, s.drawing, ctx.parent_version_id, str(s.org), s.actor,
        "solar-settings", values[-1], manifest("solar-settings"))


def test_sip_r4_legacy_nested_replay(admission, monkeypatch):
    a, s = admission, admission.s
    params = seed(s).request["parameters"]
    params["changes"]["surprise"] = 1
    fingerprint = service._submission_fingerprint(s.org, s.project, s.drawing,
        s.parent.version_id, str(s.org), s.actor, "solar-settings", params, manifest("solar-settings"))
    accepted = {"job_id": str(s.job), "params": deepcopy(params),
        "execution_context": {"schema": jobs.PROJECT_GRAPH_JOB_SCHEMA},
        "submission_fingerprint": fingerprint, "deleted_at": None}
    snapshot = deepcopy(accepted)
    monkeypatch.setattr(jobs, "get_project_graph_job_by_key", lambda *a, **k: accepted)
    monkeypatch.setattr(service, "_validate_new_project_graph_params", forbidden)
    monkeypatch.setattr(project, "verify_at_admission", forbidden)
    monkeypatch.setattr(entitlements, "stored_job_entitlement_verdict", forbidden)
    monkeypatch.setattr(jobs, "submit_project_graph_job", forbidden)
    before = deepcopy(params)
    assert submit(a, params=params) is accepted
    assert accepted == snapshot and params == before
    changed = deepcopy(params)
    changed["changes"]["surprise"] = 2
    check_admission_refusal(a, "SIP_R4_IDEMPOTENCY_CONFLICT", changed,
                            error=project.ProjectContextError)
    assert accepted == snapshot and not a.accepted
    assert a.events.count("insert") == a.events.count("entitlement") == 0


def test_sip_r4_admission_preparation_order(admission, monkeypatch):
    a = admission
    original = service._validate_new_project_graph_params
    def spy(*args, **kwargs):
        a.events.append("prepare")
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "_validate_new_project_graph_params", spy)
    submit(a)
    events = [x if isinstance(x, str) else x[0] for x in a.events]
    assert events.count("prepare") == 1
    assert events.index("admission") < events.index("prepare") < events.index("entitlement")
    assert events.index("entitlement") < events.index("insert")
    base = seed(a.s).request["parameters"]
    for code, params, kw in (
        ("INVALID_SETTINGS_REQUEST", {**base, "surprise": 1}, {}),
        ("INVALID_SEED_REQUEST", deepcopy(base), {"tool_name": "solar-feeders",
                                                  "tool_manifest_sha256": manifest("solar-feeders")})):
        start = len(a.events)
        check_admission_refusal(a, code, params, **kw)
        assert "prepare" not in a.events[start:]
    params = deepcopy(base)
    params["changes"]["surprise"] = 1
    start = len(a.events)
    check_admission_refusal(a, "INVALID_SETTINGS_REQUEST", params)
    events = a.events[start:]
    assert events.count("prepare") == 1 and "entitlement" not in events and "insert" not in events


def test_sip_r4_admission_preparation_isolation(admission, monkeypatch):
    a, s = admission, admission.s
    contexts = []
    original = service._validate_new_project_graph_params
    def capture(context, *args, **kwargs):
        parent = context.parent_version_id
        contexts.append((context, parent))
        result = original(context, *args, **kwargs)
        assert context.parent_version_id == parent
        return result
    monkeypatch.setattr(service, "_validate_new_project_graph_params", capture)
    params = seed(s).request["parameters"]
    writes = deepcopy(s.writes)
    with monkeypatch.context() as patch:
        patch.setattr(graph, "publish_project_graph_commit", forbidden)
        check_admitted(a, params)
    assert s.writes == writes and len(contexts) == 1
    ctx = chain(s, 1)[-1][2]
    kw = at(a, monkeypatch, ctx, "solar-settings")
    params = {"expected_rev": 1, "changes": {"num_mppt": 2, "panel_layer_contains": "Panel"}}
    writes = deepcopy(s.writes)
    with monkeypatch.context() as patch:
        patch.setattr(graph, "publish_project_graph_commit", forbidden)
        check_admitted(a, params, **kw)
    assert s.writes == writes and len(contexts) == 2
    assert all(context.parent_version_id == parent for context, parent in contexts)
    assert [parent for _, parent in contexts] == [s.parent.version_id, ctx.parent_version_id]
    assert len(a.accepted) == a.events.count("insert") == a.events.count("entitlement") == 2


def test_sip_r4_preparation_binding(memory, monkeypatch):
    monkeypatch.setattr(jobs, "_graph_scope", lambda row, conn: None)
    p = seed(memory)
    assert service.prepare_project_graph_job(record(memory, p), conn=memory) == p
    rows = []
    ctx = rev4(memory)
    sp, _, ctx5 = commit(memory, "solar-string-add", S, ctx)
    ep = graph.prepare_project_graph_commit(ctx5, "solar-assign-equipment", E(),
        checkout=memory.lease, job_id=memory.job, attempt=1,
        tool_manifest_sha256=manifest("solar-assign-equipment"))
    rows += [(sp, record(memory, sp)), (ep, record(memory, ep))]
    for prepared, row in rows:
        rebuilt = service.prepare_project_graph_job(row, conn=memory)
        assert rebuilt == prepared
        row["attempt"] = 2
        retry = service.prepare_project_graph_job(row, conn=memory)
        assert retry.output_intake_bytes == prepared.output_intake_bytes
        assert retry.request_sha256 != prepared.request_sha256
        assert retry.request["parent_version_id"] == prepared.request["parent_version_id"]


def test_sip_r4_background_proof(memory, monkeypatch):
    p = seed(memory)
    r = publish(memory, p)
    row = record(memory, p)
    conn = Connection(memory, row)
    scopes = []
    monkeypatch.setattr(jobs, "_graph_scope", lambda row, conn: scopes.append(row["execution_context"]["actor_binding_id"]))
    monkeypatch.setattr(project, "_access", forbidden)
    assert graph.project_graph_job_provenance(memory.job, r, expected=p, conn=conn)["output_version_id"] == r["output_version_id"]
    assert scopes == [str(memory.actor)]
    def revoked(*args):
        project.refuse("PROJECT_FORBIDDEN")
    monkeypatch.setattr(jobs, "_graph_scope", revoked)
    refused("SIP_R1_PROJECT_FORBIDDEN", lambda:
        graph.project_graph_job_provenance(memory.job, r, expected=p, conn=conn))
    with pytest.raises(AssertionError):
        graph.project_graph_commit_provenance(str(memory.org), r, expected=p, conn=conn)


def test_sip_r4_proof_binding(memory, monkeypatch):
    p = seed(memory)
    row = record(memory, p)
    for key in ("schema", "organization_id", "project_id", "drawing_id", "parent_version_id",
                "actor_binding_id", "checkout_fence", "job_id", "attempt", "tool",
                "tool_manifest_sha256", "parameters", "parent_intake_sha256", "initialized"):
        r = p.request
        r[key] = (2 if key == "attempt" else False if key == "initialized"
                  else {} if key == "parameters" else "altered")
        refused("SIP_R4_JOB_BINDING_MISMATCH", lambda: jobs._validate_graph_request(row, r))
    receipt = publish(memory, p)
    conn = Connection(memory, row)
    monkeypatch.setattr(jobs, "_graph_scope", lambda *a: None)
    version = memory.versions[UUID(receipt["output_version_id"])]
    raw = memory.blobs[version.intake_ref]
    memory.blobs[version.intake_ref] = raw + b" "
    refused("SIP_R1_INTAKE_DIGEST_MISMATCH", lambda:
        graph.project_graph_job_provenance(memory.job, receipt, expected=p, conn=conn))


def test_sip_r4_claim_filter(monkeypatch):
    calls = []
    fake = SimpleNamespace(claim_project_graph_job=lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(service, "_jobs", lambda: fake)
    assert service.run_once("owner", tool_name="solar-string-add") is False
    assert calls == [(("owner",), {"tool_name": "solar-string-add", "lease_seconds": 30.0})]
    source = inspect.getsource(jobs.claim_project_graph_job) + inspect.getsource(jobs._graph_population)
    for token in ("SKIP LOCKED", "j.kind='run'", "j.tool_name=%(tool)s",
                  "j.execution_context->>'schema'=%(schema)s", "clock_timestamp()"):
        assert token in source


def test_sip_r4_heartbeat_attempt():
    calls, renewed = [], Event()
    def heartbeat(*a, **k):
        calls.append((a, k))
        renewed.set()
        return True
    guard = service.GraphLeaseGuard(SimpleNamespace(heartbeat_project_graph_job=heartbeat),
                                   UUID(int=8), "owner", 2, 0.15)
    guard.start()
    assert renewed.wait(2)
    guard.close()
    assert calls and all(a == (UUID(int=8), "owner", 2) and k == {"lease_seconds": 0.15} for a, k in calls)
    assert not guard.lost


def worker_double(monkeypatch, *, prepare=None, complete=None, guard=None):
    failed, completed = [], []
    job = {"job_id": str(UUID(int=8)), "attempt": 2}
    fake = SimpleNamespace(claim_project_graph_job=lambda *a, **k: job,
        fail_project_graph_job=lambda *a: failed.append(a))
    monkeypatch.setattr(service, "_jobs", lambda: fake)
    monkeypatch.setattr(platform_link.platform_db(), "run_transaction", lambda op: op(None))
    monkeypatch.setattr(service, "prepare_project_graph_job", prepare or (lambda *a, **k: object()))
    monkeypatch.setattr(service, "complete_project_graph_job", complete or (lambda *a: completed.append(a)))
    if guard is not None:
        monkeypatch.setattr(service, "GraphLeaseGuard", guard)
    return failed, completed


def test_sip_r4_lost_lease(monkeypatch):
    for outcome in (False, ConnectionError("private")):
        renewed = Event()
        def heartbeat(*a, **k):
            renewed.set()
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        failed, completed = worker_double(monkeypatch)
        fake = service._jobs()
        fake.heartbeat_project_graph_job = heartbeat
        def prepare(*a, **k):
            assert renewed.wait(2)
            # Synchronize with the guard's lost flag by stopping/joining in run_once.
            return object()
        monkeypatch.setattr(service, "prepare_project_graph_job", prepare)
        assert service.run_once("owner", tool_name="solar-settings", lease_seconds=0.15)
        assert not failed and not completed
    guard = service.GraphLeaseGuard(SimpleNamespace(), UUID(int=8), "owner", 2, 0.15)
    guard._thread = SimpleNamespace(join=lambda **k: None, is_alive=lambda: True)
    guard.close()
    assert guard.lost
    class Unfinished:
        lost = False
        def __init__(self, *a): pass
        def start(self): pass
        def close(self): self.lost = True
    failed, completed = worker_double(monkeypatch, guard=Unfinished)
    assert service.run_once("owner", tool_name="solar-settings") and not failed and not completed


def test_sip_r4_failure_policy(monkeypatch):
    def blob_refusal(outcome):
        # The refusal the stored-intake reader itself raises for one blob read outcome.
        class Backend:
            def get(self, key):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        with monkeypatch.context() as patch:
            patch.setattr(project.write_loop, "upload_backend_for_tenant", lambda org: Backend())
            with pytest.raises(project.ProjectContextError) as caught:
                project._load_intake(UUID(int=1), "tenants/x/intake.json", "0" * 64)
        assert service._preparation_error(caught.value)["reason_code"] == "SIP_R1_INTAKE_UNAVAILABLE"
        return caught.value

    for exc, retry in ((TimeoutError("secret"), True), (ConnectionError("secret"), True),
        (project.ProjectContextError("SIP_R1_STORE_UNAVAILABLE"), True),
        (project.ProjectContextError("SIP_R1_CHECKOUT_UNAVAILABLE"), True),
        (project.ProjectContextError("SIP_R1_CHECKOUT_STALE"), False),
        (GraphValidationError("INVALID_SEED_REQUEST"), False), (RuntimeError("secret"), False),
        # A blob read that timed out or lost its connection is retried; a missing object, a
        # value that is not bytes and a bare refusal with no transport failure behind it are not.
        (blob_refusal(TimeoutError("secret")), True),
        (blob_refusal(ConnectionError("secret")), True),
        (blob_refusal(KeyError("secret")), False), (blob_refusal(None), False),
        (project.ProjectContextError("SIP_R1_INTAKE_UNAVAILABLE"), False)):
        def prepare(*a, **k):
            raise exc
        failed, completed = worker_double(monkeypatch, prepare=prepare)
        assert service.run_once("owner", tool_name="solar-settings")
        assert not completed and len(failed) == 1
        error = failed[0][3]
        assert error["retryable"] is retry and error["error_code"] == "GRAPH_JOB_FAILED"
        assert error["message"] == "canonical Solar graph preparation failed"
        assert "secret" not in json.dumps(error)
        assert failed[0][:3] == (UUID(int=8), "owner", 2)


def test_sip_r4_persistence_failure(monkeypatch):
    def fail(*a):
        raise ConnectionError("uncertain commit")
    failed, _ = worker_double(monkeypatch, complete=fail)
    assert service.run_once("owner", tool_name="solar-settings")
    assert failed == []


def test_sip_r4_solver_isolation(monkeypatch):
    from test_canonical_worker import FakeCanonicalJobs
    for tool in (worker.autofill.TOOL_NAME, worker.arlo_design.TOOL_NAME):
        fake = FakeCanonicalJobs({"job_id": "solver", "attempt": 1, "tool_name": tool, "params": {"x": 1}})
        monkeypatch.setattr(platform_link, "_canonical_jobs_module", lambda: fake)
        module = worker.autofill if tool == worker.autofill.TOOL_NAME else worker.arlo_design
        monkeypatch.setattr(module, "descriptor", lambda: {"tool_name": tool, "runtime": "test",
            "source_revision": "rev", "source_sha256": "a" * 64})
        calls = []
        def adapter(*a, **k):
            calls.append((a, k))
            return {"solver_revision": "rev", "source_sha256": "a" * 64, "runtime": "test"}
        monkeypatch.setitem(worker.ADAPTERS, tool, adapter)
        assert worker.run_once("owner", tool_name=tool) and len(fake.completed) == 1 and not fake.failed
        assert calls[0][0] == ({"x": 1},)
        assert set(calls[0][1]) == ({"job_context", "cancelled"} if tool == worker.arlo_design.TOOL_NAME else set())
        def failure(*a, **k): raise TimeoutError()
        fake.job = {"job_id": "solver", "attempt": 1, "tool_name": tool, "params": {}}
        monkeypatch.setitem(worker.ADAPTERS, tool, failure)
        assert worker.run_once("owner", tool_name=tool) and fake.failed[-1][2]["retryable"] is True


def test_sip_r4_result_projection(memory):
    receipt = publish(memory, seed(memory))
    success = {"graph_commit": receipt, "output_version_id": receipt["output_version_id"],
               "history_operation_id": str(UUID(int=88)), "history_hash": "a"*64}
    projected = platform_link._canonical_record({"status": "succeeded", "request_tenant_id": str(memory.org),
        "result": success, "output_version_id": receipt["output_version_id"], "lease_owner": None})
    assert projected["status"] == "complete" and projected["progress"] == "done"
    assert projected["elapsed_ms"] is None and projected["lease"] is None
    assert projected["output_version_id"] == receipt["output_version_id"]
    assert projected["result"]["graph_commit"] == receipt and "history_hash" not in receipt


def test_sip_r4_no_http_activation(monkeypatch):
    monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
    from routers import jobs as router
    root = Path(graph.__file__).parent
    monkeypatch.setattr(platform_link, "submit_canonical_graph", forbidden)
    monkeypatch.setattr(service, "submit_project_graph_job", forbidden)
    monkeypatch.setattr(router.jobs, "submit_job", forbidden)
    monkeypatch.setattr(router.jobs.platform_link, "resolve_submission_context", lambda *a:
        {"org_id": UUID(int=1), "project_id": UUID(int=2), "authority_mode": "postgres_canonical"})
    monkeypatch.setattr(router.entitlements, "w1_tool_availability", lambda *a, **k: None)
    monkeypatch.setattr(router.deps, "backedge_run_identity", lambda tenant, *a: tenant)
    monkeypatch.setattr(router.entitlements, "resolve_tier", lambda *a: "demo")
    monkeypatch.setattr(router, "_checkout_identity", lambda *a: ("Editor", 1))
    for name in NINE:
        tool = solar_tools.trusted_record(name)
        monkeypatch.setattr(router.deps, "find_tool", lambda *a: tool)
        response = router.run(router.RunRequest(tool=name, params={"expected_rev": 0},
            dwg=str(UUID(int=3)), dwg_version=1, catalog_digest=router.deps.catalog_tool_digest(tool)),
            wait=0, tenant_id="demo", x_org_id=str(UUID(int=1)), x_project_id=str(UUID(int=2)),
            authorization=None, idempotency_key="key")
        assert response.status_code == 409
        body = json.loads(response.body)
        assert body["ok"] is False and body["error"]["error_code"] == "BAD_PARAMS"
        assert "project-scoped canonical execution" in body["error"]["message"]
    for path in (root / "routers").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "submit_canonical_graph" not in source and "submit_project_graph_job" not in source
    for path in root.rglob("*broker*.py"):
        assert "submit_canonical_graph" not in path.read_text(encoding="utf-8")
    for name in NINE:
        assert "canonical_only" not in solar_tools.trusted_record(name)


def test_sip_r4_submission_replay_order(admission, monkeypatch):
    s = admission.s
    params = seed(s).request["parameters"]
    fingerprint = service._submission_fingerprint(s.org, s.project, s.drawing,
        s.parent.version_id, str(s.org), s.actor, "solar-settings", params, manifest("solar-settings"))
    accepted = {"job_id": str(s.job), "execution_context": {"schema": jobs.PROJECT_GRAPH_JOB_SCHEMA},
                "submission_fingerprint": fingerprint, "deleted_at": None}
    monkeypatch.setattr(jobs, "get_project_graph_job_by_key", lambda *a, **k: accepted)
    monkeypatch.setattr(project, "verify_at_admission", forbidden)
    monkeypatch.setattr(entitlements, "stored_job_entitlement_verdict", forbidden)
    monkeypatch.setattr(graph, "_tool", forbidden)
    monkeypatch.setattr(graph.local, "_load_builtin", forbidden)
    monkeypatch.setattr(s, "get", forbidden)
    assert submit(admission, params=params) is accepted
    refused("SIP_R4_IDEMPOTENCY_CONFLICT", lambda: submit(admission, params={**params, "cancel": False}))
    accepted["deleted_at"] = "tombstone"
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: submit(admission, params=params))


def test_sip_r4_terminal_evidence(memory, monkeypatch):
    p = seed(memory)
    row = record(memory, p)
    monkeypatch.setattr(jobs, "_graph_scope", lambda *a: None)
    platform_complete = jobs.complete_project_graph_job
    calls = []
    def complete(job, owner, attempt, completion, **kwargs):
        calls.append((completion, kwargs))
        return "duplicate", {"saved": True}
    monkeypatch.setattr(jobs, "complete_project_graph_job", complete)
    monkeypatch.setattr(platform_link.platform_db(), "run_transaction", lambda op: op(memory))
    assert service.complete_project_graph_job(memory.job, "owner", 1, p) == ("duplicate", {"saved": True})
    evidence = calls[0][0]
    assert evidence == {"request": p.request, "output_intake_sha256": p.output_intake_sha256,
                        "receipt": json.loads(p.receipt_bytes)}
    source = inspect.getsource(platform_complete)
    for text in ("canonical-project-graph-success", "solar.graph.completed",
                 "history.operation.appended", "lease_expires_at>clock_timestamp()"):
        assert text in source
    refused("SIP_R4_JOB_BINDING_MISMATCH", lambda:
        service.complete_project_graph_job(memory.job, "owner", 1, {}))
    row.update(status="succeeded", terminal_fingerprint=jobs._fingerprint("canonical-project-graph-success", evidence),
               result={"saved": True})
    conn = Connection(memory, row)
    assert platform_complete(memory.job, "other", 1, evidence,
        publish_and_prove=forbidden, conn=conn) == ("duplicate", {"saved": True})
    assert platform_complete(memory.job, "owner", 2, evidence,
        publish_and_prove=forbidden, conn=conn) == ("conflict", None)
    assert platform_complete(memory.job, "owner", 1, {**evidence, "extra": True},
        publish_and_prove=forbidden, conn=conn) == ("conflict", None)
    row.update(status="running", lease_expires_at=memory.lease.acquired_at - timedelta(seconds=1))
    monkeypatch.setattr(project.graph_store(), "_clock", lambda cur: memory.lease.acquired_at)
    assert platform_complete(memory.job, "owner", 1, {},
        publish_and_prove=forbidden, conn=conn) == ("not_owner", None)
    row["lease_expires_at"] = memory.lease.expires_at
    for malformed in ({}, {**evidence, "extra": True},
                      {**evidence, "output_intake_sha256": "bad"},
                      {**evidence, "receipt": {"parameters": {}}}):
        refused("SIP_R4_JOB_BINDING_MISMATCH", lambda:
            platform_complete(memory.job, "owner", 1, malformed,
                              publish_and_prove=forbidden, conn=conn))
