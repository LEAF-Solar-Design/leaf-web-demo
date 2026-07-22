#!/usr/bin/env python3
"""READY/NOT-READY gate: the platform jobs lane demonstrably DENIES an
un-entitled call and allows an entitled one (the P1 entitlement floor).

The release law this enforces: the web lane stays demand-capture-only until
entitlement enforcement demonstrably denies an un-entitled call. This script
IS that demonstration, and it is binary: exit 0 (READY) only when

  1. an ENTITLED org's    POST /api/projects/{id}/jobs  kind=solve  SUCCEEDS, and
  2. an UN-ENTITLED org's POST /api/projects/{id}/jobs  kind=solve  is DENIED
     with HTTP 403 and the documented ``entitlement_required`` envelope.

ANY other outcome — including an unreachable enforcement point, a DB error, or
an un-entitled call that slips through — is NOT-READY, exit 1 (a gate that
cannot prove denial must fail closed).

Local mode (default): mounts the platform router in-process (fastapi
TestClient) against DATABASE_URL (env or platform/.env.local) — the same code
path the deployed service serves. Creates a fresh entitled org
(tier=hosted_starter) and a fresh un-entitled org (tier=restricted).

Live mode: --base-url https://platform.example [--org-header] probes a running
deployment instead. With --org-header it uses the X-Org-Id dev seam (only
works when the deployment runs LEAF_AUTH_LIVE=0); against a live-auth
deployment pass --token (a verified JWT whose org claim names an entitled org)
plus --restricted-token (one naming a restricted/un-entitled org).

Usage:
  python scripts/entitlement-gate.py                 # local, self-contained
  python scripts/entitlement-gate.py --base-url URL --org-header
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import uuid

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
_PKG_DIR = _REPO_ROOT / "platform"


def _load_leaf_platform():
    """Alias-load the platform package exactly the way platform/tests/conftest.py
    does (the package name shadows the stdlib ``platform`` module)."""
    import importlib.util

    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != str(_REPO_ROOT)]
    cur = sys.modules.get("platform")
    if cur is not None and str(getattr(cur, "__file__", "") or "").startswith(str(_PKG_DIR)):
        del sys.modules["platform"]
    import platform as _stdlib_platform  # noqa: F401  re-resolves to the stdlib

    if "leaf_platform" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", _PKG_DIR / "__init__.py",
            submodule_search_locations=[str(_PKG_DIR)],
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["leaf_platform"]


def _fail(msg: str) -> "NoReturn":  # noqa: F821
    print(f"[entitlement-gate] {msg}")
    print("NOT-READY")
    sys.exit(1)


def _check(resp_status: int, body: dict, *, expect_denied: bool, label: str) -> None:
    if expect_denied:
        if resp_status != 403:
            _fail(f"{label}: expected HTTP 403 DENIED, got {resp_status}: {body}")
        if body.get("entitlement_required") is not True:
            _fail(f"{label}: 403 without entitlement_required marker: {body}")
        print(f"[entitlement-gate] {label}: DENIED as required "
              f"(403, required={body.get('required')}, tier={body.get('tier')})")
    else:
        if resp_status != 200 or "job" not in body:
            _fail(f"{label}: expected the entitled call to succeed, got {resp_status}: {body}")
        print(f"[entitlement-gate] {label}: allowed (job {body['job']['job_id']}, "
              f"kind={body['job']['kind']})")


def run_local() -> None:
    lp = _load_leaf_platform()
    if not (os.environ.get("DATABASE_URL") or (_PKG_DIR / ".env.local").exists()):
        _fail("no DATABASE_URL (env or platform/.env.local); cannot reach the enforcement point")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from leaf_platform.api import router
    from leaf_platform import db, store
    from leaf_platform.db import cursor

    db.apply_migration()
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    def probe(org, label, *, expect_denied):
        hdr = {"X-Org-Id": str(org.org_id)}
        r_proj = client.post("/api/projects", json={"name": f"gate-{label}"}, headers=hdr)
        if r_proj.status_code != 200:
            _fail(f"{label}: project bootstrap failed ({r_proj.status_code}): {r_proj.text}")
        pid = r_proj.json()["project"]["project_id"]
        r = client.post(f"/api/projects/{pid}/jobs", json={"kind": "solve"}, headers=hdr)
        _check(r.status_code, r.json(), expect_denied=expect_denied, label=label)

    try:
        entitled = store.create_org(name=f"gate-entitled-{uuid.uuid4().hex[:8]}", tier="hosted_starter")
        probe(entitled, "entitled(hosted_starter) solve", expect_denied=False)

        unentitled = store.create_org(name=f"gate-unentitled-{uuid.uuid4().hex[:8]}")
        with cursor() as cur:
            cur.execute("UPDATE orgs SET tier = 'restricted' WHERE org_id = %(o)s",
                        {"o": unentitled.org_id})
        probe(unentitled, "un-entitled(restricted) solve", expect_denied=True)
    finally:
        db.reset_pool()


def run_remote(base_url: str, *, org_header: bool, token: str | None,
               restricted_token: str | None) -> None:
    import json
    import urllib.request

    def post(path, payload, headers):
        req = urllib.request.Request(
            base_url.rstrip("/") + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except Exception:
                return e.code, {}

    def lane(headers, label, *, expect_denied):
        st, body = post("/api/projects", {"name": f"gate-{label}"}, headers)
        if st != 200:
            _fail(f"{label}: project bootstrap failed ({st}): {body}")
        pid = body["project"]["project_id"]
        st, body = post(f"/api/projects/{pid}/jobs", {"kind": "solve"}, headers)
        _check(st, body, expect_denied=expect_denied, label=label)

    if org_header:
        # There is deliberately NO API that mints an un-entitled org (every
        # TIERS value grants solve), so a remote dev-seam gate cannot fabricate
        # the denial leg on its own — refuse up front rather than half-prove.
        _fail("remote --org-header mode needs --restricted-org-id naming an org "
              "provisioned as tier=restricted out-of-band, or run the local mode.")
    if not (token and restricted_token):
        _fail("live-auth mode needs --token (entitled org) AND --restricted-token")
    lane({"Authorization": f"Bearer {token}"}, "entitled solve", expect_denied=False)
    lane({"Authorization": f"Bearer {restricted_token}"}, "un-entitled solve", expect_denied=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", help="probe a running deployment instead of in-process")
    ap.add_argument("--org-header", action="store_true",
                    help="remote dev-seam mode (X-Org-Id header; LEAF_AUTH_LIVE=0 deployments)")
    ap.add_argument("--token", help="live-auth: JWT for an ENTITLED org")
    ap.add_argument("--restricted-token", help="live-auth: JWT for an UN-ENTITLED org")
    ap.add_argument("--restricted-org-id", help="remote dev-seam: org_id of a provisioned restricted org")
    args = ap.parse_args()

    try:
        if args.base_url:
            if args.org_header and args.restricted_org_id:
                # Both lanes via dev seam with a pre-provisioned restricted org.
                run_remote_devseam_with_restricted(args.base_url, args.restricted_org_id)
            else:
                run_remote(args.base_url, org_header=args.org_header,
                           token=args.token, restricted_token=args.restricted_token)
        else:
            run_local()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — any surprise is NOT-READY, never READY
        _fail(f"unexpected error: {type(exc).__name__}: {exc}")

    print("READY")
    sys.exit(0)


def run_remote_devseam_with_restricted(base_url: str, restricted_org_id: str) -> None:
    import json
    import urllib.request

    def post(path, payload, headers):
        req = urllib.request.Request(
            base_url.rstrip("/") + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except Exception:
                return e.code, {}

    st, body = post("/api/orgs", {"name": "gate-entitled", "tier": "hosted_starter"}, {})
    if st != 200:
        _fail(f"entitled org bootstrap failed ({st}): {body}")
    for org_id, label, denied in (
        (body["org"]["org_id"], "entitled(hosted_starter) solve", False),
        (restricted_org_id, "un-entitled(restricted) solve", True),
    ):
        hdr = {"X-Org-Id": org_id}
        st, pbody = post("/api/projects", {"name": f"gate-{label[:12]}"}, hdr)
        if st != 200:
            _fail(f"{label}: project bootstrap failed ({st}): {pbody}")
        st, jbody = post(f"/api/projects/{pbody['project']['project_id']}/jobs",
                         {"kind": "solve"}, hdr)
        _check(st, jbody, expect_denied=denied, label=label)


if __name__ == "__main__":
    main()
