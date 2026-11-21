"""Atomic publication of reviewed packages to the internal development catalog."""

from collections.abc import Mapping
from dataclasses import dataclass
import errno
import json
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile

from branch_tools.package_check import check_branch_package
from tool_envelopes import valid_identity


class DevPublishError(RuntimeError):
    def __init__(self, code: str, failures: list[dict[str, str]] | None = None):
        super().__init__(code)
        self.code = code
        self.failures = failures or []


@dataclass(frozen=True)
class DevPublishResult:
    name: str
    version: str
    package_digest: str
    path: Path
    created: bool


def _safe_path(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise DevPublishError("branch_dev_catalog_unavailable")


def read_branch_publication(name: str, version: str, root: Path) -> DevPublishResult:
    """Read a completed entry without repairing, replacing, or writing it."""
    if not valid_identity(name, version):
        raise DevPublishError("branch_dev_catalog_unavailable")
    directory = root / name / version
    _safe_path(directory)
    if not directory.is_dir():
        raise DevPublishError("branch_version_conflict")
    # Inspect every descendant, including unexpected files, without following
    # symlinked directories. An existing version is immutable.
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise DevPublishError("branch_dev_catalog_unavailable")
    digest_path = directory / "digest.txt"
    try:
        if not stat.S_ISREG(digest_path.lstat().st_mode) or digest_path.stat().st_size > 65:
            raise DevPublishError("branch_version_conflict")
        with digest_path.open("rb") as stream:
            digest = stream.read(66).decode("ascii").strip()
    except (FileNotFoundError, UnicodeDecodeError) as exc:
        raise DevPublishError("branch_version_conflict") from exc
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise DevPublishError("branch_version_conflict")
    return DevPublishResult(name, version, digest, directory, False)


def publish_branch_package(files: Mapping[str, bytes], name: str, root: Path,
                           review: Mapping[str, str] | None = None) -> DevPublishResult:
    metadata = {"status": "reviewed"}
    if review is not None:
        if (review.get("approval") not in ("self", "independent")
                or review.get("approver_role") not in ("owner", "reviewer")):
            raise DevPublishError("branch_review_invalid")
        metadata.update(approval=review["approval"], approver_role=review["approver_role"])
    files = dict(files)
    checked = check_branch_package(files, name)
    if not checked.ok:
        raise DevPublishError("branch_package_invalid", checked.failures)
    failures = list(checked.failures)
    # Keep the writer bounded even if the validator's implementation changes.
    if len(files) > 64 and not any(f["rule"] == "cap_files" for f in failures):
        failures.append({"rule": "cap_files", "path": "", "message": "package exceeds 64 files"})
    if sum(len(data) for data in files.values()) > 4 * 1024 * 1024 and not any(
        f["rule"] == "cap_bytes" for f in failures
    ):
        failures.append({"rule": "cap_bytes", "path": "", "message": "package exceeds 4 MiB"})
    if failures:
        raise DevPublishError("branch_package_invalid", failures)
    version = checked.manifest["version"]
    digest = checked.package_digest
    if not valid_identity(name, version):
        raise DevPublishError("branch_package_invalid", [
            {"rule": "identity", "path": "tool.json", "message": "invalid package identity"},
        ])
    root = Path(root)
    destination = root / name / version
    _safe_path(destination)

    def existing_publication() -> DevPublishResult:
        existing = read_branch_publication(name, version, root)
        if existing.package_digest != digest:
            raise DevPublishError("branch_version_conflict")
        if review is not None:
            receipt = existing.path / "review.json"
            try:
                info = receipt.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
                    raise DevPublishError("branch_version_conflict")
                if json.loads(receipt.read_bytes()) != metadata:
                    raise DevPublishError("branch_version_conflict")
            except (FileNotFoundError, ValueError) as exc:
                raise DevPublishError("branch_version_conflict") from exc
        return existing

    if destination.exists():
        return existing_publication()
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    destination.parent.mkdir(mode=0o755, exist_ok=True)
    _safe_path(destination)
    temporary = Path(tempfile.mkdtemp(prefix=".publish-", dir=destination.parent))
    try:
        with zipfile.ZipFile(temporary / "package.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, data in sorted(files.items()):
                info = zipfile.ZipInfo(f"{name}/{path}", date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        (temporary / "digest.txt").write_text(digest + "\n", encoding="ascii", newline="\n")
        (temporary / "review.json").write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
        # mkdtemp is private while incomplete; the finished catalog is readable.
        temporary.chmod(0o755)
        _safe_path(destination)
        try:
            temporary.rename(destination)
        except OSError as exc:
            # A concurrent publisher may have installed this immutable version.
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            return existing_publication()
        return DevPublishResult(name, version, digest, destination, True)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
