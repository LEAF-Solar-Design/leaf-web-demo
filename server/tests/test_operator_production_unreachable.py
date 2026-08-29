"""Wave 4 capstone: PRODUCTION IS UNREACHABLE from the operator control plane
(contract/OPERATOR.md sections 6-7; the admin plan's five production-
reachability obligations). This gate proves ALL FIVE, across BOTH the server
operator surface AND the harness disposable worker, behaviorally where behavior
is what matters:

  O1  No production deployment credential exists in the model process OR any
      disposable worker environment. Model: the operator secret registry and
      catalog declare no production credential, and the broker REFUSES a
      production scope/environment; AND the operator MODEL child PROCESS env is
      built from an explicit allowlist (operatorModel/operatorModelEnv.ts, the
      twin of the worker's), so a production deploy credential under ANY name -
      including one the tenant author's name-DENYLIST scrub would miss - never
      reaches the model process, PROVEN behaviorally by harness/test/
      operatorModelEnv.test.ts, with the ONLY sanctioned env source pinned to
      that builder (no vendored scrub, no parent-env pass-through). Worker: the
      harness scrubs the job env to an explicit ENV_ALLOWLIST that contains NO
      credential/secret/deploy/broker/cloud key, PROVEN behaviorally by harness/
      test/operatorWorker.test.ts (a planted AWS/secret canary is absent from the
      built job env); this gate pins that allowlist and asserts the behavioral
      test exists and is gated.
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
  O4  Staging yields an IDENTITY-IMMUTABLE, RECEIPTED release candidate. The
      candidate is keyed by (source_sha, target) PRIMARY KEY, so it is claimed at
      most once (a second stage of the same candidate is a no-op conflict, never
      an overwrite) and CHECK-constrained to non-production; the row then records
      the RECEIPT (status + the staged task-def revision + the rollback target),
      which the stage-release runbook returns; worker artifacts carry sha256
      receipts (harness test). "Immutable" is identity-immutability (no re-stage),
      not a write-once row: the runbook records the outcome into the row, which is
      the receipt, and mutating that record is the trusted-handler residual below.
  O5  Production promotion needs the canonical deploy transaction and a SEPARATE
      owner, OUTSIDE every operator surface. operator.promote_production is
      absent from the catalog and the pinned action set, listed under
      not_mounted in the matrix, and production_promotion_mounted is false, so no
      operator action or route performs promotion; and the canonical production
      deploy workflow is MANUAL (workflow_dispatch, on main) gated on an exact
      confirmation plus an exact approval comment from a repository collaborator
      (the owner separate from THIS PLANE), while no operator module names or
      triggers it. Since PR #680 that approval has two NAMED and RECORDED modes,
      `independent` (approver != dispatcher, needs write) and
      `administrator-self-authorization` (approver == dispatcher AND == the rerun
      actor, needs live ADMIN); it is deliberately NOT a two-person rule, and the
      admin requirement plus the anti-laundering check are what this gate pins.

The honest residuals, bounded not waived (no static test can close these; they
are the same trust boundary that any codebase has for its own trusted code):
  - A trusted deployment-provided callback (a registered minter/rotator/stager/
    adapter) or the isolating worker substrate could reach production in its OWN
    body (e.g. a substrate that claims isolating=true with no real jail).
  - An existing operator handler calling production is NOT a residual: O2/O3 are
    ENFORCED at runtime by the operator EGRESS BOUNDARY (operator_egress_guard),
    in two layers. Layer 1 denies the KNOWN production deploy control plane (the
    production surface, the Vercel deploy API, AWS ECS, cloud metadata) and the
    deploy CLIs for the WHOLE process UNCONDITIONALLY, so a neutral helper run
    through a fresh contextvars.Context(), a raw thread, or an executor - the
    escape a context-only guard would miss - is still denied. Layer 2 adds
    operator-context deny-by-default (loopback + DB only), closing an aliased/
    env-provided target on the innocent same-context path. Behavior, including
    the escape regression, is proven in test_operator_egress_boundary.py.
    IRREDUCIBLE in-process residual, bounded and named: a handler that
    DELIBERATELY escapes its context AND targets a host that is neither a known
    deploy route nor caught while armed (a pre-arranged alias), and any egress a
    spawned CHILD process makes, are outside an in-process hook; the complete
    boundary is the deployment's NETWORK EGRESS POLICY, for which this guard is
    defense-in-depth. The receipt-row overwrite is constrained separately (O4).
  - The operator MODEL RUNNER is GREENFIELD: operatorLoop.ts takes the runner by
    injection and NO production Agent SDK runner is wired yet. So the model-half
    obligation is enforced STRUCTURALLY, not by an existing running process: the
    ONLY non-vendored env builder (operatorModelEnv.ts) is allowlist-based AND
    pins the one injectable credential to a model-auth key (a deploy token is
    refused), and this gate fails if any operator model module builds env another
    way. What remains UNBOUNDED until the runner ships: an operator SDK runner
    placed OUTSIDE the two scanned locations, or the deployment's own ambient
    process env. (The tenant author agent's env scrub under harness/src/vendor/
    mushy-author is a DIFFERENT subsystem, not this plane.)
Each residual is bounded because the plane never REQUESTS a production target
(O3 refusals), the callbacks and runbooks refuse production, the worker carries
no deploy credential (allowlist-frozen) and is refused fail-closed on a
non-isolating substrate, and production promotion is the canonical workflow with
a separate owner, off every operator surface (O5).
"""
from __future__ import annotations

