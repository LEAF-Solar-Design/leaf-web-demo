import pytest

from cad_versions import (
    CadVersionError,
    CadVersionStore,
    DigestMismatch,
    ReceiptConflict,
    UnknownArtifact,
    VersionNotFound,
)


def _sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def test_upload_names_exact_artifact_version_and_digest():
    store = CadVersionStore()
    data = b"rooftop-panel-geometry-v1"

    receipt = store.record_upload("tenant-a", "proj-1", "artifact-1", data)

    assert receipt.tenant_id == "tenant-a"
    assert receipt.project_id == "proj-1"
    assert receipt.artifact_id == "artifact-1"
    assert receipt.version == 1
    assert receipt.digest == _sha256_hex(data)
    assert receipt.kind == "upload"
    assert receipt.parent_version is None
    assert receipt.byte_length == len(data)


def test_derive_chains_next_version_to_exact_parent():
    store = CadVersionStore()
    store.record_upload("tenant-a", "proj-1", "artifact-1", b"v1-bytes")

    derived = store.record_derive(
        "tenant-a", "proj-1", "artifact-1", 1, b"v2-bytes")

    assert derived.version == 2
    assert derived.parent_version == 1
    assert derived.kind == "derive"
    assert derived.digest == _sha256_hex(b"v2-bytes")


def test_derive_against_missing_parent_raises_version_not_found():
    store = CadVersionStore()
    store.record_upload("tenant-a", "proj-1", "artifact-1", b"v1-bytes")

    with pytest.raises(VersionNotFound):
        store.record_derive("tenant-a", "proj-1", "artifact-1", 5, b"v6-bytes")


def test_derive_against_unknown_artifact_raises_unknown_artifact():
    store = CadVersionStore()

    with pytest.raises(UnknownArtifact):
        store.record_derive("tenant-a", "proj-1", "never-uploaded", 1, b"x")


def test_verify_read_succeeds_when_bytes_match_the_receipt():
    store = CadVersionStore()
    data = b"exact-bytes"
    store.record_upload("tenant-a", "proj-1", "artifact-1", data)

    receipt = store.verify_read("tenant-a", "proj-1", "artifact-1", 1, data)

    assert receipt.digest == _sha256_hex(data)


def test_verify_read_raises_digest_mismatch_on_tampered_bytes():
    store = CadVersionStore()
    store.record_upload("tenant-a", "proj-1", "artifact-1", b"original-bytes")

    with pytest.raises(DigestMismatch) as excinfo:
        store.verify_read("tenant-a", "proj-1", "artifact-1", 1, b"tampered-bytes")

    assert excinfo.value.artifact_id == "artifact-1"
    assert excinfo.value.version == 1
    assert excinfo.value.expected_digest == _sha256_hex(b"original-bytes")
    assert excinfo.value.actual_digest == _sha256_hex(b"tampered-bytes")


def test_verify_read_unknown_version_raises_version_not_found():
    store = CadVersionStore()
    store.record_upload("tenant-a", "proj-1", "artifact-1", b"v1-bytes")

    with pytest.raises(VersionNotFound):
        store.verify_read("tenant-a", "proj-1", "artifact-1", 2, b"v1-bytes")


def test_receipts_are_immutable_conflicting_rewrite_raises():
    store = CadVersionStore()
    store.record_upload("tenant-a", "proj-1", "artifact-1", b"first-bytes")

    with pytest.raises(ReceiptConflict):
        store.record_upload("tenant-a", "proj-1", "artifact-1", b"different-bytes")

    # The original receipt must survive the rejected rewrite untouched.
    receipt = store.get_receipt("tenant-a", "proj-1", "artifact-1", 1)
    assert receipt.digest == _sha256_hex(b"first-bytes")


def test_receipts_replay_of_identical_bytes_returns_original_receipt():
    store = CadVersionStore()
    first = store.record_upload("tenant-a", "proj-1", "artifact-1", b"same-bytes")

    replay = store.record_upload("tenant-a", "proj-1", "artifact-1", b"same-bytes")

    assert replay.created_at == first.created_at
    assert replay.digest == first.digest


