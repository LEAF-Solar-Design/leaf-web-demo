"""Wave 4 consolidated integration-drill manifest (plan: Wave 4 'Integration
tests' list). One artifact pins WHERE each of the plan's thirteen adversarial
drills is enforced, so no drill's coverage can be renamed, moved, or deleted
without this capstone going red in the same diff. Structural checks parse the
covering file's AST instead of importing it, so this module stays DB-free and
side-effect-free; the one executable drill here (forged elevation on a
default app) runs fail-closed with no database. CI's gate runs every covering
module for real; this manifest is the map, their runs are the territory.
"""
import ast
from pathlib import Path

from fastapi.testclient import TestClient

TESTS_DIR = Path(__file__).resolve().parent

# Plan drill -> the test functions that enforce it, as (file, function).
# A pair is load-bearing: change either name and this capstone fails.
DRILL_COVERAGE = {
    "forged operator role or profile": [
        ("test_operator_principals.py", "test_router_forged_headers_resolve_nothing"),
        ("test_operator_principals.py", "test_dev_header_names_lookup_only"),
    ],
    "same-tenant cross-user session and approval access": [
        ("test_approval_consume.py", "test_consume_approval_wrong_session_not_found"),
        ("test_approval_consume.py", "test_consume_approval_wrong_tenant_not_found"),
    ],
    "role revoked during a turn": [
        ("test_operator_authority.py", "test_revocation_between_mint_and_redeem_denies"),
        ("test_operator_principals.py", "test_role_revision_bump_invalidates"),
    ],
    "profile changed during SDK resume": [
        ("test_operator_principals.py", "test_session_key_and_no_oracle"),
        ("test_operator_principals.py", "test_turn_lock_and_snapshot"),
    ],
    "kill activated after approval": [
        ("test_operator_authority.py", "test_kill_switch_denies_mint_and_redeem"),
        ("test_operator_worker_boundary.py", "test_kill_switch_denies_dispatch_before_any_forwarding"),
    ],
    "target changed after approval": [
        ("test_operator_authority.py", "test_binding_mismatches_deny_without_consuming"),
        ("test_operator_authority.py", "test_policy_drift_denies_redemption"),
    ],
    "concurrent approval and redemption": [
        ("test_operator_authority.py", "test_fifty_concurrent_redemptions_admit_exactly_one"),
        ("test_approval_consume.py", "test_consume_approval_concurrent_exactly_one_winner"),
    ],
    "rate and spend races across processes": [
        ("test_operator_authority.py", "test_denied_mint_writes_audit_and_burns_no_rate"),
        ("test_operator_authority.py", "test_rate_ceiling_tightening_applies_within_the_hour"),
    ],
    "authority, policy, or audit storage unavailable": [
        ("test_operator_principals.py", "test_store_unavailable_maps_to_503"),
        ("test_operator_authority.py", "test_absent_catalog_denies_everything"),
        ("test_operator_worker_boundary.py", "test_harness_unreachable_is_fail_closed"),
    ],
    "prompt injection asking for generic execution": [
        ("test_operator_worker_boundary.py", "test_router_is_capability_only_never_executes_inline"),
        ("test_operator_production_unreachable.py", "test_o2_o3_generic_handler_cannot_reach_a_production_deploy_route"),
    ],
    "worker path, process, network, and secret escape": [
        ("test_operator_egress_boundary.py", "test_subprocess_denied_in_operator_context"),
        ("test_operator_egress_boundary.py", "test_operator_context_denies_non_deploy_non_allowlisted_host"),
        ("test_operator_worker_boundary.py", "test_dispatch_payload_is_bounded_and_isolation_forced"),
    ],
    "tenant six-tool and wire regression": [
        ("test_operator_vocab_freeze.py", "test_tenant_agent_policy_content_identical"),
        ("test_operator_vocab_freeze.py", "test_tenant_agent_policy_catalog_unchanged"),
    ],
    "mixed-version rollout where old components receive an operator request": [
        ("test_operator_vocab_freeze.py", "test_operator_namespace_absent_denies_with_404"),
        ("test_operator_vocab_freeze.py", "test_no_registered_route_under_operator_namespace"),
    ],
}


def _functions_defined(path: Path) -> set:
    """Top-level function names, by AST parse: no import, no side effects."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_manifest_covers_all_thirteen_plan_drills():
    assert len(DRILL_COVERAGE) == 13, (
        "the plan's Wave 4 list has thirteen drills; adding or dropping one "
        "here must be a deliberate, reviewed change")


def test_every_drill_names_live_coverage():
    missing = []
    for drill, pairs in DRILL_COVERAGE.items():
        assert pairs, f"drill {drill!r} pins no coverage"
        for fname, func in pairs:
            path = TESTS_DIR / fname
            if not path.is_file():
                missing.append(f"{drill}: file {fname} is gone")
                continue
            if func not in _functions_defined(path):
                missing.append(f"{drill}: {fname}::{func} is gone")
    assert not missing, (
        "drill coverage broke; restore or re-pin IN THE SAME PR:\n  "
        + "\n  ".join(missing))


def test_covering_modules_are_collected_by_the_gate():
    """Every pinned module must live in server/tests, where the gate's
    pytest collection actually runs it — a pin to an uncollected file
    would be a map to no territory."""
    for pairs in DRILL_COVERAGE.values():
        for fname, _ in pairs:
            assert (TESTS_DIR / fname).is_file(), fname
            assert fname.startswith("test_"), fname


# One drill executed here directly, DB-free, as this capstone's own teeth:
# forged elevation against a default (operator-flag-off) app must 404 on
# every operator surface — the forged-role AND mixed-version drills at once.
FORGED_ELEVATION_HEADERS = {
    "X-Operator-Subject": "attacker",
    "X-Operator-Profile": "release",
    "X-Ops-Secret": "guess",
}

OPERATOR_PROBES = [
    ("POST", "/api/operator/sessions"),
    ("GET", "/api/operator/audit"),
    ("POST", "/api/operator/worker/dispatch"),
    ("POST", "/api/operator/release/execute"),
    ("GET", "/api/operator/secrets"),
]


def test_forged_elevation_on_default_app_denies_404(monkeypatch):
    monkeypatch.delenv("LEAF_OPERATOR_ENABLED", raising=False)
    from app import app

    client = TestClient(app, raise_server_exceptions=False)
    for method, path in OPERATOR_PROBES:
        resp = client.request(method, path, headers=FORGED_ELEVATION_HEADERS)
        assert resp.status_code == 404, (
            f"{method} {path} answered {resp.status_code} to forged "
            "elevation on a default app; the operator surface must be "
            "indistinguishable from absent")