import json
import os
import re
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
    # Capability-only Lane D dispatch: validates the operator principal and
    # enqueues a bounded job to the isolated egress-locked disposable worker; it
    # executes no command in the app process and reaches no production route
    # (server/routers/operator_worker.py, proven in test_operator_worker_boundary.py).
    ("POST", "/api/operator/worker/dispatch"),
    # Exact worker cancellation (PR #746, merged 2026-08-20): operator-authed,
    # server-side owner/tenant/state authorization before any cancel. #746
    # mounted the route without amending this pin; the gate never caught it
    # because the fastapi 0.110 _IncludedRouter reshape had silently blinded
    # the route walk (fixed in this same change, with a non-vacuity floor so
    # a blind walk can never read as a pass again).
    ("POST", "/api/operator/worker/cancel"),
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
        "async def _start_lifespan():\n"
        "    lifespan = app.app.router.lifespan_context(app.app)\n"
        "    await lifespan.__aenter__()\n"
        "    return lifespan\n"
        "lifespan = loop.run_until_complete(_start_lifespan())\n"
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
        #
        # RECURSE into fastapi>=0.110 _IncludedRouter nodes too: include_router
        # no longer flattens -- it appends one opaque node per call (no .path,
        # no .methods, no .dependant) whose children hang off
        # original_router.routes and whose prefix is include_context.prefix.
        # Skipping them silently enumerates NOTHING, which makes the flag-off
        # "surface is empty" assertion vacuously true forever; the walked-leaf
        # count printed below is the non-vacuity proof the assertion pins.
        "walked = [0]\n"
        "def _walk(routes, prefix):\n"
        "    for r in routes:\n"
        "        orig = getattr(r, 'original_router', None)\n"
        "        if orig is not None:\n"  # fastapi>=0.110 _IncludedRouter
        "            ctx = getattr(r, 'include_context', None)\n"
        "            _walk(orig.routes, prefix + (getattr(ctx, 'prefix', '') or ''))\n"
        "            continue\n"
        "        rpath = prefix + getattr(r, 'path', '')\n"
        "        try:\n"
        "            subs = getattr(r, 'routes', None)\n"
        "        except Exception:\n"
        "            subs = None\n"
        "        if subs:\n"          # a Mount / sub-application
        "            _walk(subs, rpath)\n"
        "            continue\n"
        "        walked[0] += 1\n"
        "        if not (rpath.startswith('/api/operator/') or _uses_ro(r)):\n"
        "            continue\n"
        "        methods = getattr(r, 'methods', None) or set()\n"
        "        for m in set(methods):\n"  # capture EVERY method, incl. HEAD/OPTIONS
        "            eps.append([m, rpath])\n"
        "        if not methods:\n"
        "            eps.append(['MOUNT', rpath])\n"
        "_walk(app.app.routes, '')\n"
        "print('EPS_JSON=' + json.dumps(eps))\n"
        "print('WALKED_TOTAL=' + str(walked[0]))\n"
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
    walked_line = next(ln for ln in out.stdout.splitlines()
                       if ln.startswith("WALKED_TOTAL="))
    # NON-VACUITY GATE, load-bearing: if a routing-library change makes the
    # walk skip containers it does not recognize (exactly what fastapi 0.110's
    # _IncludedRouter did), "found no operator surface" and "enumerated
    # nothing" become indistinguishable and the flag-off proof below certifies
    # a surface it never audited. The real app registers far more than 50 leaf
    # routes on every stack this repo has shipped, so a count below that floor
    # is an enumeration break, never a real app shape.
    walked_total = int(walked_line[len("WALKED_TOTAL="):])
    assert walked_total > 50, (
        f"route walk enumerated only {walked_total} leaf routes; the walk is "
        "broken (unrecognized router container?), so surface assertions built "
        "on it would be vacuous. Fix the walk, do not touch the pinned set.")
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


