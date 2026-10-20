"""R14-C reconstruction and consumer custody, without live credentials or HTTP."""
from copy import deepcopy
from dataclasses import replace
import json
import types
from uuid import UUID, uuid4

import pytest
import deps
import solar_project_context as project
import solar_project_graph as graph
import solar_project_jobs as service
import solar_project_read as read
import solar_project_sizing as sizing
import solar_sizing_client as cloud
import solar_tools
from solar_design_graph import GraphValidationError
import test_sip_r3a_graph as a3
import test_sip_r3b_tools as b3
import test_sip_r14_admission as admission
from test_sip_r14_worker import World, store


def completed(monkeypatch):
    w = World(monkeypatch)
    w.run()
    result = w.completed()
    prepared = service.prepare_project_graph_job(w.row, conn=w)
    return w, prepared, result


def reject(operation):
    with pytest.raises((GraphValidationError, project.ProjectContextError)):
        operation()


def context(w, value):
    intake = b3.embedded(value)
    raw = graph.local.canonical_bytes(intake)
    return replace(w.context, intake_bytes=raw, intake_sha256=graph._sha(raw))


def output(w, prepared):
    return graph.resolve_project_graph_context(w.s.context(UUID(w.completed()["output_version_id"])))


def historical(w, p, r):
    return graph.project_graph_commit_provenance(str(w.s.org), r, expected=p, conn=w)


def test_R14C_21_no_network_reconstruction(monkeypatch):
    """R14C-21 Publication and both proof entrypoints reconstruct stored bytes only."""
    w = World(monkeypatch)
    w.capture()
    monkeypatch.setattr(cloud, "resolve_grant", admission.forbidden)
    monkeypatch.setattr(cloud, "post_string_length", admission.forbidden)
    monkeypatch.setattr(cloud.requests, "post", admission.forbidden)
    w.run()
    r = w.completed()
    p = service.prepare_project_graph_job(w.row, conn=w)
    assert historical(w, p, r)["output_version_id"] == r["output_version_id"]
    assert graph.project_graph_job_provenance(w.job_id, r, expected=p, conn=w) == historical(w, p, r)
    assert graph.publish_project_graph_commit(p, actor_binding_id=w.s.actor, conn=w) == r
    ref = {"job_id": str(w.job_id), "evidence_sha256": w.exchange["evidence_sha256"]}
    assert p.request["sizing_exchange"] == r["sizing_exchange"] == ref
    assert output(w, p).graph["settings"]["extra"]["string_sizing"]["project_exchange"] == ref


def test_R14C_22_authoritative_exchange_bytes(monkeypatch):
    """R14C-22 Independently rehashed changed request, response and evidence bytes still refuse."""
    w, p, r = completed(monkeypatch)
    for field in ("request", "response", "evidence"):
        old = deepcopy(w.exchange)
        value = json.loads(w.exchange[field + "_bytes"])
        if field == "request":
            value["module_name"] = "other"
        elif field == "response":
            value["pmp"] = 401
        else:
            value["derived"]["module_power_watts"] = 401.0
        raw = graph.local.canonical_bytes(value)
        w.exchange[field + "_bytes"] = raw
        w.exchange[field + "_sha256"] = graph._sha(raw)
        reject(lambda: graph._stored_sizing_graph(w.context, w.job_id, w))
        reject(lambda: historical(w, p, r))
        w.exchanges[str(w.job_id)] = old


def test_R14C_23_foreign_exchange(monkeypatch):
    """R14C-23 Valid foreign identity evidence cannot supply its own expected identity."""
    w, p, r = completed(monkeypatch)
    for field in ("job_id", "drawing_id", "project_id", "organization_id", "actor_id"):
        old = deepcopy(w.exchange)
        expected = graph._sizing_expected(w.row, w.context, w.exchange)
        expected["identity"][field] = str(uuid4())
        record = sizing.build_sizing_evidence(w.params, w.context.graph, w.context.intake_sha256,
            expected["tool_manifest_sha256"], w.raw, identity=expected["identity"],
            parent_version_id=expected["parent_version_id"], producer=expected["producer"], classification="live")
        raw = graph.local.canonical_bytes(record)
        w.exchange.update(evidence_bytes=raw, evidence_sha256=graph._sha(raw))
        reject(lambda: graph._stored_sizing_graph(w.context, w.job_id, w))
        w.exchanges[str(w.job_id)] = old


