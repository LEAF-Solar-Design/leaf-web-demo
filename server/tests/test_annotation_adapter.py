from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVER.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import annotation_adapter as adapter  # noqa: E402
from server import annotation_adapter as package_adapter  # noqa: E402

import platform_link  # noqa: E402

platform_link._ensure_platform_package()
from leaf_platform import annotation_source  # noqa: E402

TENANT = str(uuid.uuid4())
PROJECT = str(uuid.uuid4())
DRAWING = str(uuid.uuid4())
SESSION = {"session_id": "session-a", "tenant_id": TENANT,
           "drawing_id": DRAWING, "status": "active"}


def test_source_types_have_one_runtime_identity_across_import_paths():
    assert adapter.SourceVerificationRequest is package_adapter.SourceVerificationRequest
    assert adapter.VerifiedSourceReceipt is package_adapter.VerifiedSourceReceipt
    assert adapter.SourceAuthority is package_adapter.SourceAuthority
    assert adapter.SourceVerificationRequest is annotation_source.SourceVerificationRequest
    assert adapter.VerifiedSourceReceipt is annotation_source.VerifiedSourceReceipt
    assert adapter.SourceAuthority is annotation_source.SourceAuthority


def _request(**changes):
    values = {
        "session": SESSION, "tenant_id": TENANT, "org_id": TENANT,
        "project_id": PROJECT, "drawing_id": DRAWING, "kind": "apply",
        "base_version": 4, "base_commit": "a" * 40, "base_tree": "b" * 40,
        "preview_commit": "c" * 40, "preview_tree": "d" * 40,
        "payload_digest": "e" * 64, "payload_count": 3,
    }
    values.update(changes)
    return adapter.build_request(**values)


def test_owned_session_and_target_are_normalized():
    request = _request()
    assert request.tenant_id == TENANT
    assert request.org_id == TENANT
    assert adapter.store_args(request)["preview_commit"] == "c" * 40


@pytest.mark.parametrize("changes", [
    {"session": None},
    {"session": {**SESSION, "tenant_id": str(uuid.uuid4())}},
    {"session": {**SESSION, "drawing_id": str(uuid.uuid4())}},
    {"session": {**SESSION, "status": "archived"}},
    {"org_id": str(uuid.uuid4())},
    {"drawing_id": "unknown"},
])
def test_foreign_and_unknown_targets_collapse_to_not_found(changes):
    with pytest.raises(adapter.AnnotationAdapterError) as caught:
        _request(**changes)
    assert caught.value.code == "annotation_not_found"
    assert caught.value.status_code == 404


@pytest.mark.parametrize("field", [
    "base_commit", "base_tree", "preview_commit", "preview_tree",
])
def test_every_git_witness_must_be_exact_lowercase_sha(field):
    with pytest.raises(adapter.AnnotationAdapterError) as caught:
        _request(**{field: "A" * 40})
    assert caught.value.code == "invalid_git_witness"


def test_retry_is_a_fresh_apply_link():
    prior = str(uuid.uuid4())
    request = _request(retry_of_batch_id=prior)
    assert request.kind == "apply"
    assert request.retry_of_batch_id == prior
    assert request.reverses_batch_id is None


def test_undo_requires_a_fresh_inverse_batch_link():
    prior = str(uuid.uuid4())
    request = _request(kind="undo", reverses_batch_id=prior,
                       preview_commit="1" * 40, preview_tree="2" * 40)
    assert request.kind == "undo"
    assert request.reverses_batch_id == prior
    with pytest.raises(adapter.AnnotationAdapterError) as caught:
        _request(kind="undo")
    assert caught.value.code == "undo_source_required"


@pytest.mark.parametrize("changes,code", [
    ({"base_version": True}, "invalid_base_version"),
    ({"base_version": -1}, "invalid_base_version"),
    ({"payload_count": 0}, "invalid_payload_count"),
    ({"payload_digest": "f" * 63}, "invalid_payload_digest"),
    ({"kind": "retry"}, "invalid_batch_kind"),
])
def test_invalid_contract_fields_fail_closed(changes, code):
    with pytest.raises(adapter.AnnotationAdapterError) as caught:
        _request(**changes)
    assert caught.value.code == code