# --- O1 (model half): the operator MODEL child process carries no production
#     deploy credential (harness) -------------------------------------------
#
# The worker half (above) proves the disposable worker env is allowlist-frozen.
# The model half closes the symmetric gap: the operator loop's injectable runner
# (harness/src/agent/operatorLoop.ts) launches an Agent SDK model child, and that
# child's env must ALSO be allowlist-built — NOT scrubbed by the tenant author's
# name-DENYLIST (buildScrubbedEnv / scrubSecrets in the vendored mushy-author
# code), which a production credential under an unrecognised name (LEAF_LIVE_
# ACCESS, PROD_AUTHZ) would slip past. The non-vendored builder
# operatorModel/operatorModelEnv.ts mirrors the worker ENV_ALLOWLIST, and the
# BEHAVIORAL mutation check lives in harness/test/operatorModelEnv.test.ts.

_HARNESS_MODEL_ENV = (REPO_ROOT / "harness" / "src" / "operatorModel" /
                      "operatorModelEnv.ts")
_HARNESS_MODEL_ENV_TEST = (REPO_ROOT / "harness" / "test" /
                           "operatorModelEnv.test.ts")
_HARNESS_OPERATOR_LOOP = (REPO_ROOT / "harness" / "src" / "agent" /
                          "operatorLoop.ts")
# Same benign OS-key set as the worker: an allowlist-freeze, no credential key.
_EXPECTED_MODEL_ENV_ALLOWLIST = frozenset({
    "PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP",
    "PATHEXT", "WINDIR", "HOMEDRIVE", "HOMEPATH", "USERPROFILE",
})
# A key on the model allowlist must be a benign OS/path key. If a future edit
# adds a credential-shaped key (even while also updating the pin), this trips.
import re as _re_o1model
_CRED_NAME_RE = _re_o1model.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|APIKEY|API_KEY|_KEY|AUTH|JWT|"
    r"ACCESS|LIVE|PROD|DEPLOY)", _re_o1model.I)


def _strip_ts_comments(src: str) -> str:
    # Remove /* ... */ blocks and // line comments so the CODE (not the prose
    # that documents why the banned constructs are banned) is what gets scanned.
    # The (?<!:) guard preserves URLs like https:// inside string literals.
    no_block = _re_o1model.sub(r"/\*.*?\*/", "", src, flags=_re_o1model.S)
    return _re_o1model.sub(r"(?<!:)//.*", "", no_block)


