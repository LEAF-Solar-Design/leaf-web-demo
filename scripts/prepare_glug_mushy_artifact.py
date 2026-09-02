#!/usr/bin/env python3
"""Verify and seed Glug's immutable Mushy author artifact.

The archive is supplied out of band by the trusted build lane. This script
never downloads Mushy source and never accepts credentials. It checks the
archive digest, rejects link and traversal entries, verifies every extracted
file against the server-owned adoption manifest, and seeds only an empty
Docker volume. A volume that already carries the exact labels is read back and
verified without mutation, which makes failed-stage retries idempotent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from server.glug_adoption import (  # noqa: E402
    ArtifactFile,
    GlugAdoption,
    artifact_manifest_digest,
    load_adoption,
    verify_artifact_tree,
)

SHA256 = re.compile(r"^[0-9a-f]{64}$")
VOLUME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
IMAGE_DIGEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}$")

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class ArtifactPreparationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member_path(name: str) -> PurePosixPath:
    if "\\" in name or "\x00" in name:
        raise ArtifactPreparationError(f"unsafe artifact archive path: {name}")
    path = PurePosixPath(name)
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if (
        path.is_absolute()
        or not parts
        or any(part == ".." or ":" in part for part in parts)
    ):
        raise ArtifactPreparationError(f"unsafe artifact archive path: {name}")
    return PurePosixPath(*parts)


def extract_verified_archive(
    archive: Path,
    expected_archive_sha256: str,
    destination: Path,
    adoption: GlugAdoption,
) -> None:
    expected = expected_archive_sha256.strip().lower()
    if not SHA256.fullmatch(expected):
        raise ArtifactPreparationError("archive SHA-256 must be 64 lowercase hexadecimal characters")
    if not archive.is_file() or archive.is_symlink():
        raise ArtifactPreparationError("artifact archive must be a regular file")
    actual = sha256(archive)
    if actual != expected:
        raise ArtifactPreparationError(f"artifact archive digest mismatch: expected {expected}, got {actual}")
    if destination.exists() and any(destination.iterdir()):
        raise ArtifactPreparationError("artifact extraction destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)

    expected_files = {entry.path: entry for entry in adoption.artifact_files}
    expected_directories = {
        parent.as_posix()
        for path in expected_files
        for parent in PurePosixPath(path).parents
        if parent.as_posix() != "."
    }
    extracted_files: set[str] = set()
    extracted_bytes = 0

    with tarfile.open(archive, mode="r:*") as bundle:
        members = bundle.getmembers()
        for member in members:
            if member.name in (".", "./") and member.isdir():
                continue
            relative = _safe_member_path(member.name)
            if member.issym() or member.islnk() or member.isdev() or not (member.isfile() or member.isdir()):
                raise ArtifactPreparationError(f"unsupported artifact archive entry: {member.name}")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                if relative.as_posix() not in expected_directories:
                    raise ArtifactPreparationError(f"undeclared artifact archive directory: {member.name}")
                target.mkdir(parents=True, exist_ok=True)
                continue
            declared = expected_files.get(relative.as_posix())
            if declared is None:
                raise ArtifactPreparationError(f"undeclared artifact archive file: {member.name}")
            if relative.as_posix() in extracted_files:
                raise ArtifactPreparationError(f"duplicate artifact archive file: {member.name}")
            if member.size != declared.bytes:
                raise ArtifactPreparationError(f"artifact archive byte count mismatch: {member.name}")
            extracted_bytes += member.size
            if extracted_bytes > adoption.artifact_byte_count:
                raise ArtifactPreparationError("artifact archive exceeds the declared byte count")
            extracted_files.add(relative.as_posix())
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise ArtifactPreparationError(f"artifact archive entry is unreadable: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)

    missing = sorted(set(expected_files) - extracted_files)
    if missing:
        raise ArtifactPreparationError(f"artifact archive is missing declared file: {missing[0]}")

    verify_artifact_tree(adoption, destination)


def _run(
    runner: CommandRunner,
    args: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(args),
        check=check,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _remove_container(
    runner: CommandRunner,
    name: str,
) -> subprocess.CompletedProcess[str]:
    return _run(runner, ["docker", "rm", "--force", name], check=False)


def _readback_volume(
    runner: CommandRunner,
    *,
    volume: str,
    seed_image: str,
    destination: Path,
) -> None:
    container = f"glug-mushy-readback-{uuid.uuid4().hex}"
    container_created = False
    try:
        _run(runner, [
            "docker", "create", "--name", container,
            "--mount", f"type=volume,source={volume},target=/artifact,readonly",
            seed_image, "true",
        ])
        container_created = True
        _run(runner, ["docker", "cp", f"{container}:/artifact/.", str(destination)])
    finally:
        if container_created:
            removed = _remove_container(runner, container)
            if removed.returncode != 0:
                raise ArtifactPreparationError("Docker readback container cleanup failed")


def seed_docker_volume(
    artifact_root: Path,
    *,
    volume: str,
    seed_image: str,
    adoption: GlugAdoption,
    runner: CommandRunner = subprocess.run,
) -> str:
    if not VOLUME_NAME.fullmatch(volume):
        raise ArtifactPreparationError("Docker volume name is invalid")
    if not IMAGE_DIGEST.fullmatch(seed_image):
        raise ArtifactPreparationError("seed image must be bound to an exact sha256 digest")

    source_label = f"com.leaf.glug.mushy-source={adoption.mushy_source_commit}"
    artifact_label = f"com.leaf.glug.mushy-artifact={adoption.artifact_aggregate_sha256}"
    inspection = _run(
        runner,
        ["docker", "volume", "inspect", "--format", "{{json .Labels}}", volume],
        check=False,
    )
    absent = inspection.returncode != 0
    if absent and not re.search(r"no such volume", inspection.stderr, re.IGNORECASE):
        raise ArtifactPreparationError("Docker volume inspection failed without proving absence")

    created = False
    if absent:
        preparation_id = uuid.uuid4().hex
        preparation_label = f"com.leaf.glug.mushy-preparer={preparation_id}"
        _run(runner, [
            "docker", "volume", "create",
            "--label", source_label,
            "--label", artifact_label,
            "--label", preparation_label,
            volume,
        ])
        inspection = _run(
            runner,
            ["docker", "volume", "inspect", "--format", "{{json .Labels}}", volume],
        )

    try:
        labels = json.loads(inspection.stdout.strip())
    except json.JSONDecodeError as exc:
        raise ArtifactPreparationError("Docker returned invalid volume labels") from exc
    if not isinstance(labels, dict) or (
        labels.get("com.leaf.glug.mushy-source") != adoption.mushy_source_commit
        or labels.get("com.leaf.glug.mushy-artifact") != adoption.artifact_aggregate_sha256
    ):
        raise ArtifactPreparationError("existing Docker volume labels do not match the pinned artifact")
    if absent:
        created = labels.get("com.leaf.glug.mushy-preparer") == preparation_id
        # If another process won the absent-to-create race, its exact labels
        # make the volume read-only to this process. Readback may fail while
        # that owner is still seeding, but this process must never remove it.

    seed_container = f"glug-mushy-seed-{uuid.uuid4().hex}"
    seed_container_created = False
    seed_container_removed = False
    try:
        if created:
            _run(runner, [
                "docker", "create", "--name", seed_container,
                "--mount", f"type=volume,source={volume},target=/artifact",
                "--mount", f"type=bind,source={artifact_root},target=/source,readonly",
                seed_image, "sh", "-ceu",
                "test -z \"$(find /artifact -mindepth 1 -maxdepth 1 -print -quit)\"; "
                "cp -a /source/. /artifact/; chmod -R a-w /artifact",
            ])
            seed_container_created = True
            _run(runner, ["docker", "start", "--attach", seed_container])
        with tempfile.TemporaryDirectory(prefix="glug-mushy-volume-readback-") as temp:
            readback = Path(temp)
            _readback_volume(
                runner,
                volume=volume,
                seed_image=seed_image,
                destination=readback,
            )
            verify_artifact_tree(adoption, readback)
    except Exception as exc:
        cleanup_failures: list[str] = []
        if seed_container_created:
            removed = _remove_container(runner, seed_container)
            seed_container_removed = True
            if removed.returncode != 0:
                cleanup_failures.append("seed container")
        if created:
            removed = _run(runner, ["docker", "volume", "rm", volume], check=False)
            if removed.returncode != 0:
                cleanup_failures.append("new artifact volume")
        if cleanup_failures:
            raise ArtifactPreparationError(
                "Docker rollback failed for " + " and ".join(cleanup_failures)
            ) from exc
        raise
    finally:
        if seed_container_created and not seed_container_removed:
            removed = _remove_container(runner, seed_container)
            if removed.returncode != 0:
                raise ArtifactPreparationError("Docker seed container cleanup failed")
    return "created" if created else "verified"


def receipt(
    adoption: GlugAdoption,
    archive: Path,
    archive_digest: str,
    *,
    volume: str | None = None,
    volume_status: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "contract": "glug.mushy-artifact-preparation.v1",
        "mushySourceCommit": adoption.mushy_source_commit,
        "packageLockSha256": adoption.package_lock_sha256,
        "artifactFileCount": len(adoption.artifact_files),
        "artifactByteCount": adoption.artifact_byte_count,
        "artifactAggregateSha256": adoption.artifact_aggregate_sha256,
        "archiveSha256": archive_digest,
        "verified": True,
    }
    if volume is not None:
        value["dockerVolume"] = volume
        value["dockerVolumeStatus"] = volume_status
    return value


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("verify-archive", "seed-volume"))
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--manifest", type=Path, default=REPO / "server" / "glug_adoption_manifest.json")
    parser.add_argument("--volume")
    parser.add_argument("--seed-image")
    args = parser.parse_args(argv)
    if args.mode == "seed-volume" and (not args.volume or not args.seed_image):
        parser.error("seed-volume requires --volume and --seed-image")
    if args.mode == "verify-archive" and (args.volume or args.seed_image):
        parser.error("verify-archive does not accept Docker options")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    adoption = load_adoption(args.manifest)
    archive = args.archive.resolve(strict=True)
    archive_digest = args.archive_sha256.strip().lower()
    with tempfile.TemporaryDirectory(prefix="glug-mushy-artifact-") as temp:
        artifact_root = Path(temp)
        extract_verified_archive(archive, archive_digest, artifact_root, adoption)
        volume_status = None
        if args.mode == "seed-volume":
            volume_status = seed_docker_volume(
                artifact_root,
                volume=args.volume,
                seed_image=args.seed_image,
                adoption=adoption,
            )
    print(json.dumps(receipt(
        adoption,
        archive,
        archive_digest,
        volume=args.volume,
        volume_status=volume_status,
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
