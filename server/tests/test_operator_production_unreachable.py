"""Wave 4 capstone: PRODUCTION IS UNREACHABLE from the operator control plane
(contract/OPERATOR.md sections 6-7; the admin plan's five production-
reachability obligations). This gate proves ALL FIVE, across BOTH the server
operator surface AND the harness disposable worker, behaviorally where behavior
is what matters:

  O1  No production deployment credential exists in the model process OR any
      disposable worker environment. Model: the operator secret registry and
      catalog declare no production credential, and the broker REFUSES a
      production scope/environment. Worker: the harness scrubs the job env to an
      explicit ENV_ALLOWLIST that contains NO credential/secret/deploy/broker/
      cloud key, PROVEN behaviorally by harness/test/operatorWorker.test.ts
      (a planted AWS/secret canary is absent from the built job env); this gate
      pins that allowlist and asserts the behavioral test exists and is gated.
  O2  No operator manifest, allowlist, or generic handler names a production
      deploy tool or route. The operator ACTION set and the operator ROUTE
      surface (every route that is operator-authenticated OR operator-pathed,
      enumerated from the RUNTIME app) are EXACTLY the pinned sets; the
      destinations allowlist and secret registry refuse production entries.
  O3  No generic handler or executor can CALL production deploy. Behavior-
      grounded: every operator surface that takes an environment/target/
      destination REFUSES production, fail-closed (broker, allowlist, stager,
      the three write runbooks). The generic executor (the disposable worker)
      cannot reach production for two independent reasons, both proven: its env
      carries no deploy credential (O1, canary-scrubbed), so it cannot
      authenticate a production deploy; and broad command execution is FAIL-
      CLOSED on isolation - the manager REFUSES a non-isolating substrate
      (`substrate_not_isolating`), and a real isolating substrate (microVM/
      container) that enforces OS-level network isolation is a documented
      prerequisite, so production hosts + cloud metadata are denied by the jail,
      not by advisory strings. The DENIED_NETWORK_ALWAYS list is the policy
      handed to that jail; the enforcement is the isolation requirement.
  O4  Staging yields an IMMUTABLE, RECEIPTED release candidate. The candidate is
      keyed by (source_sha, target) PRIMARY KEY (one attempt per candidate) and
      CHECK-constrained to non-production; the stage-release runbook returns a
      receipt (the staged task-def revision and the rollback target); worker
      artifacts carry sha256 receipts (harness test).
  O5  Production promotion needs the canonical deploy transaction and a SEPARATE
      owner, OUTSIDE every operator surface. operator.promote_production is
      absent from the catalog and the pinned action set, listed under
      not_mounted in the matrix, and production_promotion_mounted is false, so no
      operator action or route performs promotion; and the canonical production
      deploy workflow is MANUAL (workflow_dispatch, on main) gated on an exact
      confirmation plus an INDEPENDENT approval comment (the separate owner),
      while no operator module names or triggers it.

The single honest residual, bounded not waived: trusted deployment-provided
infrastructure (a registered minter/rotator/stager/adapter, or the isolating
worker substrate) is trusted server-side code, so this gate cannot prove its OWN
body never reaches production - the same trust boundary throughout. That residual
is bounded because the plane never REQUESTS a production target (O3 refusals),
the callbacks refuse production, the worker carries no deploy credential, and a
non-isolating substrate is refused fail-closed so a real jail is a prerequisite.
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


def _operator_surface(enabled: bool) -> list:
    """The OPERATOR-SURFACE (method, path) endpoints of the REAL app, imported
    fresh in a subprocess with the operator flag off/on, WITH MULTIPLICITY.

    A route is on the operator surface if it is operator-PATHED
    (/api/operator/...) OR requires operator authentication (require_operator in
    its dependency tree). The auth arm is what makes this sound against an
    operator-authenticated production route mounted UNCONDITIONALLY and OUTSIDE
    the flag gate at any path (e.g. POST /api/deploy-production): it is captured
    regardless of path or mount, so it fails the flag-off "surface is empty"
    assertion. Runtime inspection (after startup + a full task drain), not
    source parsing, catches ANY import/mount style, method, or timing; keeping
    multiplicity catches a SHADOWING duplicate registration."""
    code = (
        "import json, asyncio\n"
        "import app\n"
        "from operator_deps import require_operator as _ro\n"
        # Run the app's startup handlers BEFORE enumerating (so a route added in
        # an @app.on_event('startup') callback via app.add_api_route is
        # captured), then DRAIN the tasks startup scheduled, in a LOOP so a
        # create_task chain that spawns descendants while draining also settles.
        "loop = asyncio.new_event_loop()\n"
        "asyncio.set_event_loop(loop)\n"
        "loop.run_until_complete(app.app.router.startup())\n"
        # The cap bounds the drain so a pathological chain cannot hang the test;
        # a chain deeper than the cap is effectively an unbounded dynamic
        # mutation (it would also stall a real server's startup), the
        # acknowledged out-of-scope residual.
        "for _ in range(1000000):\n"
        "    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]\n"
        "    if not pending:\n"
        "        break\n"
        "    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))\n"
        "def _uses_ro(route):\n"
        "    dep = getattr(route, 'dependant', None)\n"
        "    stack = [dep] if dep is not None else []\n"
        "    while stack:\n"
        "        d = stack.pop()\n"
        "        if getattr(d, 'call', None) is _ro:\n"
        "            return True\n"
        "        stack.extend(getattr(d, 'dependencies', None) or [])\n"
        "    return False\n"
        "eps = []\n"
        # RECURSE into sub-application Mounts: a Mount at /api/operator nests a
        # whole sub-app whose child routes are NOT in app.routes flatly, so a
        # production route mounted there would escape a non-recursive walk. Walk
        # child routes with the mount path as prefix.
        "def _walk(routes, prefix):\n"
        "    for r in routes:\n"
        "        rpath = prefix + getattr(r, 'path', '')\n"
        "        try:\n"
        "            subs = getattr(r, 'routes', None)\n"
        "        except Exception:\n"
        "            subs = None\n"
        "        if subs:\n"          # a Mount / sub-application
        "            _walk(subs, rpath)\n"
        "            continue\n"
        "        if not (rpath.startswith('/api/operator/') or _uses_ro(r)):\n"
        "            continue\n"
        "        methods = getattr(r, 'methods', None) or set()\n"
        "        for m in set(methods):\n"  # capture EVERY method, incl. HEAD/OPTIONS
        "            eps.append([m, rpath])\n"
        "        if not methods:\n"
        "            eps.append(['MOUNT', rpath])\n"
        "_walk(app.app.routes, '')\n"
        "print('EPS_JSON=' + json.dumps(eps))\n"
    )
    env = dict(os.environ)
    env["LEAF_OPERATOR_ENABLED"] = "1" if enabled else "0"
    # Keep the customization startup a no-op (default OFF) so running startup
    # has no side effects; the route enumeration is all we need.
    env["LEAF_CUSTOMIZATION_R5_MODE"] = "off"
    env["LEAF_CUSTOMIZATION_R6_MODE"] = "off"
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

def test_default_app_exposes_no_operator_surface():
    # DARK by default: the flag-off app has NO operator-surface endpoint at all
    # (no operator path AND no operator-authenticated route). An operator-authed
    # production route mounted unconditionally would appear here and fail.
    off = _operator_surface(enabled=False)
    assert off == [], sorted(off)


def test_flag_on_operator_surface_is_exactly_the_pinned_set():
    surface = Counter(_operator_surface(enabled=True))

    # (a) No operator-surface endpoint is registered MORE THAN ONCE. A shadowing
    #     router that registers a second handler on an already-pinned
    #     (method, path) is invisible to a set but shows here as count > 1.
    duplicates = {ep: c for ep, c in surface.items() if c > 1}
    assert not duplicates, duplicates

    # (b) The DISTINCT operator-surface endpoints equal the pinned set exactly:
    #     a new/aliased route, a new method on an existing path, or an
    #     operator-authed route at any other path all fail here.
    assert set(surface) == set(_EXPECTED_ENDPOINTS), {
        "unexpected": sorted(set(surface) - set(_EXPECTED_ENDPOINTS)),
        "missing": sorted(set(_EXPECTED_ENDPOINTS) - set(surface)),
    }
    # (c) And every pinned endpoint is operator-namespaced.
    assert all(p.startswith("/api/operator/") for _, p in _EXPECTED_ENDPOINTS)


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


# --- O1 (worker half) + O3 (generic executor): the disposable worker carries no
#     production credential and cannot reach production (harness) -------------

_HARNESS_WM = REPO_ROOT / "harness" / "src" / "operatorWorker" / "workerManager.ts"
_HARNESS_WORKER_TEST = REPO_ROOT / "harness" / "test" / "operatorWorker.test.ts"
# The EXACT set of keys allowed to cross from the manager env into a job. This is
# an ALLOWLIST-FREEZE, not a credential-name denylist: a denylist misses a
# credential under an unrecognised name (LEAF_LIVE_AUTH, PROD_AUTHZ), but pinning
# the whole set means ANY new key, whatever its name, fails until a human adds it
# here and confirms it is not a credential. Every pinned key is a benign OS path/
# locale key that holds no secret.
_EXPECTED_WORKER_ENV_ALLOWLIST = frozenset({
    "PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP",
    "PATHEXT", "WINDIR", "HOMEDRIVE", "HOMEPATH", "USERPROFILE",
})


def _ts_string_array(src: str, name: str) -> list:
    import re
    m = re.search(re.escape(name) + r"\s*=\s*\[(.*?)\]", src, re.S)
    assert m, f"could not find {name} in the harness source"
    return re.findall(r'"([^"]+)"', m.group(1))


def test_o1_worker_env_allowlist_is_exactly_the_pinned_known_safe_set():
    # The disposable-worker env is scrubbed to an explicit allowlist. Pin it
    # EXACTLY: a production deploy credential cannot cross into a worker under ANY
    # key name, because adding any key (recognised as a credential or not) fails
    # this exact-set assertion and forces human review (contract section 6, O1
    # worker half). This closes the denylist gap where LEAF_LIVE_AUTH / PROD_AUTHZ
    # would have slipped past a banned-substring check.
    allow = frozenset(_ts_string_array(
        _HARNESS_WM.read_text(encoding="utf-8"), "ENV_ALLOWLIST"))
    assert allow == _EXPECTED_WORKER_ENV_ALLOWLIST, {
        "unexpected": sorted(allow - _EXPECTED_WORKER_ENV_ALLOWLIST),
        "missing": sorted(_EXPECTED_WORKER_ENV_ALLOWLIST - allow),
    }


def test_o1_worker_network_always_denies_production_and_metadata():
    # Production hosts and cloud-metadata endpoints are ALWAYS denied and can
    # never be allowlisted back in (contract section 6; obligation O3 executor).
    deny = _ts_string_array(_HARNESS_WM.read_text(encoding="utf-8"),
                            "DENIED_NETWORK_ALWAYS")
    assert "api.leafdesign.ai" in deny   # production surface
    assert "169.254.169.254" in deny     # cloud metadata


def test_o3_worker_execution_is_fail_closed_on_isolation():
    # The REAL enforcement of worker network isolation is NOT the advisory
    # DENIED_NETWORK_ALWAYS strings: it is that broad command execution requires
    # an ISOLATING substrate. The manager refuses a non-isolating one, and the
    # only shipped substrate is explicitly non-isolating (test-only), so an
    # actual production worker must run on a real jail that enforces the deny.
    wm = _HARNESS_WM.read_text(encoding="utf-8")
    assert 'throw new Error("substrate_not_isolating")' in wm      # fail-closed refusal
    assert "if (!this.substrate.isolating && !this.allowNonIsolated)" in wm
    assert "readonly isolating = false" in wm                      # LocalProcessSubstrate is not a jail
    assert "is a prerequisite before" in wm                        # a real jail is required for O2/O3


def test_o1_o3_o4_worker_isolation_is_behaviorally_gated_in_the_harness():
    # The BEHAVIORAL proofs live in the harness vitest gate that CI runs. Assert
    # the gate exists and covers each obligation, so it cannot silently
    # disappear or be weakened.
    t = _HARNESS_WORKER_TEST.read_text(encoding="utf-8")
    # O1 worker: credential/secret/deploy/cloud keys scrubbed, canaries absent.
    assert "no credential, secret, deploy, or cloud key crosses into a job" in t
    assert "AWS_ACCESS_KEY_ID" in t and "canary-aws" in t
    assert 'expect(JSON.stringify(env)).not.toContain("canary-aws")' in t
    # O3 executor: fail-closed on isolation (the real network-deny enforcement).
    assert "submit refuses a non-isolating substrate without the explicit opt-in" in t
    assert 'rejects.toThrow("substrate_not_isolating")' in t
    assert "the local substrate is marked non-isolating" in t
    # O3 executor: always-denied network cannot be re-allowlisted (policy).
    assert "always-denied network hosts cannot be allowlisted back in" in t
    assert "api.leafdesign.ai" in t
    # O4 receipt: worker artifacts carry sha256 receipts.
    assert "sha256 receipts" in t
    assert "toMatch(/^[a-f0-9]{64}$/)" in t


# --- O4: staging yields an IMMUTABLE, RECEIPTED candidate --------------------

def test_o4_release_candidate_is_immutable_and_receipted():
    # Immutable: a composite PRIMARY KEY means one attempt per (source_sha,
    # target); a second stage of the same candidate is a no-op conflict.
    sql = (REPO_ROOT / "platform" / "migrations" /
           "0034_operator_release_candidates.sql").read_text(encoding="utf-8")
    assert "PRIMARY KEY (source_sha, target)" in sql
    # Receipted: the stage-release runbook's execute() returns the staged
    # task-def revision and the rollback target (the receipt). The BEHAVIORAL
    # proof that a real stage produces this receipt is in test_operator_stage_
    # release.py (fake-connection happy path + the needs_pg proof); here we pin
    # that the runbook constructs it.
    rb = (SERVER_DIR / "operator_stage_release_runbook.py").read_text(encoding="utf-8")
    assert '"staged_taskdef_revision": revs["new_revision"]' in rb
    assert '"reversal": {"rollback_to_taskdef_revision": previous}' in rb


# --- O5: production promotion is outside every operator surface --------------

def test_o5_production_promotion_needs_the_canonical_owner_not_the_operator():
    import operator_policy
    matrix = json.loads((REPO_ROOT / "contract" /
                         "operator_action_matrix.v1.json").read_text(encoding="utf-8"))
    # Not an action, not mounted, explicitly quarantined under not_mounted.
    assert operator_policy.get_action("operator.promote_production") is None
    assert matrix["production_promotion_mounted"] is False
    assert "operator.promote_production" in matrix["not_mounted"]
    # No operator ACTION or ROUTE performs promotion: the pinned action set and
    # the pinned (method, path) endpoint surface contain no promotion, and the
    # only release action is staging-only (O4). Production promotion therefore
    # requires the canonical deploy transaction and a separate owner, outside
    # this plane (contract section 7.3 / obligation O5).
    assert not any("promote" in a.lower() for a in _EXPECTED_ACTIONS)
    assert not any(("promote" in p.lower() or "production" in p.lower())
                   for _, p in _EXPECTED_ENDPOINTS)


_PROD_DEPLOY_WF = REPO_ROOT / ".github" / "workflows" / "deploy-platform-web-production.yml"


def test_o5_canonical_production_deploy_requires_a_separate_owner_off_the_plane():
    # The other half of O5: production promotion goes through the CANONICAL
    # deploy transaction, which requires a SEPARATE owner and is OUTSIDE every
    # operator surface. The canonical workflow is MANUAL (not operator-triggered),
    # pinned to main, and gated on an exact confirmation plus an INDEPENDENT
    # approval comment in an open issue (the separate owner).
    wf = _PROD_DEPLOY_WF.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in wf                       # manual, not operator-triggered
    assert '[ "$GITHUB_REF" = "refs/heads/main" ]' in wf    # canonical source
    assert "Validate protected production request and operator" in wf
    assert 'EXPECTED_APPROVAL="approve-vercel-production:' in wf  # separate independent approval
    assert "Independent production approval required" in wf
    # OUTSIDE every operator surface: no operator server module or router names
    # or triggers the canonical production deploy workflow or its approval token.
    op_sources = (list(SERVER_DIR.glob("operator_*.py"))
                  + list((SERVER_DIR / "routers").glob("operator_*.py")))
    assert op_sources, "expected operator source files to scan"
    for src_path in op_sources:
        s = src_path.read_text(encoding="utf-8")
        assert "deploy-platform-web-production" not in s, src_path.name
        assert "approve-vercel-production" not in s, src_path.name
