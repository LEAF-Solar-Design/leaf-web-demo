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

import hashlib
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent
MATRIX_PATH = REPO_ROOT / "contract" / "operator_action_matrix.v1.json"

# --- 1. Frozen identity vocabularies: no operator entry appeared -----------

FROZEN_TIERS = {
    "demo", "guest", "restricted", "self_hosted",
    "hosted_starter", "hosted_pro", "admin",
}

FROZEN_CAPABILITIES = {
    "run_read", "run_write", "solve", "build", "converse",
    "agent_write_autopilot", "deploy", "platform_customize", "upload",
}


# --- 0. Operator action matrix is self-contained and security-pinned -------
#
# The committed contract must carry its own action matrix (no dependency on an
# external/temp artifact). This block pins the whole matrix by content SHA AND
# asserts each security-critical field per action, so mutating any rung,
# policy, handler, reversal, or production-reachability fails the gate — and
# so does adding a production-reachable action or mounting production
# promotion.

FROZEN_MATRIX_SHA256 = (
    "5a6dd550800fed8eddcb683e67787435d5b8a3441cb7a66561b5f74614debe53")

# {action: (class, rung, policy, rate, spend, timeout_s, handler,
#           reversal_substring)}. Every normative dimension the pre-repo
# reference carried (rung, policy, rate, spend, timeout, precondition,
# reversal) now has an in-repo source and is pinned here.
FROZEN_MATRIX_FIELDS = {
    "operator.read_fleet_state": ("O1", 1, "auto", "low", "none", 10, "read_fleet_state", "read"),
    "operator.read_tenant_state": ("O1", 1, "auto", "low", "none", 10, "read_tenant_state", "read"),
    "operator.read_jobs": ("O1", 1, "auto", "low", "none", 10, "read_jobs", "read"),
    "operator.read_sessions": ("O1", 1, "auto", "low", "none", 10, "read_sessions", "read"),
    "operator.read_audit": ("O1", 1, "auto", "low", "none", 10, "read_audit", "read"),
    "operator.read_worker_status": ("O1", 1, "auto", "low", "none", 10, "read_worker_status", "read"),
    "operator.worker_submit_job": ("O2", 2, "auto", "medium", "cost_tokens", 1800, "worker_submit_job", "disposable"),
    "operator.worker_cancel_job": ("O2", 2, "auto", "medium", "none", 30, "worker_cancel_job", "idempotent"),
    "operator.repo_propose_change": ("O3", 3, "auto", "medium", "none", 1800, "repo_propose_change", "branch"),
    "operator.tenant_agent_pause": ("O4", 4, "always-confirm", "high", "none", 30, "tenant_agent_pause", "resume"),
    "operator.tenant_agent_resume": ("O4", 4, "always-confirm", "high", "none", 30, "tenant_agent_resume", "pause"),
    "operator.tenant_overlay_set": ("O4", 4, "always-confirm", "high", "none", 30, "tenant_overlay_set", "overlay"),
    "operator.worker_credential_rotate": ("O4", 4, "always-confirm", "high", "none", 60, "worker_credential_rotate", "scope"),
    "operator.external_write": ("O5", 5, "always-confirm", "high", "usd", 600, "external_write", "adapter"),
    "operator.stage_release_candidate": ("O6", 6, "always-confirm", "high", "usd", 3600, "stage_release_candidate", "rollback"),
}

# Required per-action keys the matrix must always carry (no normative
# dimension may silently disappear again).
REQUIRED_MATRIX_KEYS = {
    "class", "rung", "policy", "rate", "spend", "timeout_s",
    "precondition", "handler", "reversal", "production_reachable",
    "enabled_v1",
}


def _load_matrix():
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _parse_md_matrix_table():
    """Parse the OPERATOR.md §4.1 action table into {action: {header: cell}}.
    Contiguity-bounded so other tables in the doc are not picked up."""
    lines = (REPO_ROOT / "contract" / "OPERATOR.md").read_text(
        encoding="utf-8").splitlines()
    header_idx = next(
        i for i, ln in enumerate(lines)
        if ln.strip().startswith("|") and "Action" in ln and "Policy" in ln)
    headers = [c.strip().lower()
               for c in lines[header_idx].strip().strip("|").split("|")]
    table = {}
    for ln in lines[header_idx + 2:]:  # skip the |---| separator row
        if not ln.strip().startswith("|"):
            break  # end of this contiguous table
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) != len(headers):
            continue
        row = dict(zip(headers, cells))
        m = re.search(r"`(operator\.[a-z_]+)`", row.get("action", ""))
        if m:
            table[m.group(1)] = row
    return headers, table


