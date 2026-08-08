"""Wave 4 capstone: production is UNREACHABLE from the operator control plane
(contract/OPERATOR.md section 7).

SOUNDNESS NOTE. A keyword denylist ("reject any route/action containing the word
'production'") is NOT a proof: an aliased action `release_live` or route
`/api/operator/release/live` would pass it while deploying to production. So this
gate is an ALLOWLIST/FREEZE: the exact set of operator actions and the exact set
of operator routes are pinned here. ANY new action or route, however it is
spelled, changes the set and fails this test until a human adds it to the pin,
where they must confirm it is not a production path. Combined with the matrix
SHA-freeze (test_operator_vocab_freeze.py) and the behavioral refusals below,
that is what makes production unreachable, not the absence of a magic word.

SCOPE. This proves the SERVER operator surface plus the contract:
  7.1  The credential / destination / deploy surfaces (secret broker, external
       allowlist, release stager) and the write runbooks refuse a production
       environment/target, fail-closed; the release-candidate table CHECK
       forbids production at the schema level.
  7.2  The mounted operator route surface is EXACTLY the pinned set (all under
       /api/operator/), and the default app mounts none of it.
  7.3  The operator action set is EXACTLY the pinned set; production promotion is
       absent from it and listed under not_mounted in the matrix.
The disposable-worker environment allowlist (section 7.1's "no production
credential in a worker") is enforced and frozen on the HARNESS side
(harness/src/operatorWorker) with its own gate; it is out of scope for this
server-side Python proof and deliberately not asserted here rather than checked
vacuously across the language boundary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

REPO_ROOT = SERVER_DIR.parent

# --- the pinned surface (allowlist) -----------------------------------------
# Every operator ACTION. Adding one (however spelled) fails until it is listed
# here AND in the SHA-pinned matrix, forcing a human to confirm it is staging-
# only. This is the guard an aliased `release_live` cannot slip past.
_EXPECTED_ACTIONS = frozenset({
    "operator.read_fleet_state", "operator.read_tenant_state",
    "operator.read_jobs", "operator.read_sessions", "operator.read_audit",
    "operator.read_worker_status", "operator.worker_submit_job",
    "operator.worker_cancel_job", "operator.repo_propose_change",
    "operator.tenant_agent_pause", "operator.tenant_agent_resume",
    "operator.tenant_overlay_set", "operator.worker_credential_rotate",
    "operator.external_write", "operator.stage_release_candidate",
})

# Every operator ROUTE path. Adding one (however spelled) changes this set and
# fails, so an aliased /api/operator/release/live cannot be introduced silently.
_EXPECTED_ROUTES = frozenset({
    "/api/operator/audit",
    "/api/operator/external/destinations",
    "/api/operator/external/execute",
    "/api/operator/external/propose",
    "/api/operator/release/execute",
    "/api/operator/release/propose",
    "/api/operator/release/{target}/{source_sha}/state",
    "/api/operator/runbooks/credential/execute",
    "/api/operator/runbooks/credential/propose",
    "/api/operator/runbooks/credential/{handle}/state",
    "/api/operator/runbooks/tenant-agent/{tenant_id}/state",
    "/api/operator/runbooks/tenant-agent/{verb}/execute",
    "/api/operator/runbooks/tenant-agent/{verb}/propose",
    "/api/operator/runbooks/tenant-overlay/execute",
    "/api/operator/runbooks/tenant-overlay/propose",
    "/api/operator/runbooks/tenant-overlay/{tenant_id}/state",
    "/api/operator/secrets",
    "/api/operator/secrets/{handle}",
    "/api/operator/sessions",
    "/api/operator/sessions/{session_id}",
    "/api/operator/sessions/{session_id}/events",
    "/api/operator/sessions/{session_id}/messages",
})


def _app_routes(enabled: bool) -> set:
    """ALL route paths of the REAL app, imported fresh in a subprocess with the
    operator flag off/on. Inspecting the runtime routes (not app.py's source) is
    what makes this sound: a router mounted by ANY import style, at ANY path
    prefix, appears here, so nothing can escape a source-parsing regex or a
    /api/ filter."""
    code = (
        "import os, json\n"
        "import app\n"
        "print('ROUTES_JSON=' + json.dumps(sorted(\n"
        "    getattr(r, 'path', '') for r in app.app.routes)))\n"
    )
    env = dict(os.environ)
    env["LEAF_OPERATOR_ENABLED"] = "1" if enabled else "0"
    out = subprocess.run([sys.executable, "-c", code], cwd=str(SERVER_DIR),
                         capture_output=True, text=True, env=env, check=True)
    line = next(ln for ln in out.stdout.splitlines()
                if ln.startswith("ROUTES_JSON="))
    return set(json.loads(line[len("ROUTES_JSON="):]))


# --- 7.3 the action set is exactly the pinned set; promotion is not in it ----

def test_catalog_action_set_is_exactly_the_pinned_set():
    import operator_policy
    catalog = operator_policy.load_catalog()
    assert set(catalog["actions"]) == _EXPECTED_ACTIONS  # no alias can be added


def test_matrix_action_set_equals_catalog_and_pins_no_promotion():
    matrix = json.loads((REPO_ROOT / "contract" /
                         "operator_action_matrix.v1.json").read_text(encoding="utf-8"))
    actions = matrix["actions"]
    assert set(actions) == _EXPECTED_ACTIONS  # catalog and matrix agree, frozen
    assert actions, "matrix actions must be non-empty (guards the all() below)"
    assert matrix["production_promotion_mounted"] is False
    assert "operator.promote_production" in matrix["not_mounted"]
    assert "operator.promote_production" not in actions
    assert all(a.get("production_reachable") is False for a in actions.values())


def test_no_action_handler_is_a_promotion():
    import operator_policy
    catalog = operator_policy.load_catalog()
    handlers = {e["handler"] for e in catalog["actions"].values()}
    # The handler set is bounded by the pinned action set; assert none promotes.
    assert not any("promote" in h or "prod" in h for h in handlers)


# --- 7.2 the route surface is exactly the pinned set; default app has none ---

def test_default_app_mounts_no_operator_route():
    off = _app_routes(enabled=False)
    # No route ANYWHERE (any prefix) mentions the operator namespace when dark.
    assert not any("operator" in p for p in off), \
        sorted(p for p in off if "operator" in p)


def test_flag_on_adds_exactly_the_pinned_operator_routes():
    off = _app_routes(enabled=False)
    on = _app_routes(enabled=True)
    added = on - off  # exactly what the real operator mount contributes
    # Exact-set equality is the sound guard: a new or aliased route, an eighth
    # router mounted by ANY import style, or a route escaped to a non-/api prefix
    # all appear in `added` and fail here, regardless of spelling.
    assert added == set(_EXPECTED_ROUTES), {
        "unexpected": sorted(added - set(_EXPECTED_ROUTES)),
        "missing": sorted(set(_EXPECTED_ROUTES) - added),
    }
    # Every route the mount added is operator-namespaced: a route escaped to
    # /internal/... would be in `added`, absent from the pin, and already fail
    # above; this second assertion states the invariant directly.
    assert all(p.startswith("/api/operator/") for p in added)


# --- 7.1 the deploy/credential surfaces refuse production (behavioral) -------

def test_secret_broker_refuses_production():
    import operator_secret_broker as broker
    broker.register_minter(lambda meta: "SHORT")
    broker.register_rotator(lambda meta: None)
    try:
        with pytest.raises(broker.SecretBrokerError) as e:
            broker.with_injected("github_operator_pr", "production", lambda c: None)
        assert e.value.reason == "production_scope_refused"
        with pytest.raises(broker.SecretBrokerError) as e2:
            broker.rotate("github_operator_pr", "production")
        assert e2.value.reason == "production_scope_refused"
    finally:
        broker.register_minter(None)
        broker.register_rotator(None)


def test_secret_registry_refuses_production_environment(tmp_path, monkeypatch):
    import operator_secret_broker as broker
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"handles": {"h": {
        "scope": "s", "environment": "production", "kind": "k", "ttl_s": 900}}}),
        encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_SECRETS_FILE", str(p))
    with pytest.raises(broker.SecretBrokerError):
        broker.list_handles()


def test_external_allowlist_refuses_production():
    import operator_external_adapters as ext
    with pytest.raises(ext.ExternalWriteError) as e:
        ext.verify_allowed("staging_status_webhook", "generic_webhook", "production")
    assert e.value.reason == "production_destination_refused"


def test_external_allowlist_rejects_production_destination(tmp_path, monkeypatch):
    import operator_external_adapters as ext
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"destinations": {
        "d": {"environment": "production", "adapter": "a"}}}), encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_DESTINATIONS_FILE", str(p))
    with pytest.raises(ext.ExternalWriteError):
        ext.list_destinations()


def test_release_stager_refuses_production_target():
    import operator_release_stager as stager
    stager.register_stager(lambda sha, t: {"previous_revision": "1",
                                           "new_revision": "2"})
    try:
        with pytest.raises(stager.StageError) as e:
            stager.stage("a1b2c3d4", "production")
        assert e.value.reason == "production_target_refused"
    finally:
        stager.register_stager(None)


def test_write_runbooks_refuse_production_environment_or_target():
    import operator_credential_rotate_runbook as cred
    with pytest.raises(cred.RunbookError) as e1:
        cred._broker_verify("github_operator_pr", "production")
    assert e1.value.reason == "production_scope_refused"

    import operator_external_write_runbook as extw
    with pytest.raises(extw.RunbookError) as e2:
        extw._verify("staging_status_webhook", "github_operator_pr",
                     "generic_webhook", "production")
    assert e2.value.reason == "production_destination_refused"

    import operator_stage_release_runbook as rel
    with pytest.raises(rel.RunbookError) as e3:
        rel._validate("a1b2c3d4", "production")
    assert e3.value.reason == "target_not_staging"


def test_release_migration_check_constrains_target_to_non_production():
    sql = (REPO_ROOT / "platform" / "migrations" /
           "0034_operator_release_candidates.sql").read_text(encoding="utf-8")
    assert "CHECK (target IN ('staging', 'development'))" in sql
    # 'production' must NOT appear in any executable statement (comments only).
    statements = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--"))
    assert "production" not in statements.lower()
