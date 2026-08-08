"""Wave 4 capstone: the operator control plane does not DECLARE or REQUEST a
production operation, and its declared surface cannot grow silently
(contract/OPERATOR.md section 7).

WHAT A TEST CAN AND CANNOT PROVE HERE. "Production is unreachable" is a claim
about BEHAVIOR, and no static test can prove that an arbitrary handler body, or
a deployment-registered callback (a stager/minter/adapter is trusted code), does
not itself reach production - that is a per-action independent-review guarantee
and a trust boundary, exactly like the secret broker's minter. What this gate
DOES prove, soundly and non-vacuously, is the structural frame that makes the
review meaningful:

  7.1  The operator surfaces that take an environment/target REFUSE production,
       fail-closed (secret broker, external allowlist, release stager, the write
       runbooks), and the plane never REQUESTS a production target (the runbooks
       validate staging-only before calling a registered callback). The
       release-candidate table CHECK forbids production at the schema level.
  7.2  The operator ROUTE surface the real mount adds is EXACTLY the pinned set
       of (method, path) endpoints, discovered from the RUNTIME app (off vs on),
       so a new/aliased route, a new METHOD on an existing path, an eighth
       router mounted by ANY import style, or a route escaped to a non-/api
       prefix all appear in the diff and fail the pin - forcing a human to add
       it and confirm it is not a production path.
  7.3  The operator ACTION set is EXACTLY the pinned set; production promotion is
       absent from it and listed under not_mounted in the matrix.

EXPLICITLY NOT PROVEN HERE (guaranteed elsewhere): that an EXISTING handler or a
registered stager/minter/adapter's implementation does not reach production
(per-action review + trust boundary); and the disposable-worker environment
allowlist (section 7.1's "no production credential in a worker"), which is
enforced and frozen on the HARNESS side with its own gate. Those are stated, not
checked vacuously across the language boundary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

REPO_ROOT = SERVER_DIR.parent

# --- the pinned surface (allowlist) -----------------------------------------
# Every operator ACTION. Adding one (however spelled) fails until it is listed
# here AND in the SHA-pinned matrix, forcing a human to confirm it is staging-
# only. An aliased `release_live` cannot slip past this.
_EXPECTED_ACTIONS = frozenset({
    "operator.read_fleet_state", "operator.read_tenant_state",
    "operator.read_jobs", "operator.read_sessions", "operator.read_audit",
    "operator.read_worker_status", "operator.worker_submit_job",
    "operator.worker_cancel_job", "operator.repo_propose_change",
    "operator.tenant_agent_pause", "operator.tenant_agent_resume",
    "operator.tenant_overlay_set", "operator.worker_credential_rotate",
    "operator.external_write", "operator.stage_release_candidate",
})

# Every operator (METHOD, PATH) endpoint. Pinning the METHOD too means a new
# production-deploying POST on an already-pinned GET path also fails.
_EXPECTED_ENDPOINTS = frozenset({
    ("GET", "/api/operator/audit"),
    ("GET", "/api/operator/external/destinations"),
    ("GET", "/api/operator/release/{target}/{source_sha}/state"),
    ("GET", "/api/operator/runbooks/credential/{handle}/state"),
    ("GET", "/api/operator/runbooks/tenant-agent/{tenant_id}/state"),
    ("GET", "/api/operator/runbooks/tenant-overlay/{tenant_id}/state"),
    ("GET", "/api/operator/secrets"),
    ("GET", "/api/operator/secrets/{handle}"),
    ("GET", "/api/operator/sessions"),
    ("GET", "/api/operator/sessions/{session_id}"),
    ("GET", "/api/operator/sessions/{session_id}/events"),
    ("POST", "/api/operator/external/execute"),
    ("POST", "/api/operator/external/propose"),
    ("POST", "/api/operator/release/execute"),
    ("POST", "/api/operator/release/propose"),
    ("POST", "/api/operator/runbooks/credential/execute"),
    ("POST", "/api/operator/runbooks/credential/propose"),
    ("POST", "/api/operator/runbooks/tenant-agent/{verb}/execute"),
    ("POST", "/api/operator/runbooks/tenant-agent/{verb}/propose"),
    ("POST", "/api/operator/runbooks/tenant-overlay/execute"),
    ("POST", "/api/operator/runbooks/tenant-overlay/propose"),
    ("POST", "/api/operator/sessions"),
    ("POST", "/api/operator/sessions/{session_id}/messages"),
})


def _app_endpoints(enabled: bool) -> list:
    """ALL (method, path) endpoints of the REAL app, imported fresh in a
    subprocess with the operator flag off/on, WITH MULTIPLICITY (a list, not a
    set). Inspecting the RUNTIME routes (not app.py's source) is what makes this
    sound: a route mounted by ANY import style, with ANY method, at ANY path
    prefix appears here; and keeping duplicates is what catches a SHADOWING
    route that registers a second handler on an already-pinned (method, path)."""
    code = (
        "import json\n"
        "import app\n"
        "eps = []\n"
        "for r in app.app.routes:\n"
        "    path = getattr(r, 'path', '')\n"
        "    methods = getattr(r, 'methods', None) or set()\n"
        "    for m in set(methods):\n"  # capture EVERY method, incl. HEAD/OPTIONS
        "        eps.append([m, path])\n"
        "    if not methods:\n"
        "        eps.append(['MOUNT', path])\n"
        "print('EPS_JSON=' + json.dumps(eps))\n"
    )
    env = dict(os.environ)
    env["LEAF_OPERATOR_ENABLED"] = "1" if enabled else "0"
    out = subprocess.run([sys.executable, "-c", code], cwd=str(SERVER_DIR),
                         capture_output=True, text=True, env=env, check=True)
    line = next(ln for ln in out.stdout.splitlines()
                if ln.startswith("EPS_JSON="))
    return [tuple(e) for e in json.loads(line[len("EPS_JSON="):])]


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
    assert not any("promote" in h or "prod" in h for h in handlers)


# --- 7.2 the route+method surface is exactly the pinned set ------------------

def test_default_app_mounts_no_operator_route():
    off = _app_endpoints(enabled=False)
    # No endpoint ANYWHERE (any prefix/method) mentions the operator namespace
    # when dark.
    assert not any("operator" in p for _, p in off), \
        sorted(p for _, p in off if "operator" in p)


def test_flag_on_adds_exactly_the_pinned_operator_endpoints():
    # Compare with MULTIPLICITY: the operator mount's contribution as a multiset.
    added = Counter(_app_endpoints(enabled=True)) - Counter(_app_endpoints(enabled=False))

    # (a) No endpoint the mount added is registered MORE THAN ONCE. A shadowing
    #     router that registers a second handler on an already-pinned
    #     (method, path) would be dispatched first by FastAPI; the duplicate is
    #     invisible to a set but shows here as count > 1.
    duplicates = {ep: c for ep, c in added.items() if c > 1}
    assert not duplicates, duplicates

    # (b) Every endpoint the mount added is operator-namespaced (nothing escaped
    #     to /internal/... or any other prefix).
    escaped = sorted(ep for ep in added if not ep[1].startswith("/api/operator/"))
    assert not escaped, escaped

    # (c) The DISTINCT added endpoints equal the pinned set exactly: a new or
    #     aliased route, or a new method on an existing path, fails here.
    assert set(added) == set(_EXPECTED_ENDPOINTS), {
        "unexpected": sorted(set(added) - set(_EXPECTED_ENDPOINTS)),
        "missing": sorted(set(_EXPECTED_ENDPOINTS) - set(added)),
    }


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
