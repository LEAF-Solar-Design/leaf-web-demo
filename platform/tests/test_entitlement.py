"""Tier-branching entitlement enforcement on the jobs lane (the P1 floor).

The un-entitled cases here are the release-law evidence: an org whose tier
grants nothing (unknown tier / restricted) or whose status is not active is
DENIED at POST /api/projects/{id}/jobs with the documented
``entitlement_required`` envelope, while entitled orgs proceed. Branching
semantics are pinned against a TEST policy file (LEAF_ENTITLEMENTS_FILE) so
an operator retune of the shipped entitlements.json cannot flake these tests;
the fail-closed floor (unknown tier -> restricted) is asserted without any
policy file dependence.
"""
import json

import pytest

from leaf_platform.db import cursor


def _set_org(org_id, *, tier=None, status=None):
    sets, params = [], {"org_id": org_id}
    if tier is not None:
        sets.append("tier = %(tier)s")
        params["tier"] = tier
    if status is not None:
        sets.append("status = %(status)s")
        params["status"] = status
    with cursor() as cur:
        cur.execute(f"UPDATE orgs SET {', '.join(sets)} WHERE org_id = %(org_id)s", params)


def _make_project(client, org):
    hdr = {"X-Org-Id": str(org.org_id)}
    pid = client.post("/api/projects", json={"name": "Ent"}, headers=hdr).json()["project"]["project_id"]
    return hdr, pid


def _post_job(client, hdr, pid, kind, **extra):
    return client.post(f"/api/projects/{pid}/jobs", json={"kind": kind, **extra}, headers=hdr)


@pytest.fixture
def pinned_policy(tmp_path, monkeypatch):
    """A deterministic tier policy so branching tests survive operator retunes."""
    policy = {
        "hosted_starter": {"run_read": True, "run_write": True, "build": False},
        "hosted_pro": {"run_read": True, "run_write": True, "build": True},
        "restricted": {"run_read": True, "run_write": False, "build": False},
    }
    p = tmp_path / "entitlements.json"
    p.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(p))
    return policy


def test_entitled_org_solve_succeeds(client, make_org, pinned_policy):
    org = make_org(name="Entitled Org", tier="hosted_starter")
    hdr, pid = _make_project(client, org)
    r = _post_job(client, hdr, pid, "solve")
    assert r.status_code == 200, r.text
    assert r.json()["job"]["kind"] == "solve"


def test_unknown_tier_is_denied_fail_closed(client, make_org):
    """No policy-file dependence: an unknown tier falls to the hardcoded
    restricted floor and run_write is denied."""
    org = make_org(name="Mystery Org")
    _set_org(org.org_id, tier="mystery_tier")
    hdr, pid = _make_project(client, org)
    r = _post_job(client, hdr, pid, "solve")
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["entitlement_required"] is True
    assert body["required"] == "run_write"
    assert body["tier"] == "mystery_tier"
    assert body["error"]["error_code"] == "ENTITLEMENT_REQUIRED"


def test_restricted_tier_denies_write_allows_read(client, make_org, pinned_policy):
    org = make_org(name="Restricted Org")
    _set_org(org.org_id, tier="restricted")
    hdr, pid = _make_project(client, org)
    assert _post_job(client, hdr, pid, "solve").status_code == 403
    assert _post_job(client, hdr, pid, "run").status_code == 403
    r_read = _post_job(client, hdr, pid, "extract")
    assert r_read.status_code == 200, r_read.text


def test_tier_branching_build(client, make_org, pinned_policy):
    """The same request branches on tier: starter denied, pro allowed."""
    starter = make_org(name="Starter", tier="hosted_starter")
    hdr_s, pid_s = _make_project(client, starter)
    r_s = _post_job(client, hdr_s, pid_s, "build")
    assert r_s.status_code == 403
    assert r_s.json()["required"] == "build"

    pro = make_org(name="Pro", tier="hosted_pro")
    hdr_p, pid_p = _make_project(client, pro)
    assert _post_job(client, hdr_p, pid_p, "build").status_code == 200


def test_inactive_org_is_denied(client, make_org, pinned_policy):
    org = make_org(name="Offboarding Org", tier="hosted_pro")
    hdr, pid = _make_project(client, org)
    _set_org(org.org_id, status="offboarding")
    r = _post_job(client, hdr, pid, "solve")
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["entitlement_required"] is True
    assert body["required"] == "org_active"
    assert body["error"]["error_code"] == "ENTITLEMENT_REQUIRED"