def test_o1_model_env_allowlist_is_exactly_the_pinned_known_safe_set():
    # The operator MODEL child env is scrubbed to an explicit allowlist. Pin it
    # EXACTLY: a production deploy credential cannot cross into the model process
    # under ANY key name, because adding any key fails this exact-set assertion
    # and forces human review (contract section 6, O1 model half). This is the
    # symmetric twin of the worker allowlist-freeze; together they close the
    # denylist gap on BOTH generative surfaces (worker AND model).
    allow = frozenset(_ts_string_array(
        _HARNESS_MODEL_ENV.read_text(encoding="utf-8"),
        "OPERATOR_MODEL_ENV_ALLOWLIST"))
    assert allow == _EXPECTED_MODEL_ENV_ALLOWLIST, {
        "unexpected": sorted(allow - _EXPECTED_MODEL_ENV_ALLOWLIST),
        "missing": sorted(_EXPECTED_MODEL_ENV_ALLOWLIST - allow),
    }
    # No allowlisted key is credential-shaped (belt and suspenders: a credential
    # added to the allowlist fails even if its pin above were also updated).
    bad = [k for k in allow if _CRED_NAME_RE.search(k)]
    assert not bad, bad


def test_o1_model_env_source_is_the_allowlist_builder_not_the_tenant_scrub():
    # MUTATION CHECK: the ONLY sanctioned source for the operator model child env
    # is buildOperatorModelEnv. No operator model-launch module may build that
    # env from the vendored tenant name-denylist (buildScrubbedEnv / scrubSecrets)
    # or by passing the parent env through (a `...process.env` spread). If a
    # future runner is wired that way — the exact production-reaching change under
    # review — this scan FAILS.
    model_modules = [_HARNESS_OPERATOR_LOOP] + sorted(
        (REPO_ROOT / "harness" / "src" / "operatorModel").glob("*.ts"))
    assert _HARNESS_MODEL_ENV in model_modules, "the builder must be scanned"
    banned = ("buildScrubbedEnv", "scrubSecrets", "...process.env")
    hits = []
    for mod in model_modules:
        s = _strip_ts_comments(mod.read_text(encoding="utf-8"))
        for tok in banned:
            if tok in s:
                hits.append((mod.name, tok))
    assert not hits, hits
    # The builder itself is parameterized (takes the parent env as an argument)
    # and never reads process.env, so it cannot silently re-widen the source.
    builder = _strip_ts_comments(_HARNESS_MODEL_ENV.read_text(encoding="utf-8"))
    assert "process.env" not in builder, "builder must take parentEnv as a param"
    assert "OPERATOR_MODEL_ENV_ALLOWLIST" in builder
    # It does not import the vendored scrub.
    assert "envScrub" not in builder and "mushy-author" not in builder
    # CREDENTIAL PIN: the one injected credential must be the pinned model-auth
    # key. Without this, a caller could hand the builder a PRODUCTION DEPLOY token
    # (VERCEL_TOKEN, an AWS deploy key) as "the model credential" and it would
    # reach the model process through the sanctioned injection path. The builder
    # refuses any other key.
    assert ("grant.credentialKey !== OPERATOR_MODEL_CREDENTIAL_KEY" in builder
            and 'throw new Error("operator_model_credential_key_not_allowed")'
            in builder), "builder must pin the injected credential key"
    # The pinned key is a model-auth key, not a deploy key.
    m = _re_o1model.search(
        r'OPERATOR_MODEL_CREDENTIAL_KEY\s*=\s*"([^"]+)"', builder)
    assert m, "OPERATOR_MODEL_CREDENTIAL_KEY must be a pinned string constant"
    assert not _re_o1model.search(r"(VERCEL|DEPLOY|LIVE|PROD)", m.group(1),
                                  _re_o1model.I), m.group(1)


