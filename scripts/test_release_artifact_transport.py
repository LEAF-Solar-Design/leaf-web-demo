"""Local ZIP fixtures exercise content checks, not real producer acceptance."""

import hashlib
import stat
import zipfile

import pytest

from release_artifact_transport import ArchiveError, Limits, Member, verify_archive


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fixture(tmp_path, entries=None):
    entries = entries or {"staging-supply-set.json": b'{"schema":"fixture"}\n'}
    path = tmp_path / "archive.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path, {name: Member(len(data), sha(data)) for name, data in entries.items()}


def check(path, members, **kwargs):
    return verify_archive(path, archive_sha256=sha(path.read_bytes()),
                          expected_members=members, **kwargs)


def test_verifies_bytes_without_extracting_or_claiming_provenance(tmp_path):
    path, members = fixture(tmp_path)
    before = path.read_bytes()
    result = check(path, members)
    assert result["scope"] == "content-integrity-only"
    assert result["members"] == 1
    assert "producer" not in result and "source" not in result
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_web_members_and_empty_file(tmp_path):
    path, members = fixture(tmp_path, {"dist/index.html": b"hello", "dist/empty": b""})
    assert check(path, members)["expanded_bytes"] == 5


def test_archive_digest_mismatch(tmp_path):
    path, members = fixture(tmp_path)
    with pytest.raises(ArchiveError, match="archive digest mismatch"):
        verify_archive(path, archive_sha256="0" * 64, expected_members=members)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/escape", "a\\b", "a//b", "a/./b", "a/../b"])
def test_rejects_unsafe_expected_names(tmp_path, name):
    path, _ = fixture(tmp_path)
    with pytest.raises(ArchiveError, match="member path"):
        check(path, {name: Member(0, sha(b""))})


@pytest.mark.parametrize("mutation", ["missing", "extra", "size", "content"])
def test_exact_member_contract(tmp_path, mutation):
    path, members = fixture(tmp_path)
    if mutation == "missing":
        members = {"different.json": next(iter(members.values()))}
    elif mutation == "extra":
        members["extra"] = Member(0, sha(b""))
    elif mutation == "size":
        members["staging-supply-set.json"] = Member(0, sha(b""))
    else:
        old = members["staging-supply-set.json"]
        members["staging-supply-set.json"] = Member(old.size, "0" * 64)
    with pytest.raises(ArchiveError):
        check(path, members)


def test_duplicate_names(tmp_path):
    path, members = fixture(tmp_path)
    with zipfile.ZipFile(path, "a") as archive, pytest.warns(UserWarning):
        archive.writestr("staging-supply-set.json", b"duplicate")
    with pytest.raises(ArchiveError, match="duplicate"):
        check(path, members)


def test_rejects_symlink(tmp_path):
    path = tmp_path / "link.zip"
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, b"target")
    with pytest.raises(ArchiveError, match="entry type"):
        check(path, {"link": Member(6, sha(b"target"))})


@pytest.mark.parametrize("limits", [Limits(archive_bytes=1), Limits(expanded_bytes=1), Limits(members=0)])
def test_size_bounds(tmp_path, limits):
    path, members = fixture(tmp_path)
    with pytest.raises(ArchiveError):
        check(path, members, limits=limits)


def test_malformed_zip(tmp_path):
    path = tmp_path / "bad.zip"
    path.write_bytes(b"not a zip")
    with pytest.raises(ArchiveError, match="cannot be read"):
        check(path, {"file": Member(0, sha(b""))})


def test_expected_expansion_rejected_before_open(tmp_path):
    with pytest.raises(ArchiveError, match="expanded size"):
        verify_archive(tmp_path / "absent.zip", archive_sha256="0" * 64,
                       expected_members={"large": Member(100, "0" * 64)},
                       limits=Limits(expanded_bytes=10))
