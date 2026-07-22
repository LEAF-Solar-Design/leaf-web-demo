import copy

from leaf_platform import evidence


def test_bundle_root_is_deterministic_and_offline_verifiable():
    blobs = {"records/solve.json": evidence.json_blob({"hash": "a" * 64}),
             "artifacts/drawing.dwg": b"DWG fixture bytes",
             "records/findings.json": evidence.json_blob([{"state": "advisory"}])}
    first = evidence.build(blobs, metadata={"projectId": "project-1"})
    second = evidence.build(dict(reversed(list(blobs.items()))), metadata={"projectId": "project-1"})
    assert first == second
    assert evidence.verify(first, blobs) == {
        "valid": True, "errors": [], "rootSha256": first["rootSha256"]}


def test_any_byte_status_or_missing_artifact_invalidates_bundle():
    blobs = {"records/waivers.json": evidence.json_blob([{"state": "approved"}]),
             "artifacts/report.pdf": b"report"}
    manifest = evidence.build(blobs, metadata={"solveId": "solve-1"})
    changed = {**blobs, "records/waivers.json": evidence.json_blob([{"state": "revoked"}])}
    assert not evidence.verify(manifest, changed)["valid"]
    assert not evidence.verify(manifest, {"records/waivers.json": blobs["records/waivers.json"]})["valid"]
    forged = copy.deepcopy(manifest)
    forged["entries"][0]["sha256"] = "0" * 64
    assert not evidence.verify(forged, blobs)["valid"]
    metadata_tamper = copy.deepcopy(manifest)
    metadata_tamper["metadata"]["solveId"] = "solve-2"
    assert not evidence.verify(metadata_tamper, blobs)["valid"]


def test_paths_and_empty_bundles_fail_closed():
    for blobs in ({}, {"../secret": b"x"}, {"/absolute": b"x"}):
        try:
            evidence.build(blobs, metadata={"project": "x"})
            assert False, "expected invalid bundle"
        except ValueError:
            pass