def test_matrix_is_committed_and_content_pinned():
    assert MATRIX_PATH.exists(), (
        "contract/operator_action_matrix.v1.json is missing — the operator "
        "contract must carry its own matrix, not reference an external file")
    canon = json.dumps(_load_matrix(), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert digest == FROZEN_MATRIX_SHA256, (
        "operator action matrix content drifted; if intentional, re-pin this "
        "SHA in the same PR (promotion ritual)")


def test_matrix_carries_every_normative_dimension():
    """No normative dimension (rate/spend/timeout/precondition/...) may be
    dropped from any action entry."""
    for name, entry in _load_matrix()["actions"].items():
        missing = REQUIRED_MATRIX_KEYS - set(entry)
        assert not missing, (name, "missing normative fields", missing)


def test_matrix_security_fields_pinned_per_action():
    matrix = _load_matrix()
    actions = matrix["actions"]
    assert set(actions) == set(FROZEN_MATRIX_FIELDS), (
        "operator action set changed", set(actions) ^ set(FROZEN_MATRIX_FIELDS))
    for name, fields in FROZEN_MATRIX_FIELDS.items():
        cls, rung, policy, rate, spend, timeout_s, handler, rev_sub = fields
        entry = actions[name]
        assert entry["class"] == cls, (name, "class", entry["class"])
        assert entry["rung"] == rung, (name, "rung", entry["rung"])
        assert entry["policy"] == policy, (name, "policy", entry["policy"])
        assert entry["rate"] == rate, (name, "rate", entry["rate"])
        assert entry["spend"] == spend, (name, "spend", entry["spend"])
        assert entry["timeout_s"] == timeout_s, (name, "timeout_s")
        assert entry["handler"] == handler, (name, "handler", entry["handler"])
        assert rev_sub in entry["reversal"].lower(), (name, "reversal")
        assert entry["precondition"], (name, "precondition empty")


def test_matrix_has_no_production_reachable_action():
    matrix = _load_matrix()
    assert matrix["production_promotion_mounted"] is False
    assert "operator.promote_production" in matrix["not_mounted"]
    for name, entry in matrix["actions"].items():
        assert entry["production_reachable"] is False, (
            f"{name} declares production_reachable=true; production promotion "
            "must stay off every operator surface (contract section 7)")
        blob = json.dumps(entry).lower()
        assert "deploy-platform" not in blob
        # 'production' may appear only inside a reversal note, never as a route
        assert "/api/production" not in blob


def test_operator_md_table_fields_match_json():
    """Load-bearing JSON<->Markdown agreement: EVERY shared column of the
    OPERATOR.md §4.1 table must equal the JSON for every action, compared
    exactly. Editing any field in either copy fails the gate."""
    matrix = _load_matrix()["actions"]
    headers, table = _parse_md_matrix_table()
    # md-header -> json-key (exact string compare of str(json_value))
    COLUMN_MAP = {
        "class": "class", "rung": "rung", "policy": "policy",
        "rate": "rate", "spend": "spend", "timeout(s)": "timeout_s",
        "precondition": "precondition", "reversal": "reversal",
    }
    for col in list(COLUMN_MAP) + ["handler", "prod-reachable", "v1"]:
        assert col in headers, f"OPERATOR.md §4.1 table missing '{col}' column"
    # Bidirectional membership: neither a JSON action missing from the table
    # nor a Markdown-only action row may slip through.
    assert set(table) == set(matrix), (
        "OPERATOR.md table and JSON action sets differ",
        set(table) ^ set(matrix))
    for name, entry in matrix.items():
        row = table[name]
        for md_col, json_key in COLUMN_MAP.items():
            assert row[md_col] == str(entry[json_key]), (
                name, md_col, "md=", row[md_col], "json=", entry[json_key])
        # handler cell may be backtick-wrapped; compare on the bare name
        assert entry["handler"] == row["handler"].strip("`"), (
            name, "handler", row["handler"])
        assert row["prod-reachable"].lower() == "no", (
            name, "prod-reachable", row["prod-reachable"])
        # enabled_v1 is bool in JSON, rendered on/off in the table.
        assert row["v1"] == ("on" if entry["enabled_v1"] else "off"), (
            name, "v1", row["v1"], entry["enabled_v1"])


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