def test_R14C_24_exact_output(monkeypatch):
    """R14C-24 Changed companion bytes or electrical values fail independent reconstruction."""
    w, p, r = completed(monkeypatch)
    version = w.s.versions[UUID(r["output_version_id"])]
    for electrical in (False, True):
        intake = json.loads(p.output_intake_bytes)
        if electrical:
            intake["solar_design_graph"]["settings"]["panels_in_sequence"] += 1
            intake["solar_design_graph_sha256"] = graph.local.digest(intake["solar_design_graph"])
        else:
            intake["custom"] = {"unrequested": True}
        raw = graph.local.canonical_bytes(intake)
        altered = replace(output(w, p), intake_bytes=raw, intake_sha256=graph._sha(raw))
        stored = graph._stored_context
        with monkeypatch.context() as patch:
            patch.setattr(graph, "_stored_context", lambda request, vid, conn:
                (altered, version) if vid == version.version_id else stored(request, vid, conn))
            reject(lambda: historical(w, p, r))
        forged = replace(p, output_intake_bytes=raw)
        # New publication runs its byte callback; remove the existing idempotent result temporarily.
        del w.s.versions[version.version_id]
        reject(lambda: graph.publish_project_graph_commit(forged, actor_binding_id=w.s.actor, conn=w))
        w.s.versions[version.version_id] = version


def test_R14C_25_pending_terminal(monkeypatch):
    """R14C-25 Private completion proves pending custody; historical proof requires terminal binding."""
    w = World(monkeypatch)
    original = graph._pending_project_graph_job_provenance
    observed = []
    def pending(job_id, result, **kwargs):
        assert w.row["status"] == "running" and w.exchange["state"] == "response_stored"
        reject(lambda: historical(w, kwargs["expected"], result))
        reject(lambda: graph.project_graph_job_provenance(job_id, result, **kwargs))
        observed.append(original(job_id, result, **kwargs))
        return observed[-1]
    monkeypatch.setattr(graph, "_pending_project_graph_job_provenance", pending)
    w.run()
    r = w.completed()
    assert len(observed) == 1
    p = service.prepare_project_graph_job(w.row, conn=w)
    for field, value in (("output_version_id", uuid4()), ("published_attempt", 9),
                          ("published_worker", "w-" + uuid4().hex)):
        old = w.exchange[field]
        w.exchange[field] = value
        reject(lambda: historical(w, p, r))
        w.exchange[field] = old


def test_R14C_26_synthetic_product_refusal(monkeypatch):
    """R14C-26 Synthetic custody refuses in product; private test dependency substitution is explicit."""
    w, p, r = completed(monkeypatch)
    record = json.loads(w.exchange["evidence_bytes"])
    record["classification"] = "synthetic"
    raw = graph.local.canonical_bytes(record)
    w.exchange.update(classification="synthetic", evidence_bytes=raw, evidence_sha256=graph._sha(raw))
    with pytest.raises(GraphValidationError) as caught:
        graph._stored_sizing_graph(w.context, w.job_id, w)
    assert caught.value.code == "SIP_R14_SYNTHETIC_EVIDENCE_REFUSED"
    prove = sizing.prove_sizing_evidence
    monkeypatch.setattr(sizing, "prove_sizing_evidence", lambda *args, **kwargs:
        prove(*args, **{**kwargs, "allow_synthetic_for_test": True}))
    assert graph._stored_sizing_graph(w.context, w.job_id, w)["settings"]["panels_in_sequence"] == 10


def test_R14C_27_historical_credentials(monkeypatch):
    """R14C-27 Completed proof uses current read authority but no live grant or checkout."""
    w, p, r = completed(monkeypatch)
    monkeypatch.setattr(cloud, "resolve_grant", admission.forbidden)
    monkeypatch.setattr(cloud, "post_string_length", admission.forbidden)
    monkeypatch.setattr(project, "verify_at_admission", admission.forbidden)
    checked = []
    monkeypatch.setattr(project, "_access", lambda tenant, proj, write:
        checked.append((tenant, proj, write)) or (w.s.org, w.s.actor))
    assert historical(w, p, r)["output_version_id"] == r["output_version_id"]
    assert checked == [(str(w.s.org), w.s.project, False)]
    reject(lambda: graph.project_graph_commit_provenance(str(uuid4()), r, expected=p, conn=w))


