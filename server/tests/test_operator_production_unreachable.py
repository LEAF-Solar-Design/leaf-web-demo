"""Wave 4 capstone: production is UNREACHABLE from every operator surface
(contract/OPERATOR.md section 7).

Per-action reviews checked this one action at a time; this proves it across the
WHOLE operator control plane at once, so a future action that quietly reaches
production fails here. Three conditions of section 7:

  7.1  No operator surface accepts a production environment/target: the secret
       broker, the external-write allowlist, the release stager, and all three
       write runbooks refuse production, fail-closed.
  7.2  No operator manifest, route, or handler names a production deploy path:
       the default app mounts no /api/operator route, and with the operator
       flag ON every mounted route is under /api/operator/ and none names
       production/promote/deploy.
  7.3  Production promotion is NOT an action: operator.promote_production is
       absent from the catalog and listed under not_mounted in the matrix;
       release preparation stops at the staging-only stage_release_candidate.

No DB is required: every check is structural or a fail-closed refusal path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

REPO_ROOT = SERVER_DIR.parent
_NON_PRODUCTION = {"staging", "development"}
_PROD_WORDS = ("production", "promote", "prod-deploy", "deploy-platform")

# Every operator router that _mount_operator_router() mounts under the flag.
_OPERATOR_ROUTER_MODULES = [
    "operator_sessions", "operator_runbooks", "operator_overlay",
    "operator_secrets", "operator_credential", "operator_external",
    "operator_release",
]


# --- 7.3 promotion is not an action -----------------------------------------

def test_promote_production_is_not_a_catalog_action():
    import operator_policy
    assert operator_policy.get_action("operator.promote_production") is None


def test_matrix_lists_promotion_as_not_mounted_only():
    matrix = json.loads((REPO_ROOT / "contract" /
                         "operator_action_matrix.v1.json").read_text(encoding="utf-8"))
    assert matrix["production_promotion_mounted"] is False
    assert "operator.promote_production" in matrix["not_mounted"]
    # A not_mounted action never appears as a real, production-reachable action.
    assert "operator.promote_production" not in matrix["actions"]
    assert all(a.get("production_reachable") is False
               for a in matrix["actions"].values())


def test_catalog_declares_no_production_reachable_handler():
    import operator_policy
    catalog = operator_policy.load_catalog()
    for name, entry in catalog["actions"].items():
        # The parser already refuses production-shaped content except the
        # staging-only release action; assert none is a promotion.
        assert "promote_production" not in entry["handler"], name


# --- 7.2 no production route named ------------------------------------------

def test_default_app_mounts_no_operator_route():
    import app
    assert [r.path for r in app.app.routes if "/api/operator" in r.path] == []


def test_flag_on_surface_is_operator_namespaced_and_names_no_production():
    from fastapi import FastAPI
    app = FastAPI()
    for mod_name in _OPERATOR_ROUTER_MODULES:
        mod = __import__(f"routers.{mod_name}", fromlist=["router"])
        app.include_router(mod.router)
    paths = [r.path for r in app.routes if r.path.startswith("/api/")]
    assert paths, "expected the operator routers to register routes"
    for p in paths:
        assert p.startswith("/api/operator/"), p
        low = p.lower()
        assert not any(w in low for w in _PROD_WORDS), p


def test_operator_router_source_names_no_production_deploy_route():
    for mod_name in _OPERATOR_ROUTER_MODULES:
        src = (SERVER_DIR / "routers" / f"{mod_name}.py").read_text(encoding="utf-8")
        low = src.lower()
        # "production" may only appear in a REFUSAL context, never as a route
        # or a deploy target. Assert no route decorator path names production.
        for line in src.splitlines():
            if "@router." in line:
                assert not any(w in line.lower() for w in _PROD_WORDS), (mod_name, line)


# --- 7.1 every operator surface refuses production --------------------------

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
    # Each write runbook's server-owned precondition refuses production.
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
    # The candidate table forbids a production row at the schema level, so a
    # production candidate is unrepresentable even if a bug reached the INSERT.
    sql = (REPO_ROOT / "platform" / "migrations" /
           "0034_operator_release_candidates.sql").read_text(encoding="utf-8")
    assert "CHECK (target IN ('staging', 'development'))" in sql
    # 'production' must NOT appear in any executable statement (comments only).
    statements = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--"))
    assert "production" not in statements.lower()
