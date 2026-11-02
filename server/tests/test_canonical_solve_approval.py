"""BSP47 solve custody using real catalog identity and synthetic authority/storage."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import project_checkout_custody as custody
import solar_project_context as project
import solar_tools
from test_project_checkout_custody import BINDING, DRAWING, PINS, VERSION, issue, lane  # noqa: F401


_catalog_digest = custody.deps.catalog_tool_digest
_catalog_view = custody.deps.catalog_tool_view
TOOL = "solar-solve-proposal"
CONTEXT = {"frame_ref": "frame-1", "expected_rev": 17, "phase": "initial"}


@pytest.fixture
def solve(lane, monkeypatch):
    tool = solar_tools.trusted_record(TOOL)
    pins = {**PINS, "catalog_digest": _catalog_digest(tool),
            "tool_manifest_sha256": _catalog_digest(tool)}
    params = {"grant_ref": "fixture-grant", "request": {"num_mppt": 2}}
    lane.approval.update(tool=TOOL, capability="solve", params=deepcopy(params))
    lane.gate.update(action="run_solve_tool")
    lane.gate["args"].update(tool=TOOL, params=deepcopy(params),
        solve_context=deepcopy(CONTEXT), **pins)
    lane.data["confirm"]["proposal"].update(tool=TOOL, capability="solve", params=deepcopy(params))
    monkeypatch.setattr(custody.deps, "find_tool", lambda *_a: deepcopy(tool))
    monkeypatch.setattr(custody.deps, "catalog_tool_digest", _catalog_digest)
    monkeypatch.setattr(custody.deps, "catalog_tool_view", _catalog_view)
    monkeypatch.setattr(custody.customization_service, "effective_catalog_pin", lambda _t: dict(pins))
    selected = {"rev": 17}
    verified = project.resolve_context(None, None, None)
    graph_reads = []
    def selected_graph(context):
        assert context is verified
        graph_reads.append(True)
        return SimpleNamespace(graph=selected)
    monkeypatch.setattr(custody.graph, "resolve_project_graph_context", selected_graph)
    lane.params, lane.pins = params, pins
    lane.selected, lane.graph_reads = selected, graph_reads
    _rehash(lane)
    return lane


def _rehash(lane):
    lane.gate["args_hash"] = custody.agent_gate.canonical_args_hash(lane.gate["args"])


def _proof(lane, context=None):
    lane.approval["consumed"] = True
    lane.gate["consumed_at"] = datetime.now(timezone.utc).isoformat()
    return custody.resolve_for_turn(lane.tenant, "session", "resume", tool=TOOL,
        params=deepcopy(lane.params), catalog_pins=dict(lane.pins), input_binding=deepcopy(BINDING),
        run_extras={"solve_context": deepcopy(CONTEXT if context is None else context)})


@pytest.mark.parametrize("phase", ["initial", "background"])
def test_exact_solve_approval_issues_and_rechecks_private_possession(solve, phase):
    context = {**CONTEXT, "phase": phase}
    solve.gate["args"]["solve_context"] = context
    _rehash(solve)
    first = issue(solve)
    assert issue(solve) == first
    grant = _proof(solve, context)
    assert grant.fence_for(solve.tenant, project.checkout_scope(
        BINDING["project_id"], DRAWING), solve.state) == 7
    assert grant.idempotency_key == "checkout-custody:" + first["delegation_id"]
    assert len(solve.db.rows) == 1 and len(solve.verified) == 2
    assert len(solve.graph_reads) == 4
    assert solve.token not in repr(solve.db.rows) + repr(solve.db.statements)
    assert not any(sql.startswith("UPDATE") for sql, _ in solve.db.statements)


@pytest.mark.parametrize("capability,action", [
    ("drawing.read", "run_read_tool"), ("drawing.write", "run_write_tool"),
    ("solve", "run_read_tool"), ("solve", "submit_live_solve"),
    ("drawing.write", "run_solve_tool"),
])
def test_solve_cannot_borrow_read_write_or_aps_approval(solve, capability, action):
    solve.approval["capability"], solve.gate["action"] = capability, action
    with pytest.raises(custody.CustodyError, match="checkout_custody_denied"):
        issue(solve)
    assert not solve.db.rows and not solve.verified


@pytest.mark.parametrize("context", [
    None, {}, {**CONTEXT, "extra": True}, {k: v for k, v in CONTEXT.items() if k != "frame_ref"},
    {**CONTEXT, "frame_ref": ""}, {**CONTEXT, "frame_ref": 1},
    {**CONTEXT, "expected_rev": True}, {**CONTEXT, "expected_rev": 17.0},
    {**CONTEXT, "expected_rev": -1}, {**CONTEXT, "phase": "other"},
])
def test_invalid_approved_solve_context_never_issues(solve, context):
    solve.gate["args"]["solve_context"] = context
    _rehash(solve)
    with pytest.raises(custody.CustodyError, match="checkout_custody_denied"):
        issue(solve)
    assert not solve.db.rows and not solve.verified


@pytest.mark.parametrize("rev", [18, True, None])
def test_approved_revision_must_match_verified_selected_graph(solve, rev):
    solve.selected["rev"] = rev
    with pytest.raises(custody.CustodyError, match="checkout_custody_denied"):
        issue(solve)
    assert not solve.db.rows and not solve.verified


@pytest.mark.parametrize("context", [
    {**CONTEXT, "expected_rev": 18}, {**CONTEXT, "phase": "background"},
    {**CONTEXT, "frame_ref": "other-frame"},
])
def test_solve_resume_cannot_change_approved_context(solve, context):
    issue(solve)
    before = deepcopy(solve.db.rows)
    with pytest.raises(custody.CustodyError, match="checkout_custody_denied"):
        _proof(solve, context)
    assert solve.db.rows == before


@pytest.mark.parametrize("tool", ["solar-commit-solve", "solar-settings", "unknown-solve"])
def test_solve_approval_is_only_for_the_exact_proposal_tool(solve, tool):
    solve.approval["tool"] = solve.gate["args"]["tool"] = tool
    _rehash(solve)
    with pytest.raises(custody.CustodyError, match="checkout_custody_denied"):
        issue(solve)
    assert not solve.db.rows


def test_changed_catalog_tool_cannot_issue_solve_custody(solve, monkeypatch):
    changed = solar_tools.trusted_record(TOOL)
    changed["capabilities"] = ["drawing.write"]
    monkeypatch.setattr(custody.deps, "find_tool", lambda *_a: changed)
    with pytest.raises(custody.CustodyError, match="checkout_custody_denied"):
        issue(solve)
    assert not solve.db.rows


def test_solve_requires_genuine_checkout_possession(solve):
    with pytest.raises(custody.CustodyError):
        custody.issue(solve.tenant, "cid", "session", VERSION, {"fence": 7, "holder": "Browser"})
    assert not solve.db.rows


@pytest.mark.parametrize("drift", ["graph", "revoked", "expired"])
def test_solve_typed_proof_rechecks_graph_and_private_custody(solve, drift):
    issued = issue(solve)
    grant = _proof(solve)
    if drift == "graph":
        solve.selected["rev"] = 18
    elif drift == "revoked":
        assert custody.revoke(solve.tenant, "cid", "session", issued["delegation_id"])["revoked"]
    else:
        next(iter(solve.db.rows.values()))["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(project.ProjectContextError, match="CHECKOUT_DENIED"):
        grant.fence_for(solve.tenant, project.checkout_scope(BINDING["project_id"], DRAWING), solve.state)


def test_existing_write_approval_remains_supported(lane):
    assert issue(lane)["delegated"]
    assert len(lane.db.rows) == 1