def test_R14C_28_standalone_substitution(monkeypatch):
    """R14C-28 Canonical mutations reject standalone cloud sizing without project custody."""
    w, p, r = completed(monkeypatch)
    value = output(w, p).graph
    del value["settings"]["extra"]["string_sizing"]["project_exchange"]
    ctx = context(w, value)
    with pytest.raises(project.ProjectContextError) as caught:
        b3.prepare(w.s, "solar-string-conductors", {"expected_rev": value["rev"],
            "operation": "set-conductors", "assignments": []}, context=ctx)
    assert caught.value.reason_code == "SIP_R3_SERVICE_EVIDENCE_REQUIRED"


def test_R14C_29_downstream_reuse(monkeypatch):
    """R14C-29 Later graph revisions reuse proved sizing; basis, records and electrical tampering refuse."""
    w, p, r = completed(monkeypatch)
    value = output(w, p).graph
    value["parent_rev"], value["rev"] = value["rev"], value["rev"] + 1
    graph.require_project_sizing(context(w, value), conn=w)
    for change in ("basis", "records", "voltage", "length", "power", "foreign"):
        changed = deepcopy(value)
        evidence = changed["settings"]["extra"]["string_sizing"]
        if change == "basis":
            changed["panels"][0]["centre"][0] += 1
        elif change == "records":
            next(iter(evidence["records"].values()))["tenant_id"] = str(uuid4())
        elif change == "voltage":
            changed["settings"]["voc_cold"]["per_module"] += 1
        elif change == "length":
            changed["settings"]["panels_in_sequence"] += 1
        elif change == "power":
            changed["frames"][0]["module_power_watts"] += 1
        else:
            w.row["project_id"] = str(uuid4())
        reject(lambda: graph.require_project_sizing(context(w, changed), conn=w))
        w.row["project_id"] = str(w.s.project)


def test_R14C_30_compatibility_boundary(monkeypatch):
    """R14C-30 Manual unknown power, standalone repeated sends and direct cloud refusal persist."""
    w = World(monkeypatch)
    params = {"expected_rev": 0, "mode": "manual-global", "confirm": True}
    p = b3.prepare(w.s, graph.SIZING_TOOL, params, w.context)
    result = json.loads(p.output_intake_bytes)["solar_design_graph"]
    expected = graph._run_builtin(w.context, graph.SIZING_TOOL, w.context.graph, params, {}, w.job_id)
    assert graph.local.canonical_bytes(result) == graph.local.canonical_bytes(expected)
    assert result["settings"]["extra"]["string_sizing"]["records"] == {}
    assert result["settings"]["voc_cold"] == cloud.manual_voltage()
    from solar_sizing_power import frame_module_power
    for frame in result["frames"]:
        with pytest.raises(GraphValidationError, match="MODULE_POWER_REQUIRED"):
            frame_module_power(result, frame["panel_refs"], frame["electrical_zone_ref"])
    with pytest.raises(project.ProjectContextError) as caught:
        b3.prepare(w.s, graph.SIZING_TOOL, w.params, w.context)
    assert caught.value.reason_code == "SIP_R3_SERVICE_EVIDENCE_REQUIRED"
    calls = []
    monkeypatch.setattr(cloud, "post_string_length", lambda *args, **kwargs: calls.append(args) or w.raw)
    standalone = {"grant_ref": "g1", "request": next(iter(w.params["requests"].values()))}
    for _ in range(2):
        assert cloud.size(standalone, str(w.s.org), str(uuid4()))["sizing"]["panels_in_sequence"] == 10
    assert len(calls) == 2