def test_denial_envelope_shape(client, make_org, pinned_policy):
    """The 403 body mirrors the server lane's entitlement_denied_response."""
    org = make_org(name="Envelope Org")
    _set_org(org.org_id, tier="restricted")
    hdr, pid = _make_project(client, org)
    body = _post_job(client, hdr, pid, "solve").json()
    assert set(body) >= {"entitlement_required", "required", "tier", "error", "degraded_mode"}
    assert body["error"]["retryable"] is False
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_cross_org_probe_still_404_when_unentitled(client, make_org, pinned_policy):
    """Ownership 404 wins over entitlement 403: a cross-org probe by an
    un-entitled caller learns nothing (no existence leak, no tier echo)."""
    victim = make_org(name="Victim Org")
    _hdr_v, pid_v = _make_project(client, victim)
    prober = make_org(name="Prober Org")
    _set_org(prober.org_id, tier="restricted")
    hdr_p = {"X-Org-Id": str(prober.org_id)}
    r = client.post(f"/api/projects/{pid_v}/jobs", json={"kind": "solve"}, headers=hdr_p)
    assert r.status_code == 404
    assert "entitlement_required" not in r.json()


def test_missing_org_row_is_denied(pinned_policy):
    """A missing orgs row is refused, never allowed — asserted at the
    enforcement function directly because the HTTP route can't reach this
    state (projects carry a hard FK to orgs, so an unprovisioned org has no
    project to post jobs against)."""
    from leaf_platform.entitlements import job_entitlement_denial

    denial = job_entitlement_denial(None, "solve")
    assert denial is not None and denial.status_code == 403
    body = json.loads(denial.body)
    assert body["entitlement_required"] is True
    assert body["required"] == "org_active"
    assert body["tier"] == "restricted"


# --------------------------------------------------------------------------- #
# canonical submission choke point (POST /api/run spine path) — the STORED
# org's tier/status decides at submit_solve_job itself, so no caller (UI,
# spine, off-auth demo tenant) can bypass the floor with a permissive
# request-side tier.
# --------------------------------------------------------------------------- #
def _canonical_project(make_org, name, **org_overrides):
    from leaf_platform import store

    org = make_org(name=name, **({"tier": org_overrides.pop("tier")} if "tier" in org_overrides else {}))
    if org_overrides:
        _set_org(org.org_id, **org_overrides)
    project = store.create_project(org.org_id, name)
    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    return org, project


def test_canonical_submission_denies_stored_restricted_org(make_org, pinned_policy):
    from leaf_platform import canonical_jobs
    from leaf_platform.entitlements import EntitlementDenied

    org, project = _canonical_project(make_org, "Canon Restricted")
    _set_org(org.org_id, tier="restricted")
    with pytest.raises(EntitlementDenied) as excinfo:
        canonical_jobs.submit_solve_job(
            org.org_id, project.project_id, str(org.org_id),
            "string-autofill-opt", {}, "ent-canon-deny-1")
    resp = excinfo.value.response
    assert resp.status_code == 403
    body = json.loads(resp.body)
    assert body["entitlement_required"] is True
    assert body["required"] == "run_write"
    assert body["tier"] == "restricted"
    assert body["error"]["error_code"] == "ENTITLEMENT_REQUIRED"
    assert body["error"]["retryable"] is False
    assert body["degraded_mode"] is False


def test_canonical_submission_denies_inactive_org_and_allows_entitled(make_org, pinned_policy):
    from leaf_platform import canonical_jobs
    from leaf_platform.entitlements import EntitlementDenied

    org, project = _canonical_project(make_org, "Canon Entitled", tier="hosted_starter")
    job = canonical_jobs.submit_solve_job(
        org.org_id, project.project_id, str(org.org_id),
        "string-autofill-opt", {}, "ent-canon-allow-1")
    assert job["kind"] == "solve" and job["status"] == "queued"

    _set_org(org.org_id, status="offboarding")
    with pytest.raises(EntitlementDenied) as excinfo:
        canonical_jobs.submit_solve_job(
            org.org_id, project.project_id, str(org.org_id),
            "string-autofill-opt", {}, "ent-canon-deny-2")
    body = json.loads(excinfo.value.response.body)
    assert body["required"] == "org_active"
    assert body["error"]["error_code"] == "ENTITLEMENT_REQUIRED"


