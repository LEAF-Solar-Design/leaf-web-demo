import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import pytest


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import glug_adoption


RANGLR_BASE = "205317570ea1a0299a93c694af2480ed3ed4c5b3"
CURRENT_BASE = "6" * 40
MUSHY_SOURCE = "c3fdc0869692c804ae69fe00b5b6f0722c80943a"
NOW = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc)


def _raw_manifest():
    payload = b"built mushy artifact"
    files = [{
        "path": "index.js",
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }]
    aggregate = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "version": 1,
        "workspace_id": "glug",
        "repository": {
            "slug_env": "GLUG_REPOSITORY_SLUG",
            "ranglr_upstream": "https://github.com/Evan-Haug/ef26.git",
            "ranglr_base_commit": RANGLR_BASE,
            "require_clean": True,
            "forbid_linked_refs": True,
            "forbid_submodules": True,
            "forbid_symlinks": True,
        },
        "sources": {
            "mushy_source_commit": MUSHY_SOURCE,
            "package_lock_sha256": "1" * 64,
        },
        "artifact": {
            "component": "mushy-author",
            "entrypoint": "index.js",
            "files": files,
            "byte_count": len(payload),
            "aggregate_sha256": aggregate,
        },
        "limits": {
            "max_changed_files": 20,
            "max_diff_bytes": 120000,
            "author_timeout_seconds": 240,
            "wrapper_timeout_seconds": 280,
            "reclaim_timeout_seconds": 300,
        },
        "powers": {
            "allowed": [
                "code_question", "announcement_draft", "schedule_draft",
                "stage_change", "create_review_branch", "create_pull_request",
            ],
            "denied": [
                "raw_member_query", "raw_finance_query", "membership_mutation",
                "treasury_action", "merge", "deploy", "app_store_publish",
            ],
        },
        "checks": [
            "server-tests", "migrations", "ios-build", "authorization", "pin-integrity",
        ],
        "targets": ["staging", "review_branch", "pull_request", "testflight"],
        "rollback": {
            "source_commit": RANGLR_BASE,
            "backend_deployment": "prior-proven-deployment",
            "ios_source_commit": RANGLR_BASE,
        },
    }


def _write_manifest(tmp_path, raw=None):
    target = tmp_path / "adoption.json"
    target.write_text(json.dumps(raw or _raw_manifest()), encoding="utf-8")
    return target


def _request(**overrides):
    value = {
        "workspace_id": "glug",
        "repository_slug": "biting-fogies/glug",
        "requested_power": "stage_change",
        "claim": {
            "contract": "glug.mushy-claim.v1",
            "id": "claim-1",
            "workspace": "glug",
            "actor_digest": "8" * 64,
            "power": "stage_change",
            "issued_at": "2026-09-01T11:58:00Z",
            "expires_at": "2026-09-01T12:03:00Z",
            "base_commit": CURRENT_BASE,
            "signature": "9" * 64,
        },
        "repository_state": {
            "head_commit": CURRENT_BASE,
            "clean": True,
            "linked_refs": [],
            "submodules": [],
            "symlinks": [],
        },
    }
    value.update(overrides)
    return value


def _validate(adoption, request):
    glug_adoption.validate_stage_request(
        adoption,
        request,
        env={"GLUG_REPOSITORY_SLUG": "biting-fogies/glug"},
        now=NOW,
    )


def test_loads_policy_and_builds_client_safe_receipt(tmp_path):
    adoption = glug_adoption.load_adoption(_write_manifest(tmp_path))
    assert adoption.artifact_entrypoint == "index.js"
    receipt = glug_adoption.client_pin_receipt(adoption)
    assert receipt["source_commit"] == MUSHY_SOURCE
    assert "repository" not in receipt
    assert "package_lock" not in receipt
    glug_adoption.validate_client_pin_receipt(receipt)


