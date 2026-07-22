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