# --------------------------------------------------------------------------- #
# policy/enforcement-data unavailability — structured 503, never a bare 500,
# never an allow (MAJOR fix: the boundary covers the WHOLE evaluation, not
# just module loading).
# --------------------------------------------------------------------------- #
def test_unreadable_policy_is_structured_503(client, make_org, tmp_path, monkeypatch):
    org = make_org(name="Broken Policy Org", tier="hosted_pro")
    hdr, pid = _make_project(client, org)
    bad = tmp_path / "broken-entitlements.json"
    bad.write_text("{this is not json", encoding="utf-8")
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(bad))
    r = _post_job(client, hdr, pid, "solve")
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["entitlement_required"] is True
    assert body["required"] == "run_write"
    assert body["tier"] == "hosted_pro"
    assert body["degraded_mode"] is False
    assert body["error"]["error_code"] == "INTERNAL"  # frozen §10 enum, not an ad-hoc code
    assert body["error"]["retryable"] is True
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_org_read_failure_is_structured_503(monkeypatch, pinned_policy):
    import uuid as _uuid

    from leaf_platform import entitlements as ents_mod

    def _boom(_org_id):
        raise RuntimeError("enforcement DB unreachable")

    monkeypatch.setattr(ents_mod.store, "get_org", _boom)
    denial = ents_mod.stored_job_entitlement_denial(_uuid.uuid4(), "solve")
    assert denial is not None and denial.status_code == 503
    body = json.loads(denial.body)
    assert body["entitlement_required"] is True
    assert body["required"] == "run_write"
    assert body["tier"] == "restricted"
    assert body["error"]["error_code"] == "INTERNAL"
    assert body["error"]["retryable"] is True


# --------------------------------------------------------------------------- #
# round-3 semantics: idempotent replay precedes enforcement; the INSERT
# re-checks org status/tier atomically (TOCTOU guard); the /api/run
# request-tier gate 503s structurally on an untrustworthy policy file.
# --------------------------------------------------------------------------- #
def test_idempotent_replay_survives_downgrade_but_new_submissions_deny(make_org, pinned_policy):
    from leaf_platform import canonical_jobs
    from leaf_platform.entitlements import EntitlementDenied

    org, project = _canonical_project(make_org, "Replay Downgrade", tier="hosted_starter")
    first = canonical_jobs.submit_solve_job(
        org.org_id, project.project_id, str(org.org_id),
        "string-autofill-opt", {"panelsPerString": 9}, "replay-key-1")

    _set_org(org.org_id, tier="restricted")
    replay = canonical_jobs.submit_solve_job(
        org.org_id, project.project_id, str(org.org_id),
        "string-autofill-opt", {"panelsPerString": 9}, "replay-key-1")
    assert replay["job_id"] == first["job_id"]

    with pytest.raises(EntitlementDenied):
        canonical_jobs.submit_solve_job(
            org.org_id, project.project_id, str(org.org_id),
            "string-autofill-opt", {"panelsPerString": 9}, "replay-key-2")


def test_toctou_downgrade_between_check_and_insert_is_denied(make_org, pinned_policy, monkeypatch):
    """Simulate the race: the entitlement read sees a stale entitled org while
    the DB row is already restricted. The INSERT's pinned-tier guard must
    refuse the row and the re-evaluation must surface the documented denial."""
    import dataclasses

    from leaf_platform import canonical_jobs
    from leaf_platform import entitlements as ents_mod
    from leaf_platform.entitlements import EntitlementDenied

    org, project = _canonical_project(make_org, "TOCTOU Org")
    _set_org(org.org_id, tier="restricted")

    real_get_org = ents_mod.store.get_org
    calls = {"n": 0}

    def stale_then_real(oid):
        calls["n"] += 1
        real = real_get_org(oid)
        if calls["n"] == 1:
            return dataclasses.replace(real, tier="hosted_starter", status="active")
        return real

    monkeypatch.setattr(ents_mod.store, "get_org", stale_then_real)
    with pytest.raises(EntitlementDenied) as excinfo:
        canonical_jobs.submit_solve_job(
            org.org_id, project.project_id, str(org.org_id),
            "string-autofill-opt", {}, "toctou-key-1")
    assert excinfo.value.response.status_code == 403
    assert calls["n"] >= 2  # the stale read passed; the atomic guard + recheck denied
