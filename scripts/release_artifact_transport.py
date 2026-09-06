"""Offline archive integrity boundary for Studio release transport.

This module neither contacts a provider nor establishes producer authority.
The caller must independently bind expectations to the accepted producer,
source, attempt and immutable storage version before consuming a result.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import re
import stat
import zipfile


class ArchiveError(ValueError):
    """The downloaded bytes do not satisfy the expected archive contract."""


@dataclass(frozen=True)
class Member:
    size: int
    sha256: str


@dataclass(frozen=True)
class Limits:
    archive_bytes: int = 256 * 1024 * 1024
    expanded_bytes: int = 512 * 1024 * 1024
    members: int = 10000


_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _digest(value: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ArchiveError("expected a lowercase SHA256 digest")


def _name(value: str) -> None:
    # Validate the literal name, not a normalized path that hides traversal.
    if (not isinstance(value, str) or not value or "\\" in value
            or ":" in value or any(ord(c) < 32 for c in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ArchiveError("unsupported archive member path")


def verify_archive(
    path: Path,
    *,
    archive_sha256: str,
    expected_members: dict[str, Member],
    limits: Limits = Limits(),
) -> dict:
    """Verify exact archive bytes and members; never extract or write files.

    Expectations are an input, NOT a trusted receipt. A successful return
    must not be used as a replacement for the provider's provenance checks.
    Only regular-file ZIP entries are supported in this first offline slice.
    """
    _digest(archive_sha256)
    for bound in (limits.archive_bytes, limits.expanded_bytes, limits.members):
        if type(bound) is not int or bound <= 0:
            raise ArchiveError("archive limits must be positive integers")
    if not expected_members or len(expected_members) > limits.members:
        raise ArchiveError("expected member count exceeds bounds or is empty")
    total = 0
    for name, member in expected_members.items():
        _name(name)
        if not isinstance(member, Member) or type(member.size) is not int or member.size < 0:
            raise ArchiveError("invalid expected member size")
        _digest(member.sha256)
        total += member.size
    if total > limits.expanded_bytes:
        raise ArchiveError("expected expanded size exceeds bound")

    try:
        # A bounded immutable snapshot prevents changes between hashing and
        # ZIP parsing from substituting different bytes. No file is extracted.
        with Path(path).open("rb") as source:
            payload = source.read(limits.archive_bytes + 1)
        if len(payload) > limits.archive_bytes:
            raise ArchiveError("archive size exceeds bound")
        if hashlib.sha256(payload).hexdigest() != archive_sha256:
            raise ArchiveError("archive digest mismatch")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)):
                raise ArchiveError("duplicate archive member")
            if set(names) != set(expected_members):
                raise ArchiveError("archive members differ from expected set")
            for entry in entries:
                _name(entry.filename)
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if (entry.orig_filename != entry.filename or entry.is_dir()
                        or mode not in {0, stat.S_IFREG} or entry.flag_bits & 1):
                    raise ArchiveError("unsupported archive entry type")
                expected = expected_members[entry.filename]
                if entry.file_size != expected.size:
                    raise ArchiveError("member size mismatch")
                digest = hashlib.sha256()
                size = 0
                with archive.open(entry) as member:
                    while chunk := member.read(min(65536, expected.size - size + 1)):
                        size += len(chunk)
                        if size > expected.size:
                            raise ArchiveError("expanded member exceeds expected size")
                        digest.update(chunk)
                if size != expected.size or digest.hexdigest() != expected.sha256:
                    raise ArchiveError("member content mismatch")
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
        raise ArchiveError("archive cannot be read") from exc
    return {
        "schema": "leaf.archive-integrity.v1",
        "scope": "content-integrity-only",
        "archive_sha256": archive_sha256,
        "archive_bytes": len(payload),
        "expanded_bytes": total,
        "members": len(expected_members),
    }
