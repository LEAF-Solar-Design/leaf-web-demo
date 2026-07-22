import hashlib
import json
import uuid

import pytest

from leaf_platform import canonical_jobs, compliance_store, snapshots, store
from leaf_platform.db import cursor


PACK = {
    "pack_id": "leaf.electrical.voltage-v1", "edition": "NEC 2023",
    "version": "0.1.0", "status": "candidate",
    "rules": [{"rule_id": "NEC-690.7-COLD-VOC", "rule_type": "max_cold_string_voltage",
               "citation": {"authority": "NEC", "edition": "2023", "section": "690.7(A)(3)"},
               "inputs": {"modules_in_series": "modules", "module_voc_stc_v": "voc",
                          "beta_voc_per_c": "beta", "minimum_temperature_c": "min_temp",
                          "inverter_max_dc_voltage_v": "max_voltage"}}],
}
INPUTS = {"modules": 20, "voc": "50", "beta": "-0.0028", "min_temp": "-10",
          "max_voltage": "1000"}


def _completed_solve(make_org, label):
    standard = snapshots.import_snapshot(snapshots.draft(
        "standards", PACK, snapshots.canonical_bytes(PACK),
        {"uri": "fixture://narrow-compliance-pack", "claim": "candidate test pack"}))
    snapshots.select_channel("standards", "local-candidate", standard["snapshot_id"], "test")
    org = make_org(label)
    project = store.create_project(org.org_id, label)
    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    job = canonical_jobs.submit_solve_job(org.org_id, project.project_id, str(org.org_id),
                                          "string-autofill-opt", {}, f"{label}-job")
    canonical_jobs.claim_next(f"{label}-worker", request_tenant_id=str(org.org_id))
    solver_input = {"groups": [], "panelsPerString": 10, "options": {}}
    solver_result = {"feasible": True, "groupTargets": {}}
    digest = lambda value: hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = {"solver": "string-autofill-opt", "solver_input": solver_input,
              "solver_result": solver_result, "request_sha256": digest({}),
              "input_sha256": digest(solver_input), "result_sha256": digest(solver_result),
              "solver_revision": "test", "source_sha256": "c" * 64, "runtime": "python-test"}
    provenance = {"attempt": 1, "execution_path": "local", "solver_revision": "test",
                  "source_sha256": "c" * 64, "runtime": "python-test"}
    assert canonical_jobs.complete_solve(job["job_id"], f"{label}-worker", result, provenance) == "applied"
    saved = canonical_jobs.get_job_for_tenant(job["job_id"], str(org.org_id))
    return org, project, uuid.UUID(saved["result"]["solve_id"])


def test_compliance_run_is_pinned_idempotent_and_immutable(make_org):
    org, project, solve_id = _completed_solve(make_org, "compliance")
    first = compliance_store.record_run(org.org_id, project.project_id, solve_id, PACK, INPUTS)
    repeated = compliance_store.record_run(org.org_id, project.project_id, solve_id, PACK, INPUTS)
    assert repeated == first
    assert first["findings"][0]["effect"] == "advisory"
    assert first["findings"][0]["result"] == "fail"
    with pytest.raises(ValueError, match="does not match"):
        compliance_store.record_run(org.org_id, project.project_id, solve_id,
                                    {**PACK, "version": "other"}, INPUTS)
    with pytest.raises(ValueError, match="different inputs"):
        compliance_store.record_run(org.org_id, project.project_id, solve_id, PACK,
                                    {**INPUTS, "modules": 19})
    with cursor() as cur:
        with pytest.raises(Exception, match="immutable canonical ledger"):
            cur.execute("UPDATE compliance_findings SET payload = '{}' WHERE finding_id = %(id)s",
                        {"id": first["findings"][0]["finding_id"]})


def test_waiver_state_machine_is_tenant_and_role_bound(make_org):
    org, project, solve_id = _completed_solve(make_org, "waiver")
    run = compliance_store.record_run(org.org_id, project.project_id, solve_id, PACK, INPUTS)
    owner = store.create_identity_binding(org.org_id, "auth0", "waiver-owner", role="owner")
    editor = store.create_identity_binding(org.org_id, "auth0", "waiver-editor", role="editor")
    reviewer = store.create_identity_binding(org.org_id, "auth0", "waiver-reviewer", role="reviewer")
    finding_id = uuid.UUID(run["findings"][0]["finding_id"])
    proposed = compliance_store.propose_waiver(
        org.org_id, project.project_id, finding_id, editor.binding_id, "field condition")
    with pytest.raises(ValueError, match="owner role"):
        compliance_store.transition_waiver(
            org.org_id, project.project_id, uuid.UUID(proposed["waiver_id"]),
            reviewer.binding_id, "approved")
    approved = compliance_store.transition_waiver(
        org.org_id, project.project_id, uuid.UUID(proposed["waiver_id"]),
        owner.binding_id, "approved", "engineering disposition")
    assert approved["state"] == "approved"
    with pytest.raises(ValueError, match="invalid waiver transition"):
        compliance_store.transition_waiver(
            org.org_id, project.project_id, uuid.UUID(proposed["waiver_id"]),
            owner.binding_id, "rejected")
    assert compliance_store.transition_waiver(
        org.org_id, project.project_id, uuid.UUID(proposed["waiver_id"]),
        owner.binding_id, "revoked")["state"] == "revoked"


def test_compliance_and_waiver_api_use_pinned_pack_and_binding(client, make_org):
    org, project, solve_id = _completed_solve(make_org, "compliance-api")
    owner = store.create_identity_binding(org.org_id, "auth0", "compliance-api-owner", role="owner")
    headers = {"X-Org-Id": str(org.org_id), "X-Actor-Binding-Id": str(owner.binding_id)}
    run = client.post(
        f"/api/projects/{project.project_id}/solves/{solve_id}/compliance",
        headers=headers, json={"inputs": INPUTS})
    assert run.status_code == 201, run.text
    finding_id = run.json()["findings"][0]["finding_id"]
    listed = client.get(
        f"/api/projects/{project.project_id}/solves/{solve_id}/compliance-findings",
        headers={"X-Org-Id": str(org.org_id)})
    assert listed.status_code == 200
    assert listed.json()["findings"][0]["finding_id"] == finding_id
    waiver = client.post(
        f"/api/projects/{project.project_id}/compliance-findings/{finding_id}/waivers",
        headers=headers, json={"reason": "documented field condition"})
    assert waiver.status_code == 201, waiver.text
    decision = client.post(
        f"/api/projects/{project.project_id}/compliance-waivers/{waiver.json()['waiver_id']}/transitions",
        headers=headers, json={"state": "approved", "note": "owner disposition"})
    assert decision.status_code == 200, decision.text
    assert decision.json()["state"] == "approved"
