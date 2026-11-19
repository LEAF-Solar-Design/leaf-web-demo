"""Small, uncached internal authoring catalog; never reads the signed catalog."""

from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
import json
import os
from pathlib import Path
import re
import stat

from tool_envelopes import valid_identity

_MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class DevEntry:
    metadata: dict
    package: Path


def _directory(path: Path) -> bool:
    return stat.S_ISDIR(path.lstat().st_mode)


def _file(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
        raise ValueError("unsafe catalog file")
    return info


def _read(path: Path) -> bytes:
    _file(path)
    with path.open("rb") as stream:
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("oversized catalog file")
    return raw


def _entry(directory: Path, name: str, version: str) -> DevEntry:
    package = directory / "package.zip"
    _file(package)
    digest_path = directory / "digest.txt"
    digest = _read(digest_path).decode("ascii").strip()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("invalid digest")
    review = "unreviewed"
    try:
        raw = _read(directory / "review.json")
    except FileNotFoundError:
        pass
    else:
        value = json.loads(raw)
        if isinstance(value, dict) and isinstance(value.get("status"), str):
            review = value["status"]
    staged_at = datetime.fromtimestamp(_file(digest_path).st_mtime, timezone.utc)
    return DevEntry({"name": name, "version": version, "package_digest": digest,
                     "review": review, "staged_at": staged_at.isoformat().replace("+00:00", "Z")},
                    package)


def read_catalog() -> list[DevEntry] | None:
    """None means an unavailable root; an existing empty directory lists no tools."""
    configured = os.environ.get("LEAF_BRANCH_DEV_TOOLS_DIR")
    if not configured:
        return None
    root = Path(configured).absolute()
    try:
        if any(path.is_symlink() for path in (root, *root.parents)) or not _directory(root):
            return None
        with os.scandir(root) as children:
            names = [Path(child.path) for child in islice(children, 200)]
        entries = []
        for name_dir in names:
            try:
                if not valid_identity(name_dir.name, "0.0.0") or not _directory(name_dir):
                    continue
                with os.scandir(name_dir) as children:
                    versions = [Path(child.path) for child in islice(children, 50)]
                for version_dir in versions:
                    try:
                        if valid_identity(name_dir.name, version_dir.name) and _directory(version_dir):
                            entries.append(_entry(version_dir, name_dir.name, version_dir.name))
                    except (OSError, ValueError, OverflowError):
                        continue
            except OSError:
                continue
        return sorted(entries, key=lambda entry: (entry.metadata["name"], entry.metadata["version"]))
    except OSError:
        return None
