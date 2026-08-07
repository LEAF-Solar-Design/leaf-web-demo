"""Operator-vocabulary freeze gate (contract/OPERATOR.md + AUTH.md section 11.6).

Lands with the contract-only operator PR (Wave 0). Load-bearing negative tests:

1. The frozen identity vocabularies gained NO operator entry — operator
   authority is deliberately not claim-mintable, so the tier set, capability
   set, and role file must be byte-identical to the pre-operator freeze.
2. The tenant agent-policy catalog is unchanged (exact action-name -> policy
   map) — the operator catalog is a separate namespace, never a tenant edit.
3. The /api/operator/* namespace does not exist on this app revision: any
   probe, including one carrying every forgeable elevation header, answers
   404. This is the mixed-version guarantee — an old component DENIES an
   operator request; it never downgrades it into a tenant request.
   NOTE for Lane A: when the operator router mounts, amend test 3 in the SAME
   PR (per the promotion ritual) to expect authenticated-deny (401/403/404
   no-oracle) instead of route-absent 404.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parents[1]

# --- 1. Frozen identity vocabularies: no operator entry appeared -----------

FROZEN_TIERS = {
    "demo", "guest", "restricted", "self_hosted",
    "hosted_starter", "hosted_pro", "admin",
}

FROZEN_CAPABILITIES = {
    "run_read", "run_write", "solve", "build", "converse",
    "agent_write_autopilot", "deploy", "platform_customize", "upload",
}


def test_tier_vocabulary_gained_no_operator_entry():
    import billing_tiers

    assert set(billing_tiers.TIER_VOCABULARY) == FROZEN_TIERS
    assert not any("operator" in t for t in billing_tiers.TIER_VOCABULARY)


def test_capability_vocabulary_gained_no_operator_entry():
    import entitlements

    assert set(entitlements.CAPABILITIES) == FROZEN_CAPABILITIES
    assert not any("operator" in c for c in entitlements.CAPABILITIES)


def test_roles_file_gained_no_operator_role():
    roles = json.loads((SERVER_DIR / "roles.json").read_text(encoding="utf-8"))
    role_names = set(roles.get("roles", roles) if isinstance(roles, dict) else roles)
    assert not any("operator" in name for name in role_names), (
        "operator authority must come from the server-owned operator_principals "
        "store, never a role claim (contract/OPERATOR.md section 1)")


# --- 2. Tenant agent-policy catalog unchanged (whole-content pin) ----------

# SHA-256 over the canonical JSON (sort_keys, compact separators) of the
# ENTIRE parsed agent_policy.json — every action's routes, rung, schema,
# capability, timeout, and every top-level knob. Whitespace-insensitive,
# content-exact: any semantic drift (e.g. a production route appearing in a
# dispatch list) changes this digest. A legitimate tenant-catalog change
# re-pins this constant in the same PR, per the promotion ritual.
FROZEN_TENANT_POLICY_SHA256 = (
    "7c35884f5cfd1b654b5e8b418deec2e59e5a86f751fa23c238b9dac628a88f1d")

FROZEN_TENANT_ACTIONS = {
    "request_confirmation": "always-confirm",
    "read_platform_state": "auto",
    "run_read_tool": "auto",
    "run_write_tool": "always-confirm",
    "submit_live_solve": "confirm-once",
    "undo_drawing_version": "auto",
    "author_tool": "confirm-once",
    "request_publication": "auto",
    "register_tool": "always-confirm",
    "customize_platform": "always-confirm",
    "propose_overlay": "auto",
}


def _load_tenant_policy():
    return json.loads(
        (SERVER_DIR / "agent_policy.json").read_text(encoding="utf-8"))


def test_tenant_agent_policy_content_identical():
    import hashlib

    canon = json.dumps(_load_tenant_policy(), sort_keys=True,
                       separators=(",", ":"))
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert digest == FROZEN_TENANT_POLICY_SHA256, (
        "server/agent_policy.json content drifted under the operator "
        "contract; if the tenant catalog change is intentional, re-pin this "
        "digest in the same PR (promotion ritual)")


def test_tenant_agent_policy_catalog_unchanged():
    live = {name: entry["policy"]
            for name, entry in _load_tenant_policy()["actions"].items()}
    assert live == FROZEN_TENANT_ACTIONS
    assert not any(name.startswith("operator.") for name in live), (
        "operator actions live in server/operator_policy.json, never here")


def test_tenant_agent_policy_names_no_production_route():
    for name, entry in _load_tenant_policy()["actions"].items():
        for route in entry.get("dispatch", {}).get("routes", []):
            lowered = route.lower()
            assert "production" not in lowered and "deploy-platform" not in lowered, (
                f"action {name} names a production-shaped route: {route}")


# --- 3. Operator namespace absent: forged elevation resolves nothing -------

FORGED_ELEVATION_HEADERS = {
    "Authorization": "Bearer forged.admin.token",
    "X-Tenant-Id": "demo-tenant",
    "X-Ops-Secret": "forged-ops-secret",
    "X-Dispatch-Secret": "forged-dispatch-secret",
    "X-Harness-Secret": "forged-harness-secret",
    "X-Operator": "true",
    "X-Operator-Profile": "default",
}

OPERATOR_PROBES = (
    ("GET", "/api/operator/sessions"),
    ("POST", "/api/operator/sessions"),
    ("GET", "/api/operator/sessions/any-id"),
    ("POST", "/api/operator/sessions/any-id/messages"),
    ("GET", "/api/operator/audit"),
    ("POST", "/api/operator/approvals/any-id"),
)


def test_no_registered_route_under_operator_namespace():
    """Exhaustive, not sampled: walk every registered route on the app and
    prove none lives under /api/operator. Covers /stream, /transcript, and
    any surface a later change might mount without amending this gate."""
    from app import app

    operator_routes = [
        getattr(r, "path", "") for r in app.routes
        if getattr(r, "path", "").startswith("/api/operator")]
    assert operator_routes == [], (
        f"routes registered under /api/operator on a pre-operator app "
        f"revision: {operator_routes}; Lane A must amend this gate in the "
        "same PR that mounts the operator router")


def test_operator_namespace_absent_denies_with_404():
    from app import app

    client = TestClient(app, raise_server_exceptions=False)
    for method, path in OPERATOR_PROBES:
        resp = client.request(method, path, headers=FORGED_ELEVATION_HEADERS)
        assert resp.status_code == 404, (
            f"{method} {path} -> {resp.status_code}; an app revision without "
            "the operator control plane must DENY (404), never route or "
            "downgrade an operator request")