def test_o1_model_env_is_behaviorally_gated_in_the_harness():
    # The BEHAVIORAL proof (the builder actually strips an unknown-named
    # credential) lives in the harness vitest gate that CI runs. Assert it exists
    # and plants the credentials, so it cannot silently disappear or be weakened.
    t = _HARNESS_MODEL_ENV_TEST.read_text(encoding="utf-8")
    assert "strips unknown-named production deploy credentials" in t
    # The planted credentials whose NAMES the tenant denylist would miss.
    assert "LEAF_LIVE_ACCESS" in t and "PROD_AUTHZ" in t
    assert 'expect(env).not.toHaveProperty("LEAF_LIVE_ACCESS")' in t
    assert 'expect(serialized).not.toContain("api.leafdesign.ai")' in t
    # And it proves ONLY allowlisted keys + the one injected credential survive.
    assert "ONLY allowlisted OS keys plus the one injected model credential" in t
    # And it proves the builder REFUSES a production deploy token as the grant.
    assert "REFUSES to inject a production deploy token as the model credential" in t
    assert 'toThrow("operator_model_credential_key_not_allowed")' in t


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
    # IDENTITY-IMMUTABLE (mutation check): every UPDATE of the candidate table
    # may write ONLY the receipt fields (status / revisions / timestamps), NEVER
    # the identity columns source_sha or target. If a future edit adds
    # `SET source_sha=` or `SET target=` (mutating the immutable identity), the
    # SET clause below contains it and this fails. Quotes are stripped first so a
    # QUOTED identifier (SET "target" =) cannot slip past, and UPDATE ONLY /
    # a quoted table name are matched.
    import re

    def _dequote(s: str) -> str:
        return s.replace('"', "").replace("`", "")

    rb_dq = _dequote(rb)
    updated = False
    for m in re.finditer(
            r"UPDATE\s+(?:ONLY\s+)?operator_release_candidates\s+SET\s+(.*?)\s+WHERE",
            rb_dq, re.S | re.I):
        updated = True
        set_clause = m.group(1)
        assert not re.search(r"\bsource_sha\s*=", set_clause), set_clause
        assert re.search(r"\btarget\s*=", set_clause) is None, set_clause
    assert updated, "expected at least one UPDATE of the candidate table (non-vacuous)"
    # DB-ENFORCED immutability: identity is protected at the schema level, not by
    # code discipline. A migration defines a BEFORE UPDATE trigger that REJECTS a
    # change to source_sha/target. Any trigger/rule on the table must be
    # PROTECTIVE (READs the identity columns and RAISEs), never REWRITING (an
    # assignment `NEW.source_sha :=` / `NEW.target :=` would mutate the reviewed
    # identity). Scan EVERY migration.
    mig_dir = REPO_ROOT / "platform" / "migrations"
    protective_trigger_found = False
    for mig in sorted(mig_dir.glob("*.sql")):
        text = mig.read_text(encoding="utf-8")
        up = _dequote(text).upper()
        if "OPERATOR_RELEASE_CANDIDATES" not in up:
            continue
        # A CREATE RULE is never used here (a rule can silently rewrite/suppress).
        assert "CREATE RULE" not in up, mig.name
        if "CREATE TRIGGER" in up:
            # A protective trigger never ASSIGNS the identity columns.
            assert not re.search(r"NEW\.source_sha\s*:?=", text), mig.name
            assert not re.search(r"NEW\.target\s*:?=", text), mig.name
            # ...and it RAISEs on an identity change (the protection).
            assert "RAISE EXCEPTION" in up and "IS DISTINCT FROM" in up, mig.name
            protective_trigger_found = True
    assert protective_trigger_found, (
        "expected a migration with a protective identity-immutability trigger")
    # The BEHAVIORAL proof against the FULLY MIGRATED schema (an attempted UPDATE
    # of source_sha/target is rejected by the DB) is the needs_pg test
    # test_pg_candidate_identity_is_db_immutable in test_operator_stage_release.py.
    st = (SERVER_DIR / "tests" / "test_operator_stage_release.py").read_text(encoding="utf-8")
    assert "def test_pg_candidate_identity_is_db_immutable" in st


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
    # deploy transaction, which is OUTSIDE every operator surface. The canonical
    # workflow is MANUAL (not operator-triggered), pinned to main, and gated on
    # an exact confirmation plus an exact approval comment in an open issue.
    #
    # SEPARATE OWNER, as the contract means it (section 7, obligation 3): the
    # owner is separate from THIS PLANE. It is a repository collaborator acting
    # through GitHub, which the operator control plane cannot be and cannot
    # reach. It is NOT a two-person rule: PR #680 (4f8a5f71, 2026-08-18)
    # deliberately retired the bare `[ "$APPROVER" != "$ACTOR" ]` pair, which
    # made a single-approver release undeployable, and replaced it with two
    # NAMED and RECORDED modes, porting the mode the infra repo already proves
    # in accept-leaf-platform-staging-authored-cad.yml:
    #
    #   independent                       approver != dispatcher, needs write
    #   administrator-self-authorization  approver == dispatcher, needs live ADMIN
    #
    # These pins mirror scripts/test_production_web_release.py, which #680
    # re-pinned in the same PR (this file was missed because no CI workflow and
    # no run-all-gates.py Suite executes it).
    wf = _PROD_DEPLOY_WF.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in wf                       # manual, not operator-triggered
    assert '[ "$GITHUB_REF" = "refs/heads/main" ]' in wf    # canonical source
    assert "Validate protected production request and operator" in wf
    assert 'EXPECTED_APPROVAL="approve-vercel-production:' in wf  # exact approval payload
    assert ("Production approval required (independent approver, or a "
            "repository administrator self-authorizing)") in wf
    # The administrator requirement and the anti-laundering check, enforced not
    # just present, at BOTH enforcement points (admission and promotion).
    # Dropping any one of these would silently let a non-admin, or a rerun by a
    # different actor, self-authorize a production deploy.
    assert "administrator-self-authorization" in wf
    assert "Self-authorization requires live repository admin permission." in wf
    assert ("Self-authorization requires the dispatcher and rerun actor to be "
            "the same administrator.") in wf
    assert ("Self-authorization requires live repository admin permission at "
            "promotion.") in wf
    assert "Approval mode changed between admission" in wf
    # A mutation that defeats a check with `|| true` / `|| :` is caught here,
    # and a missing exit is caught by requiring the fail-closed `exit 1`.
    assert "|| true" not in wf and "|| :" not in wf   # no tautology defeat
    assert "exit 1" in wf                             # fail-closed on a bad request
    # OUTSIDE every operator surface: no operator server module or router names
    # or triggers the canonical production deploy workflow or its approval token.
    op_sources = (list(SERVER_DIR.glob("operator_*.py"))
                  + list((SERVER_DIR / "routers").glob("operator_*.py")))
    assert op_sources, "expected operator source files to scan"
    for src_path in op_sources:
        s = src_path.read_text(encoding="utf-8")
        assert "deploy-platform-web-production" not in s, src_path.name
        assert "approve-vercel-production" not in s, src_path.name