def test_derive_conflicting_rewrite_of_existing_version_raises():
    store = CadVersionStore()
    store.record_upload("tenant-a", "proj-1", "artifact-1", b"v1-bytes")
    store.record_derive("tenant-a", "proj-1", "artifact-1", 1, b"v2-bytes")

    with pytest.raises(ReceiptConflict):
        store.record_derive("tenant-a", "proj-1", "artifact-1", 1, b"v2-different-bytes")


def test_receipts_are_scoped_by_tenant():
    store = CadVersionStore()
    store.record_upload("tenant-a", "proj-1", "artifact-1", b"tenant-a-bytes")

    with pytest.raises(UnknownArtifact):
        store.get_receipt("tenant-b", "proj-1", "artifact-1", 1)


def test_receipts_are_scoped_by_project():
    store = CadVersionStore()
    store.record_upload("tenant-a", "proj-1", "artifact-1", b"proj-1-bytes")

    with pytest.raises(UnknownArtifact):
        store.get_receipt("tenant-a", "proj-2", "artifact-1", 1)


def test_same_artifact_id_different_scopes_hold_independent_lineages():
    store = CadVersionStore()
    a = store.record_upload("tenant-a", "proj-1", "artifact-1", b"scope-a-bytes")
    b = store.record_upload("tenant-b", "proj-1", "artifact-1", b"scope-b-bytes")

    assert a.digest != b.digest
    assert store.get_receipt("tenant-a", "proj-1", "artifact-1", 1).digest == a.digest
    assert store.get_receipt("tenant-b", "proj-1", "artifact-1", 1).digest == b.digest


def test_latest_receipt_returns_highest_version():
    store = CadVersionStore()
    store.record_upload("tenant-a", "proj-1", "artifact-1", b"v1-bytes")
    store.record_derive("tenant-a", "proj-1", "artifact-1", 1, b"v2-bytes")
    store.record_derive("tenant-a", "proj-1", "artifact-1", 2, b"v3-bytes")

    latest = store.latest_receipt("tenant-a", "proj-1", "artifact-1")

    assert latest.version == 3
    assert latest.parent_version == 2


def test_list_versions_returns_receipts_in_ascending_order():
    store = CadVersionStore()
    store.record_upload("tenant-a", "proj-1", "artifact-1", b"v1-bytes")
    store.record_derive("tenant-a", "proj-1", "artifact-1", 1, b"v2-bytes")

    versions = store.list_versions("tenant-a", "proj-1", "artifact-1")

    assert [r.version for r in versions] == [1, 2]


def test_list_versions_unknown_artifact_returns_empty():
    store = CadVersionStore()
    assert store.list_versions("tenant-a", "proj-1", "never-uploaded") == []


def test_get_receipt_unknown_artifact_raises_unknown_artifact():
    store = CadVersionStore()
    with pytest.raises(UnknownArtifact):
        store.get_receipt("tenant-a", "proj-1", "never-uploaded", 1)


@pytest.mark.parametrize("tenant_id", ["", None])
def test_upload_rejects_empty_or_missing_tenant_id(tenant_id):
    store = CadVersionStore()
    with pytest.raises(CadVersionError):
        store.record_upload(tenant_id, "proj-1", "artifact-1", b"data")


def test_upload_rejects_non_bytes_payload():
    store = CadVersionStore()
    with pytest.raises(CadVersionError):
        store.record_upload("tenant-a", "proj-1", "artifact-1", "not-bytes")


def test_derive_rejects_non_positive_parent_version():
    store = CadVersionStore()
    store.record_upload("tenant-a", "proj-1", "artifact-1", b"v1-bytes")
    with pytest.raises(CadVersionError):
        store.record_derive("tenant-a", "proj-1", "artifact-1", 0, b"data")
