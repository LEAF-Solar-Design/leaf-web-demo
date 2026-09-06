"""W4g-7b routing skeleton for the hosted drawing mutation path."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import mutation_plan
import write_loop
from envelopes import DEFAULT_HTTP_STATUS, ErrorCode

import mutation_apply


BASE_SHA = "1" * 64
V3_DISABLED = "contract v3 is not enabled on this deployment"
V3_UNPROVISIONED = "contract v3 Activity is not provisioned"


def _line_plan():
    return mutation_plan.validate_mutations({"polylines": []}, {"added": [{
        "handle": "new-line", "kind": "LINE", "layer": "0", "pts": [[0, 0], [3, 4]],
    }]})


class RoutingDa:
    """Stop at the submission boundary after recording the exact target."""

    def __init__(self):
        self.uploads = []
        self.targets = []
        self.submissions = []

    def upload_scratch_object(self, path, key):
        self.uploads.append((key, Path(path).read_bytes()))

    def scratch_signed_download_url(self, key):
        return f"https://scratch.test/get/{key}"

    def scratch_signed_upload_url(self, key):
        return "upload-token", f"https://scratch.test/put/{key}"

    def activity_qualified(self, name):
        self.targets.append(name)
        # The real client qualifies by its OWN configurable alias, never a
        # literal, so this fake mirrors that instead of hardcoding "+prod".
        return f"owner.{name}+{mutation_apply.client.ALIAS}"

    def submit_workitem(self, activity, arguments, **kwargs):
        self.submissions.append((activity, arguments, kwargs))
        return {"id": "routing-only", "status": "failed"}


def _submit(da, canonical, plan_bytes):
    return write_loop._apply_plan_live(
        tenant_id="tenant", drawing_id="drawing", head_v=1,
        vkey="source.dwg", execution_source=b"AC1032-source",
        bridged_legacy_bootstrap=False, base_intake={"polylines": []},
        canonical=canonical, plan_bytes=plan_bytes,
        plan_digest=mutation_plan.plan_sha256(plan_bytes), backend=object(), da=da,
        name="cad-edit-plan", tool_version="1.0.0", t0=0.0,
        ledger_entry=None, holder=None, fence=None, on_submitted=None,
        source_ref=None, meta_note="routing", envelope={}, result={}, provenance={},
        planner_ms=None, drawing_fetch_ms=None, scratch_keys=[],
    )


def test_v2_line_header_submits_to_the_frozen_activity(monkeypatch):
    canonical = _line_plan()
    plan = mutation_plan.emit_plan(canonical, base_sha256=BASE_SHA)
    assert plan.splitlines()[0] == b"LEAF_MUTATION_PLAN|2"

    def unexpected_readiness(*args, **kwargs):
        raise AssertionError("the v3 pre-upload guard must not change the v2 path")

    monkeypatch.setattr(mutation_apply, "readiness", unexpected_readiness)
    da = RoutingDa()
    env, status = _submit(da, canonical, plan)
    assert status == DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED]
    assert env["error"]["error_code"] == ErrorCode.WORKITEM_FAILED
    assert da.targets == ["LeafApplyMutations"]
    assert da.submissions[0][0] == "owner.LeafApplyMutations+prod"
    assert [data for _key, data in da.uploads] == [b"AC1032-source", plan]
    assert write_loop.WRITE_ACTIVITY == "LeafApplyMutations"


@pytest.mark.parametrize("ready", [False, True])
def test_v3_header_checks_readiness_before_upload_and_selects_its_target(monkeypatch, ready):
    canonical = _line_plan()
    monkeypatch.setattr(mutation_plan, "uses_v3", lambda value: True)
    plan = mutation_plan.emit_plan(canonical, base_sha256=BASE_SHA)
    assert plan.splitlines()[0] == b"LEAF_MUTATION_PLAN|3"
    da = RoutingDa()
    calls = []

    def readiness(contract=2):
        assert da.uploads == []
        calls.append(contract)
        return {"ready": ready, "contract": contract, "mismatches": [] if ready else ["alias absent"]}

    monkeypatch.setattr(mutation_apply, "readiness", readiness)
    env, status = _submit(da, canonical, plan)
    assert calls == [3]
    if not ready:
        assert status == 503
        assert env["error"]["error_code"] == ErrorCode.APS_UNAVAILABLE
        assert env["error"]["retryable"] is True
        assert env["error"]["message"] == f"mutation Activity not ready: {V3_UNPROVISIONED}"
        assert da.uploads == [] and da.targets == [] and da.submissions == []
    else:
        assert status == DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED]
        assert da.targets == ["LeafApplyMutationsV3"]
        assert da.submissions[0][0] == "owner.LeafApplyMutationsV3+prod"
        assert [data for _key, data in da.uploads] == [b"AC1032-source", plan]


def test_explicit_contract_forces_only_the_header():
    canonical = _line_plan()
    default = mutation_plan.emit_plan(canonical, base_sha256=BASE_SHA)
    assert mutation_plan.emit_plan(canonical, base_sha256=BASE_SHA, contract=2) == default
    forced = mutation_plan.emit_plan(canonical, base_sha256=BASE_SHA, contract=3)
    assert forced == default.replace(b"LEAF_MUTATION_PLAN|2\n", b"LEAF_MUTATION_PLAN|3\n", 1)


@pytest.mark.parametrize("kind", mutation_plan.V3_ADD_KINDS)
def test_v3_adds_are_declared_but_refused(kind):
    mutation = {"added": [{"kind": kind, "handle": "new-v3", "name": "ExistingBlock"}]}
    assert mutation_plan.uses_v3(mutation) is True
    with pytest.raises(ValueError, match=f"^{V3_DISABLED}$"):
        mutation_plan.validate_mutations({"polylines": []}, mutation)


@pytest.mark.parametrize("operation", mutation_plan.V3_SET_OPS)
@pytest.mark.parametrize("entries", [[], [{"handle": "A", "value": 7}]])
def test_v3_property_keys_are_refused_even_when_empty(operation, entries):
    mutation = {operation: entries}
    assert mutation_plan.uses_v3(mutation) is bool(entries)
    with pytest.raises(ValueError, match=f"^{V3_DISABLED}$"):
        mutation_plan.validate_mutations({"polylines": []}, mutation)


@pytest.mark.parametrize("value", [
    {"added": [None]}, {"added": None}, "garbage", [], None, 3, True,
    {"added": {"kind": "INSERT"}}, {"added": [[], "INSERT", 3]},
    {"set_color": None}, {"set_color": "garbage"}, {"set_color": [None]},
])
def test_uses_v3_ignores_malformed_json_shapes(value):
    assert mutation_plan.uses_v3(value) is False


def test_v1_bytes_and_catalogue_target_stay_frozen():
    base = {"polylines": [{"handle": "A", "layer": "Panels", "closed": True,
                            "pts": [[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]]}]}
    canonical = mutation_plan.validate_mutations(base, {
        "removed": ["A"], "added": [{
            "handle": "N", "layer": "Leaf Output", "closed": True, "xdata": None,
            "pts": [[10, 10, 0], [12, 10, 0], [12, 12, 0], [10, 12, 0]],
        }],
    })
    assert set(canonical) == {"added", "removed"}
    assert "kind" not in canonical["added"][0]
    assert mutation_plan.uses_v2(canonical) is False
    assert mutation_plan.uses_v3(canonical) is False
    plan = mutation_plan.emit_plan(canonical, base_sha256=BASE_SHA)
    assert plan == (
        "LEAF_MUTATION_PLAN|1\n" + f"BASE_SHA256|{BASE_SHA}\n"
        "REMOVE|A\nADD|Leaf Output|0,0,1|0|10,10;12,10;12,12;10,12\n"
    ).encode("ascii")
    da = RoutingDa()
    _submit(da, canonical, plan)
    assert da.targets == ["LeafApplyMutations"]


def test_submission_and_readiness_target_the_same_configurable_alias(monkeypatch):
    monkeypatch.setattr(mutation_apply.client, "ALIAS", "canary")
    canonical = _line_plan()
    plan = mutation_plan.emit_plan(canonical, base_sha256=BASE_SHA, contract=3)
    da = RoutingDa()

    def readiness(contract=2):
        return {"ready": True, "contract": contract, "mismatches": [],
                "activity": {"alias": mutation_apply.client.ALIAS, "version": 1}}

    monkeypatch.setattr(mutation_apply, "readiness", readiness)
    _submit(da, canonical, plan)
    assert da.submissions[0][0] == "owner.LeafApplyMutationsV3+canary"


def test_broker_readiness_cache_is_separate_for_each_contract(monkeypatch):
    import broker

    monkeypatch.setattr(broker, "_plan_readiness_cache", {})
    clock = [100.0]
    monkeypatch.setattr(broker.time, "monotonic", lambda: clock[0])
    calls = []

    def readiness(contract=2):
        calls.append(contract)
        return {"ready": contract == 2, "contract": contract, "mismatches": []}

    monkeypatch.setattr(mutation_apply, "readiness", readiness)
    for _ in range(2):
        assert broker._plan_activity_ready() == (True, {"ready": True, "contract": 2, "mismatches": []})
        assert broker._plan_activity_ready(3) == (False, {"ready": False, "contract": 3, "mismatches": []})
    assert calls == [2, 3]
    assert set(broker._plan_readiness_cache) == {2, 3}
    clock[0] += broker.PLAN_READINESS_TTL_S
    broker._plan_activity_ready(3)
    assert calls == [2, 3, 3]


@pytest.fixture()
def broker_plan_request(monkeypatch):
    import broker

    monkeypatch.setattr(broker, "tenant_disabled", lambda tenant: False)
    monkeypatch.setattr(write_loop, "drawing_mutations_enabled", lambda: True)
    monkeypatch.setattr(broker, "_tenant_tier", lambda tenant: "pro")
    monkeypatch.setattr(broker.entitlements, "tool_required_capability", lambda tool: "drawing.write")
    monkeypatch.setattr(broker.entitlements, "entitlements_for", lambda tier: {"drawing.write": True})
    monkeypatch.setattr(broker, "_require_supported_live_completion_mode", lambda: None)
    da = SimpleNamespace(run_tool=lambda: None)
    backend = object()
    monkeypatch.setattr(broker, "_get_da", lambda: da)

    def default_backend(*, aps_live, da):
        assert aps_live is True
        return backend

    def read_intake(actual_backend, tenant, drawing, version):
        assert actual_backend is backend
        assert (tenant, drawing, version) == ("tenant", "drawing", 1)
        return 1, {"polylines": []}

    monkeypatch.setattr(write_loop, "default_backend", default_backend)
    monkeypatch.setattr(write_loop, "read_intake", read_intake)

    def request(mutations):
        return broker.BrokerPlanRunRequest(
            tenant_id="tenant", dwg="drawing", dwg_version=1,
            plan={"drawing_id": "drawing", "parent_version": 1, "mutations": mutations,
                  "plan_sha256": BASE_SHA, "source_sha256": BASE_SHA},
        )

    return request


@pytest.mark.parametrize("v3,contract", [(False, 2), (True, 3)])
def test_broker_selects_readiness_from_the_canonical_plan(
        monkeypatch, broker_plan_request, v3, contract):
    import broker

    req = broker_plan_request({"added": [{
        "handle": "new-line", "kind": "LINE", "layer": "0", "pts": [[0, 0], [3, 4]],
    }]})
    canonical = _line_plan()
    selected = []

    def uses_v3(value):
        assert value == canonical
        assert value != req.plan.mutations
        selected.append(value)
        return v3

    monkeypatch.setattr(mutation_plan, "uses_v3", uses_v3)
    calls = []

    def readiness(contract=2):
        calls.append(contract)
        return False, {"ready": False, "contract": contract, "mismatches": ["alias absent"]}

    monkeypatch.setattr(broker, "_plan_activity_ready", readiness)
    env, status = broker._execute_plan(req, {}, "write", 0.0, {}, quota_reserved=True)
    assert status == 503 and env["error"]["error_code"] == ErrorCode.APS_UNAVAILABLE
    assert selected == [canonical]
    assert calls == [contract]


@pytest.mark.parametrize("mutations,message", [
    ({"added": [None]}, "added entity at index 0 must be an object"),
    ({"added": None}, "mutations.added must be a list"),
    (
        {"added": [{"handle": "new-line", "kind": "LINE", "layer": "0",
                    "pts": [[10 ** 309, 0], [3, 4]]}]},
        "added entity 'new-line' point 0 is outside the supported range",
    ),
])
def test_broker_malformed_mutations_keep_the_validator_refusal(
        monkeypatch, broker_plan_request, mutations, message):
    import broker

    def unexpected(*args, **kwargs):
        raise AssertionError("invalid mutations must be refused before routing or execution")

    monkeypatch.setattr(mutation_plan, "uses_v3", unexpected)
    monkeypatch.setattr(broker, "_plan_activity_ready", unexpected)
    monkeypatch.setattr(write_loop, "run_data_plan_live", unexpected)
    req = broker_plan_request(mutations)
    env, status = broker._execute_plan(req, {}, "write", 0.0, {}, quota_reserved=True)
    assert status == 422
    assert env["error"]["error_code"] == ErrorCode.BAD_PARAMS
    assert env["error"]["message"] == f"the edit plan was refused: {message}"
    assert env["error"]["retryable"] is False