def test_rejects_undeclared_artifact_entrypoint(tmp_path):
    raw = _raw_manifest()
    raw["artifact"]["entrypoint"] = "stale-author.js"
    with pytest.raises(glug_adoption.GlugAdoptionError, match="declared file"):
        glug_adoption.load_adoption(_write_manifest(tmp_path, raw))


def test_rejects_unknown_workspace_and_extra_input_keys(tmp_path):
    target = _write_manifest(tmp_path)
    with pytest.raises(glug_adoption.GlugAdoptionError, match="unknown workspace"):
        glug_adoption.adoption_for_workspace("ranglr", target)
    adoption = glug_adoption.load_adoption(target)
    with pytest.raises(glug_adoption.GlugAdoptionError, match="unknown fields"):
        _validate(adoption, _request(extra=True))


def test_rejects_stale_claim_and_head_drift(tmp_path):
    adoption = glug_adoption.load_adoption(_write_manifest(tmp_path))
    stale = _request()
    stale["claim"]["expires_at"] = "2026-09-01T11:59:00Z"
    with pytest.raises(glug_adoption.GlugAdoptionError, match="stale claim"):
        _validate(adoption, stale)
    drifted = _request()
    drifted["claim"]["base_commit"] = "f" * 40
    with pytest.raises(glug_adoption.GlugAdoptionError, match="head drift"):
        _validate(adoption, drifted)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("workspace", "ranglr", "workspace"),
        ("power", "code_question", "power"),
    ],
)
def test_rejects_claim_scope_drift(tmp_path, field, value, reason):
    adoption = glug_adoption.load_adoption(_write_manifest(tmp_path))
    request = _request()
    request["claim"][field] = value
    with pytest.raises(glug_adoption.GlugAdoptionError, match=reason):
        _validate(adoption, request)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("clean", False, "dirty clone"),
        ("linked_refs", ["refs/replace/x"], "linked_refs"),
        ("submodules", ["vendor/code"], "submodules"),
        ("symlinks", ["config/current"], "symlinks"),
    ],
)
def test_rejects_unsafe_repository_state(tmp_path, field, value, reason):
    adoption = glug_adoption.load_adoption(_write_manifest(tmp_path))
    request = _request()
    request["repository_state"][field] = value
    with pytest.raises(glug_adoption.GlugAdoptionError, match=reason):
        _validate(adoption, request)


def test_rejects_digest_drift_and_extra_artifact_file(tmp_path):
    adoption = glug_adoption.load_adoption(_write_manifest(tmp_path))
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "index.js").write_bytes(b"wrong")
    with pytest.raises(glug_adoption.GlugAdoptionError, match="digest drift"):
        glug_adoption.verify_artifact_tree(adoption, root)
    (root / "index.js").write_bytes(b"built mushy artifact")
    (root / "extra.js").write_text("extra", encoding="utf-8")
    with pytest.raises(glug_adoption.GlugAdoptionError, match="undeclared"):
        glug_adoption.verify_artifact_tree(adoption, root)


def test_rejects_secret_shaped_or_local_path_receipt_content():
    base = {
        "contract": "glug.mushy-pin.v1",
        "workspace": "glug",
        "source_commit": MUSHY_SOURCE,
        "artifact_component": "mushy-author",
        "artifact_byte_count": 20,
        "artifact_aggregate_sha256": "2" * 64,
    }
    with pytest.raises(glug_adoption.GlugAdoptionError, match="secret-shaped key"):
        glug_adoption.validate_client_pin_receipt({**base, "token": "not-safe"})
    with pytest.raises(glug_adoption.GlugAdoptionError, match="local-path"):
        glug_adoption.validate_client_pin_receipt({
            **base,
            "artifact_component": "C:\\tmp\\artifact",
        })


def test_denied_power_never_reaches_staging(tmp_path):
    adoption = glug_adoption.load_adoption(_write_manifest(tmp_path))
    with pytest.raises(glug_adoption.GlugAdoptionError, match="unavailable"):
        _validate(adoption, _request(requested_power="treasury_action"))
