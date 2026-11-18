"""Recorded release examples. All package bytes are local synthetic fixtures."""
import hashlib
import pytest


@pytest.mark.parametrize("tool", ["panel-count", "beam-length", "fan-table"])
def test_package_target_and_rollback_pair(tmp_path, tool):
    package = tmp_path / tool
    package.write_bytes(tool.encode())
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    target = "private-test"
    def valid_release(receipt):
        return (receipt["target"] == target and receipt["tool"] == tool
                and receipt["digest"] == hashlib.sha256(package.read_bytes()).hexdigest())
    release = {"target":target, "tool":tool, "digest":digest}
    assert valid_release(release)
    assert not valid_release(dict(release, target="other"))
    package.write_bytes(b"tampered")
    assert not valid_release(release)
    before = {"tool":tool, "revision":"v1", "drawing":"d1", "output":hashlib.sha256(b"first").hexdigest()}
    def restored(receipt):
        return all(receipt.get(k) == v for k, v in before.items())
    assert restored(dict(before))
    for key in before:
        assert not restored(dict(before, **{key:"mismatch"}))