def read_setup(w, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setattr(deps, "find_tool", lambda name, tenant: solar_tools.trusted_record(name))
    monkeypatch.setattr(read.entitlements, "resolve_tier", lambda tenant: "hosted_pro")
    monkeypatch.setattr(read.entitlements, "resolve_roles", lambda tenant: ((), False))
    monkeypatch.setattr(read.entitlements, "entitlements_for", lambda *args: {"run_read": True})
    monkeypatch.setattr(read.stored, "stored_job_entitlement_verdict", lambda *args: (None, None))
    monkeypatch.setattr(project, "resolve_context", lambda tenant, proj, vid, **kw: w.s.context(vid))


def test_R14C_31_read_custody(monkeypatch):
    """R14C-31 Read consumers refuse imported cloud evidence before creating any output."""
    w, p, r = completed(monkeypatch)
    read_setup(w, monkeypatch)
    tool = "solar-nec-ampacity-correction"
    params = dict(base_ampacity=100, temp_factor=.91, conduit_factor=.8)
    def run(version):
        return read.run_project_read(str(w.s.org), w.s.project, w.s.drawing, version,
            tool, params, catalog_digest=b3.manifest(tool))
    assert run(UUID(r["output_version_id"]))["result"]["output"]["result"] == 72.8
    value = output(w, p).graph
    del value["settings"]["extra"]["string_sizing"]["project_exchange"]
    w.s.set_parent(b3.embedded(value))
    monkeypatch.setattr(read.local, "_read_output", admission.forbidden)
    before = list(w.s.writes)
    with pytest.raises(project.ProjectContextError) as caught:
        run(w.s.parent.version_id)
    assert caught.value.reason_code == "SIP_R3_SERVICE_EVIDENCE_REQUIRED" and w.s.writes == before

@pytest.mark.parametrize("error_type, expected", (
    (store.SizingExchangeError, "SIP_R3_PROOF_REJECTED"),
    (store.ExchangeInputError, "SIP_R3_PROOF_REJECTED"),
    (store.ExchangeNotFound, "SIP_R3_PROOF_REJECTED"),
    (store.ExchangeConflict, "SIP_R3_PROOF_REJECTED"),
    (store.ExchangeLeaseLost, "SIP_R3_PROOF_REJECTED"),
    (store.ExchangeStateError, "SIP_R3_PROOF_REJECTED"),
    (project.ProjectContextError, "SIP_R3_SERVICE_EVIDENCE_REQUIRED"),
))
def test_R14C_36_proof_sanitizes_exchange_errors(monkeypatch, error_type, expected):
    """R14C-36 Exchange details are sanitized; other project refusals propagate."""
    w, p, r = completed(monkeypatch)
    ctx = output(w, p)
    detail = "private exchange custody detail"
    if error_type is project.ProjectContextError:
        error = error_type(expected)
    else:
        # Exchange subclasses have fixed no-argument constructors; inject private detail
        # through their shared base initializer while preserving the concrete type.
        error = error_type(detail) if error_type is store.SizingExchangeError else error_type()
        project.ProjectContextError.__init__(error, detail)
    def refuse(*args, **kwargs):
        raise error
    monkeypatch.setattr(graph, "_stored_job", refuse)
    with pytest.raises(project.ProjectContextError) as caught:
        graph.require_project_sizing(ctx, conn=w)
    assert caught.value.reason_code == expected
    assert caught.value.args == (expected,)
    assert detail not in str(caught.value)
    if error_type is project.ProjectContextError:
        assert caught.value is error



class _BrokenImport:
    def __call__(self):
        raise ImportError("private exchange import detail")


@pytest.mark.parametrize("lookup", (
    _BrokenImport(),
    lambda: types.SimpleNamespace(),
    lambda: types.SimpleNamespace(SizingExchangeError=ValueError),
), ids=("import-fails", "family-missing", "family-wrong"))
def test_R14C_38_classification_needs_no_lookup(monkeypatch, lookup):
    """R14C-38 A broken exchange lookup neither raises nor decides the refusal class."""
    w, p, r = completed(monkeypatch)
    ctx = output(w, p)
    monkeypatch.setattr(graph, "_exchanges", lookup)
    plain = project.ProjectContextError("SIP_R3_SERVICE_EVIDENCE_REQUIRED")
    leaked = store.SizingExchangeError("private custody detail")
    for error in (plain, leaked):
        def refuse(*args, _error=error, **kwargs):
            raise _error
        monkeypatch.setattr(graph, "_stored_job", refuse)
        with pytest.raises(project.ProjectContextError) as caught:
            graph.require_project_sizing(ctx, conn=w)
        if error is plain:
            assert caught.value is plain
        else:
            assert caught.value.reason_code == "SIP_R3_PROOF_REJECTED"
            assert "private custody detail" not in str(caught.value)



def _raise_on_class(self):
    raise RuntimeError("private attribute custody detail")


class _AttributeHostileExchangeError(store.SizingExchangeError):
    """An exchange error whose ``__class__`` attribute lookup raises."""

    __class__ = property(_raise_on_class)


def test_R14C_39_classification_reads_the_type(monkeypatch):
    """R14C-39 The refusal class comes from type(), so an error whose attribute lookup raises is still refused."""
    w, p, r = completed(monkeypatch)
    ctx = output(w, p)
    error = _AttributeHostileExchangeError("private custody detail")

    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(graph, "_stored_job", refuse)
    # Record the outcome and assert outside the except clauses, so a leaked error never
    # chains the hostile exception into pytest's own report.
    refused, leaked = None, None
    try:
        graph.require_project_sizing(ctx, conn=w)
    except project.ProjectContextError as caught:
        refused = caught
    except Exception as other:
        leaked = type(other).__name__
    assert leaked is None, f"classification raised {leaked}"
    assert type(refused) is project.ProjectContextError
    assert refused.reason_code == "SIP_R3_PROOF_REJECTED"
    assert refused.args == ("SIP_R3_PROOF_REJECTED",)
    assert "private" not in str(refused)

def test_R14C_35_forged_manual_label(monkeypatch):
    """R14C-35 A cloud-sized graph relabelled manual is refused by proof, read and mutation."""
    w, p, r = completed(monkeypatch)
    read_setup(w, monkeypatch)
    monkeypatch.setattr(read.local, "_read_output", admission.forbidden)
    base = output(w, p).graph
    genuine = deepcopy(base)
    genuine["settings"]["extra"]["string_sizing"] = cloud.manual_sizing_evidence(genuine)
    genuine["settings"]["voc_cold"] = cloud.manual_voltage()
    graph.require_project_sizing(context(w, genuine), conn=w)
    forgeries = []
    relabelled = deepcopy(base)
    evidence = relabelled["settings"]["extra"]["string_sizing"]
    del evidence["project_exchange"]
    evidence["mode"] = graph.MANUAL_SIZING_MODE
    forgeries.append(relabelled)
    kept_voltage = deepcopy(base)
    kept_voltage["settings"]["extra"]["string_sizing"] = cloud.manual_sizing_evidence(kept_voltage)
    forgeries.append(kept_voltage)
    raised = deepcopy(kept_voltage)
    for frame in raised["frames"]:
        frame["module_power_watts"] = frame["module_power_watts"] * 2 + 1
    forgeries.append(raised)
    tool = "solar-nec-ampacity-correction"
    params = dict(base_ampacity=100, temp_factor=.91, conduit_factor=.8)
    for value in forgeries:
        reject(lambda: graph.require_project_sizing(context(w, value), conn=w))
        w.s.set_parent(b3.embedded(value))
        before = list(w.s.writes)
        with pytest.raises(project.ProjectContextError) as caught:
            read.run_project_read(str(w.s.org), w.s.project, w.s.drawing, w.s.parent.version_id,
                tool, params, catalog_digest=b3.manifest(tool))
        assert caught.value.reason_code == "SIP_R3_PROOF_REJECTED"
        ctx = graph.resolve_project_graph_context(w.s.context(w.s.parent.version_id))
        with pytest.raises(project.ProjectContextError) as caught:
            b3.prepare(w.s, "solar-feeders", {"expected_rev": value["rev"]}, ctx)
        assert caught.value.reason_code == "SIP_R3_PROOF_REJECTED" and w.s.writes == before


def synthetic_completed(monkeypatch):
    monkeypatch.setattr(cloud, "SIZING_CLASSIFICATION", "synthetic")
    monkeypatch.setattr(graph, "SYNTHETIC_SIZING_PROOF_ACCEPTED", True)
    w, p, r = completed(monkeypatch)
    assert w.exchange["classification"] == "synthetic"
    return w, p, r


def test_R14E0_06_bootstrap_acceptance_gates_synthetic_proof(monkeypatch):
    """R14E0-06 Stored synthetic custody proves only while the bootstrap acceptance is True."""
    w, p, r = synthetic_completed(monkeypatch)
    assert graph._stored_sizing_graph(w.context, w.job_id, w)["settings"]["panels_in_sequence"] == 10
    monkeypatch.setattr(graph, "SYNTHETIC_SIZING_PROOF_ACCEPTED", False)
    with pytest.raises(GraphValidationError) as caught:
        graph._stored_sizing_graph(w.context, w.job_id, w)
    assert caught.value.code == "SIP_R14_SYNTHETIC_EVIDENCE_REFUSED"


@pytest.mark.parametrize("value", [1, 1.0, "True", "true", [True], object(), None],
                         ids=["one", "float", "True-str", "true-str", "list", "object", "none"])
def test_R14E0_07_only_identity_true_accepts(monkeypatch, value):
    """R14E0-07 A truthy acceptance that is not the object True still refuses synthetic custody."""
    w, p, r = synthetic_completed(monkeypatch)
    monkeypatch.setattr(graph, "SYNTHETIC_SIZING_PROOF_ACCEPTED", value)
    with pytest.raises(GraphValidationError) as caught:
        graph._stored_sizing_graph(w.context, w.job_id, w)
    assert caught.value.code == "SIP_R14_SYNTHETIC_EVIDENCE_REFUSED"


@pytest.mark.parametrize("accepted", [False, True])
def test_R14E0_08_live_custody_unaffected(monkeypatch, accepted):
    """R14E0-08 Live custody proves the same graph whatever the bootstrap acceptance holds."""
    w, p, r = completed(monkeypatch)
    monkeypatch.setattr(graph, "SYNTHETIC_SIZING_PROOF_ACCEPTED", accepted)
    assert w.exchange["classification"] == "live"
    assert graph._stored_sizing_graph(w.context, w.job_id, w)["settings"]["panels_in_sequence"] == 10


@pytest.mark.parametrize("stored, written", [("synthetic", "live"), ("live", "synthetic")])
def test_R14E0_09_label_disagreement_rejected(monkeypatch, stored, written):
    """R14E0-09 Evidence relabelled against its exchange refuses even with acceptance True."""
    w, p, r = synthetic_completed(monkeypatch) if stored == "synthetic" else completed(monkeypatch)
    monkeypatch.setattr(graph, "SYNTHETIC_SIZING_PROOF_ACCEPTED", True)
    record = json.loads(w.exchange["evidence_bytes"])
    record["classification"] = written
    raw = graph.local.canonical_bytes(record)
    w.exchange.update(evidence_bytes=raw, evidence_sha256=graph._sha(raw))
    with pytest.raises(GraphValidationError) as caught:
        graph._stored_sizing_graph(w.context, w.job_id, w)
    assert caught.value.code == "SIP_R14_EVIDENCE_REJECTED"


def test_R14E0_10_seam_lives_only_in_two_constants():
    """R14E0-10 Product code sets the seam once each, never from a request, parameter or environment."""
    import re
    from pathlib import Path
    server = Path(__file__).resolve().parents[1]
    sources = {}
    for path in sorted(server.rglob("*.py")):
        rel = path.relative_to(server).as_posix()
        if rel.startswith("tests/") or "/tests/" in rel or "__pycache__" in rel:
            continue
        sources[rel] = path.read_text(encoding="utf-8")
    def where(pattern):
        found = []
        for rel, text in sources.items():
            found += [rel] * len(re.findall(pattern, text, re.M))
        return found
    assert where(r"^SYNTHETIC_SIZING_PROOF_ACCEPTED = False$") == ["solar_project_graph.py"]
    assert where(r"\bSYNTHETIC_SIZING_PROOF_ACCEPTED\s*=(?!=)") == ["solar_project_graph.py"]
    assert where(r'^SIZING_CLASSIFICATION = "live"$') == ["solar_sizing_client.py"]
    assert where(r"\bSIZING_CLASSIFICATION\s*=(?!=)") == ["solar_sizing_client.py"]
    assert where(r"allow_synthetic_for_test=") == ["solar_project_graph.py"]
    assert where(r"SYNTHETIC_SIZING_PROOF_ACCEPTED is True") == ["solar_project_graph.py"]
    assert where(r"setattr\([^)]*(SYNTHETIC_SIZING_PROOF_ACCEPTED|SIZING_CLASSIFICATION)") == []
    assert where(r"(environ|getenv)[^\n]*(SYNTHETIC_SIZING_PROOF_ACCEPTED|SIZING_CLASSIFICATION)") == []
    assert where(r"proof_string_size[r]") == []
    assert graph.SYNTHETIC_SIZING_PROOF_ACCEPTED is False and cloud.SIZING_CLASSIFICATION == "live"