_APPROVAL_MODE_IF = ('if [ "${APPROVER,,}" = "${ACTOR,,}" ] '
                     '|| [ "${APPROVER,,}" = "${TRIGGERING_ACTOR,,}" ]; then')


def _extract_approval_mode_blocks(wf: str) -> list:
    """Every `if ... fi` approval-mode branch, lifted VERBATIM from the workflow.

    Indentation-delimited: the branch ends at the first `fi` sitting at the same
    column as its `if`. Nothing is rewritten, so a tautology defeat or a dropped
    admin check inside the branch is carried into the executed script.
    """
    lines = wf.splitlines()
    blocks = []
    for i, line in enumerate(lines):
        if line.strip() != _APPROVAL_MODE_IF:
            continue
        indent = len(line) - len(line.lstrip())
        for j in range(i + 1, len(lines)):
            candidate = lines[j]
            if (candidate.strip() == "fi"
                    and len(candidate) - len(candidate.lstrip()) == indent):
                blocks.append("\n".join(lines[i:j + 1]))
                break
        else:
            raise AssertionError(f"unterminated approval-mode branch at line {i + 1}")
    return blocks


def test_o5_self_approval_is_behaviorally_admin_only():
    # BEHAVIORAL: EXECUTE the canonical workflow's OWN approval-mode branch (not
    # a hand-copy) and prove what the post-#680 contract actually enforces:
    #
    #   - self-approval WITHOUT live repository admin is REJECTED;
    #   - a rerun by a different actor cannot launder a self-approval;
    #   - self-approval BY an administrator is accepted, and is RECORDED as
    #     administrator-self-authorization (never as independent);
    #   - an independent approver with write is accepted as independent;
    #   - an independent approver below write is REJECTED.
    #
    # The branch is lifted verbatim from the workflow and run under
    # `set -euo pipefail`, exactly as the workflow runs it, so a `|| true`
    # tautology defeat or a dropped admin check is executed too and makes a bad
    # request wrongly pass -> this test then FAILS, catching the mutation.
    import shutil
    import textwrap
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash unavailable")
    wf = _PROD_DEPLOY_WF.read_text(encoding="utf-8")
    blocks = _extract_approval_mode_blocks(wf)
    # BOTH enforcement points carry the branch: admission (APPROVER_PERMISSION)
    # and promotion (PERMISSION, re-derived against freshly fetched permission).
    assert len(blocks) == 2, f"expected admission + promotion branches, got {len(blocks)}"
    admission, promotion = blocks
    assert "APPROVAL_MODE=" in admission and "APPROVER_PERMISSION" in admission
    assert "FINAL_APPROVAL_MODE=" in promotion

    def run(block, mode_var, perm_var, approver, actor, triggering, permission):
        script = ("set -euo pipefail\n" + textwrap.dedent(block)
                  + f'\necho "MODE=${mode_var}"\n')
        proc = subprocess.run(
            [bash, "-c", script],
            env={"APPROVER": approver, "ACTOR": actor,
                 "TRIGGERING_ACTOR": triggering, perm_var: permission,
                 "PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True)
        mode = ""
        for line in proc.stdout.splitlines():
            if line.startswith("MODE="):
                mode = line[len("MODE="):]
        return proc.returncode, mode

    for block, mode_var, perm_var in ((admission, "APPROVAL_MODE", "APPROVER_PERMISSION"),
                                      (promotion, "FINAL_APPROVAL_MODE", "PERMISSION")):
        # Self-approval on write/maintain is REFUSED: admin is the price of
        # approving your own dispatch.
        for weak in ("write", "maintain"):
            rc, _ = run(block, mode_var, perm_var, "alice", "alice", "alice", weak)
            assert rc != 0, f"self-approval on {weak} must be rejected"
        # Laundering: the approver is the dispatcher but the RERUN actor is
        # someone else (or the reverse), so the two identities disagree.
        for actor, triggering in (("alice", "bob"), ("bob", "alice")):
            rc, _ = run(block, mode_var, perm_var, "alice", actor, triggering, "admin")
            assert rc != 0, "a rerun by a different actor must not launder self-approval"
        # Self-approval BY an administrator is the sanctioned second mode, and
        # it is RECORDED as such - never laundered into "independent".
        rc, mode = run(block, mode_var, perm_var, "alice", "alice", "alice", "admin")
        assert rc == 0, "an administrator self-authorizing must pass"
        assert mode == "administrator-self-authorization", mode
        # Case-insensitivity is part of the contract: a login differing only in
        # case is the SAME person and must not read as independent.
        rc, mode = run(block, mode_var, perm_var, "Alice", "alice", "ALICE", "admin")
        assert rc == 0 and mode == "administrator-self-authorization", (rc, mode)
        rc, _ = run(block, mode_var, perm_var, "Alice", "alice", "ALICE", "write")
        assert rc != 0, "case-differing self-approval on write must be rejected"
        # An independent approver passes and is recorded as independent.
        rc, mode = run(block, mode_var, perm_var, "carol", "alice", "bob", "write")
        assert rc == 0, "an independent approver with write must pass"
        assert mode == "independent", mode

    # Only the ADMISSION branch screens the independent approver's permission
    # (promotion pre-filters it with a jq `select`, then re-derives the mode).
    rc, _ = run(admission, "APPROVAL_MODE", "APPROVER_PERMISSION",
                "carol", "alice", "bob", "read")
    assert rc != 0, "an independent approver below write must be rejected"


# --- O3 (mutation check): no operator handler names a production deploy target

# A handler that CALLED production deploy would have to reference a production
# deploy target: the production host, the Vercel deploy platform, the canonical
# production workflow or its approval token, or a cloud-metadata endpoint. None
# appears in any operator server module today (they use "production" only to
# REFUSE it), so adding such a call to an existing handler trips this scan.
_PROD_DEPLOY_CALL_TOKENS = (
    "api.leafdesign.ai",                 # production surface / host
    "vercel",                            # the production deploy platform
    "deploy-platform-web-production",    # the canonical production workflow
    "approve-vercel-production",         # its independent approval token
    "169.254.169.254",                   # cloud metadata endpoint
    "update_service",                    # AWS ECS deploy (defense-in-depth)
    "boto3",                             # AWS SDK deploy path (defense-in-depth)
)
# The EGRESS BOUNDARY (operator_egress_guard.py) is the enforcement; it names
# these routes precisely in order to DENY them, so it is excluded from the scan.
_EGRESS_GUARD_FILE = "operator_egress_guard.py"


def test_o3_no_operator_handler_names_a_production_deploy_target():
    # Defense-in-depth ONLY (the real enforcement is the egress boundary below):
    # no operator handler even names a production deploy route today.
    op_sources = (list(SERVER_DIR.glob("operator_*.py"))
                  + list((SERVER_DIR / "routers").glob("operator_*.py")))
    op_sources = [p for p in op_sources if p.name != _EGRESS_GUARD_FILE]
    assert op_sources, "expected operator source files to scan"
    hits = []
    for src_path in op_sources:
        low = src_path.read_text(encoding="utf-8").lower()
        for tok in _PROD_DEPLOY_CALL_TOKENS:
            if tok.lower() in low:
                hits.append((src_path.name, tok))
    assert not hits, hits


# --- O2/O3 ENFORCED: a generic operator handler CANNOT CALL a production deploy
#     route. This is the real enforcement the scan above cannot provide (a
#     handler can call a neutral helper in any file). The egress boundary denies
#     the call at the socket/subprocess chokepoint, armed for every operator
#     handler by require_operator. Behavior is proven in
#     server/tests/test_operator_egress_boundary.py. ------------------------
_EGRESS_GUARD = SERVER_DIR / "operator_egress_guard.py"
_EGRESS_TEST = SERVER_DIR / "tests" / "test_operator_egress_boundary.py"


def test_o2_o3_generic_handler_cannot_reach_a_production_deploy_route():
    g = _EGRESS_GUARD.read_text(encoding="utf-8")
    # Enforced at the process audit-hook chokepoint (catches httpx/boto3/raw
    # sockets AND the vercel/aws CLIs; audit hooks cannot be uninstalled).
    assert "sys.addaudithook" in g
    assert "socket.connect" in g and "socket.getaddrinfo" in g
    assert "subprocess.Popen" in g            # deploy-CLI spawn denied
    assert "OperatorEgressDenied" in g
    # LAYER 1 - UNCONDITIONAL (no context to escape via a fresh context / raw
    # thread / executor): the known production deploy control plane + deploy CLIs
    # are denied for the whole process, always.
    assert "_DEPLOY_HOST_EXACT" in g and "_is_deploy_control_plane" in g
    assert "_DEPLOY_CLIS" in g
    for endpoint in ("api.leafdesign.ai", "vercel", "ecs", "169.254.169.254"):
        assert endpoint in g, endpoint
    # LAYER 2 - operator-context DENY-BY-DEFAULT (an aliased / env-provided target
    # on the innocent same-context path; only loopback + DB + a declared extra).
    assert "_LOOPBACK" in g and "LEAF_OPERATOR_EGRESS_ALLOW" in g
    # WIRED into the real request path: require_operator arms it for every
    # operator handler (not an unused helper).
    deps = (SERVER_DIR / "operator_deps.py").read_text(encoding="utf-8")
    assert "from operator_egress_guard import operator_execution" in deps
    assert "with operator_execution():" in deps
    assert "yield ctx" in deps
    # The BEHAVIORAL proof exists and is gated, INCLUDING the regression test for
    # the context/thread/executor escape (a real deploy route stays denied even
    # from a fresh context - Layer 1 has none to escape).
    t = _EGRESS_TEST.read_text(encoding="utf-8")
    assert "test_neutral_ship_helper_to_vercel_is_denied" in t
    assert "test_deploy_control_plane_denied_without_arming" in t
    assert "test_deploy_route_denied_across_context_escapes" in t
    assert "test_deploy_cli_spawn_denied_unconditionally" in t
    assert "test_require_operator_arms_the_egress_boundary" in t
