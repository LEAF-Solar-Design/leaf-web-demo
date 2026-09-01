"""Server-owned Glug enrollment for bounded Mushy maintenance.

The organization repository receives only the client pin receipt returned by
client_pin_receipt. Repository policy, executor limits, internal source
identity, and rollback identities remain in this trusted control plane.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping


SERVER_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST_FILE = SERVER_DIR / "glug_adoption_manifest.json"

SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REPO_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,199}$")
SAFE_ARTIFACT_PATH = re.compile(r"^[A-Za-z0-9._/@+-]+(?:/[A-Za-z0-9._/@+-]+)*$")
SECRET_KEY = re.compile(r"(secret|token|password|credential|private.?key|grant)", re.I)
SECRET_VALUE = re.compile(
    r"(?:gh[opsu]_[A-Za-z0-9]{20,}|sk_(?:live|test)_[A-Za-z0-9]{12,}|"
    r"whsec_[A-Za-z0-9]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
LOCAL_PATH = re.compile(r"(?:[A-Za-z]:\\|/(?:Users|home|tmp)/)")

TOP_FIELDS = frozenset({
    "version", "workspace_id", "repository", "sources", "artifact", "limits",
    "powers", "checks", "targets", "rollback",
})
REPOSITORY_FIELDS = frozenset({
    "slug_env", "ranglr_upstream", "ranglr_base_commit", "require_clean",
    "forbid_linked_refs", "forbid_submodules", "forbid_symlinks",
})
SOURCES_FIELDS = frozenset({"mushy_source_commit", "package_lock_sha256"})
ARTIFACT_FIELDS = frozenset({
    "component", "files", "byte_count", "aggregate_sha256",
})
ARTIFACT_FILE_FIELDS = frozenset({"path", "bytes", "sha256"})
LIMIT_FIELDS = frozenset({
    "max_changed_files", "max_diff_bytes", "author_timeout_seconds",
    "wrapper_timeout_seconds", "reclaim_timeout_seconds",
})
POWER_FIELDS = frozenset({"allowed", "denied"})
ROLLBACK_FIELDS = frozenset({
    "source_commit", "backend_deployment", "ios_source_commit",
})
STAGE_FIELDS = frozenset({
    "workspace_id", "repository_slug", "requested_power", "claim",
    "repository_state",
})
CLAIM_FIELDS = frozenset({
    "contract", "id", "workspace", "actor_digest", "power", "base_commit",
    "issued_at", "expires_at", "signature",
})
REPOSITORY_STATE_FIELDS = frozenset({
    "head_commit", "clean", "linked_refs", "submodules", "symlinks",
})


class GlugAdoptionError(ValueError):
    """A fail-closed Glug enrollment or staging validation error."""


@dataclasses.dataclass(frozen=True)
class ArtifactFile:
    path: str
    bytes: int
    sha256: str


@dataclasses.dataclass(frozen=True)
class GlugAdoption:
    raw: Mapping[str, Any]
    workspace_id: str
    repository_slug_env: str
    ranglr_base_commit: str
    mushy_source_commit: str
    package_lock_sha256: str
    artifact_component: str
    artifact_files: tuple[ArtifactFile, ...]
    artifact_byte_count: int
    artifact_aggregate_sha256: str
    allowed_powers: frozenset[str]
    denied_powers: frozenset[str]


def manifest_file() -> Path:
    configured = os.environ.get("LEAF_GLUG_ADOPTION_MANIFEST_FILE", "").strip()
    return Path(configured) if configured else DEFAULT_MANIFEST_FILE


def load_adoption(path: Path | str | None = None) -> GlugAdoption:
    target = Path(path) if path is not None else manifest_file()
    if target.is_symlink():
        raise GlugAdoptionError("adoption manifest must not be a symlink")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GlugAdoptionError("Glug adoption manifest is unavailable or invalid") from exc
    if not isinstance(raw, Mapping):
        raise GlugAdoptionError("adoption manifest must be an object")
    _exact(raw, TOP_FIELDS, "manifest")
    if raw["version"] != 1:
        raise GlugAdoptionError("unsupported adoption manifest version")
    if raw["workspace_id"] != "glug":
        raise GlugAdoptionError("unknown workspace")

    repository = _mapping(raw["repository"], "repository")
    _exact(repository, REPOSITORY_FIELDS, "repository")
    if repository["slug_env"] != "GLUG_REPOSITORY_SLUG":
        raise GlugAdoptionError("repository slug must remain server-owned")
    if repository["ranglr_upstream"] != "https://github.com/Evan-Haug/ef26.git":
        raise GlugAdoptionError("unexpected Ranglr upstream")
    base_commit = _sha40(repository["ranglr_base_commit"], "ranglr base")
    for key in ("require_clean", "forbid_linked_refs", "forbid_submodules", "forbid_symlinks"):
        if repository[key] is not True:
            raise GlugAdoptionError(key + " must be true")

    sources = _mapping(raw["sources"], "sources")
    _exact(sources, SOURCES_FIELDS, "sources")
    mushy_commit = _sha40(sources["mushy_source_commit"], "Mushy source")
    package_lock = _sha256(sources["package_lock_sha256"], "package lock")

    artifact = _mapping(raw["artifact"], "artifact")
    _exact(artifact, ARTIFACT_FIELDS, "artifact")
    component = _safe_id(artifact["component"], "artifact component")
    files_raw = artifact["files"]
    if not isinstance(files_raw, list) or not files_raw:
        raise GlugAdoptionError("artifact files must be a non-empty list")
    files: list[ArtifactFile] = []
    for item in files_raw:
        mapped = _mapping(item, "artifact file")
        _exact(mapped, ARTIFACT_FILE_FIELDS, "artifact file")
        path_value = mapped["path"]
        if not isinstance(path_value, str) or not SAFE_ARTIFACT_PATH.fullmatch(path_value):
            raise GlugAdoptionError("artifact path is invalid")
        byte_count = mapped["bytes"]
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise GlugAdoptionError("artifact byte count is invalid")
        files.append(ArtifactFile(path_value, byte_count, _sha256(mapped["sha256"], "artifact file")))
    if [entry.path for entry in files] != sorted(entry.path for entry in files):
        raise GlugAdoptionError("artifact files must be sorted")
    if len({entry.path for entry in files}) != len(files):
        raise GlugAdoptionError("artifact file paths must be unique")
    declared_bytes = artifact["byte_count"]
    if isinstance(declared_bytes, bool) or declared_bytes != sum(entry.bytes for entry in files):
        raise GlugAdoptionError("artifact aggregate byte count does not match files")
    declared_aggregate = _sha256(artifact["aggregate_sha256"], "artifact aggregate")
    if declared_aggregate != artifact_manifest_digest(files):
        raise GlugAdoptionError("artifact aggregate digest does not match files")

    limits = _mapping(raw["limits"], "limits")
    _exact(limits, LIMIT_FIELDS, "limits")
    expected_limits = {
        "max_changed_files": 20,
        "max_diff_bytes": 120_000,
        "author_timeout_seconds": 240,
        "wrapper_timeout_seconds": 280,
        "reclaim_timeout_seconds": 300,
    }
    if dict(limits) != expected_limits:
        raise GlugAdoptionError("Glug staging limits do not match the frozen policy")

    powers = _mapping(raw["powers"], "powers")
    _exact(powers, POWER_FIELDS, "powers")
    allowed = _string_set(powers["allowed"], "allowed powers")
    denied = _string_set(powers["denied"], "denied powers")
    if allowed & denied:
        raise GlugAdoptionError("a power cannot be both allowed and denied")
    expected_allowed = frozenset({
        "code_question", "announcement_draft", "schedule_draft",
        "stage_change", "create_review_branch", "create_pull_request",
    })
    expected_denied = frozenset({
        "raw_member_query", "raw_finance_query", "membership_mutation",
        "treasury_action", "merge", "deploy", "app_store_publish",
    })
    if allowed != expected_allowed or denied != expected_denied:
        raise GlugAdoptionError("Glug power matrix does not match the frozen policy")

    checks = raw["checks"]
    if not isinstance(checks, list) or not checks or any(not isinstance(item, str) or not item for item in checks):
        raise GlugAdoptionError("checks must be a non-empty string list")
    if raw["targets"] != ["staging", "review_branch", "pull_request", "testflight"]:
        raise GlugAdoptionError("targets do not match the frozen Glug sequence")
    rollback = _mapping(raw["rollback"], "rollback")
    _exact(rollback, ROLLBACK_FIELDS, "rollback")
    if rollback["source_commit"] != base_commit:
        raise GlugAdoptionError("source rollback must name the Ranglr base")
    for key in ("backend_deployment", "ios_source_commit"):
        if not isinstance(rollback[key], str) or not rollback[key].strip():
            raise GlugAdoptionError("rollback identity is missing")

    return GlugAdoption(
        raw=raw,
        workspace_id="glug",
        repository_slug_env=repository["slug_env"],
        ranglr_base_commit=base_commit,
        mushy_source_commit=mushy_commit,
        package_lock_sha256=package_lock,
        artifact_component=component,
        artifact_files=tuple(files),
        artifact_byte_count=declared_bytes,
        artifact_aggregate_sha256=declared_aggregate,
        allowed_powers=allowed,
        denied_powers=denied,
    )


def adoption_for_workspace(workspace_id: str, path: Path | str | None = None) -> GlugAdoption:
    if workspace_id != "glug":
        raise GlugAdoptionError("unknown workspace")
    return load_adoption(path)


def validate_stage_request(
    adoption: GlugAdoption,
    request: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    now: dt.datetime,
) -> None:
    _exact(request, STAGE_FIELDS, "stage request")
    if request["workspace_id"] != adoption.workspace_id:
        raise GlugAdoptionError("unknown workspace")
    expected_slug = env.get(adoption.repository_slug_env, "").strip()
    if not REPO_SLUG.fullmatch(expected_slug):
        raise GlugAdoptionError("Glug repository is not server-bound")
    if request["repository_slug"] != expected_slug:
        raise GlugAdoptionError("repository does not match the server-owned policy")
    power = request["requested_power"]
    if power not in adoption.allowed_powers or power in adoption.denied_powers:
        raise GlugAdoptionError("requested power is unavailable")
    claim = _mapping(request["claim"], "claim")
    _exact(claim, CLAIM_FIELDS, "claim")
    if claim["contract"] != "glug.mushy-claim.v1":
        raise GlugAdoptionError("claim contract is invalid")
    _safe_id(claim["id"], "claim id")
    if claim["workspace"] != adoption.workspace_id:
        raise GlugAdoptionError("claim workspace is invalid")
    if claim["power"] != power:
        raise GlugAdoptionError("claim power is invalid")
    _sha256(claim["actor_digest"], "claim actor digest")
    _sha40(claim["base_commit"], "claim base")
    _sha256(claim["signature"], "claim signature")
    issued = _instant(claim["issued_at"], "issued_at")
    expires = _instant(claim["expires_at"], "expires_at")
    now_utc = now.astimezone(dt.timezone.utc)
    if expires <= issued or expires - issued > dt.timedelta(seconds=300):
        raise GlugAdoptionError("claim lifetime is invalid")
    if now_utc < issued or now_utc >= expires:
        raise GlugAdoptionError("stale claim")

    state = _mapping(request["repository_state"], "repository state")
    _exact(state, REPOSITORY_STATE_FIELDS, "repository state")
    _sha40(state["head_commit"], "repository head")
    if state["head_commit"] != claim["base_commit"]:
        raise GlugAdoptionError("repository head drift")
    if state["clean"] is not True:
        raise GlugAdoptionError("dirty clone")
    for key in ("linked_refs", "submodules", "symlinks"):
        if not isinstance(state[key], list):
            raise GlugAdoptionError(key + " must be a list")
        if state[key]:
            raise GlugAdoptionError(key + " are not allowed")


def verify_artifact_tree(adoption: GlugAdoption, root: Path | str) -> None:
    artifact_root = Path(root)
    if artifact_root.is_symlink() or not artifact_root.is_dir():
        raise GlugAdoptionError("artifact root is unavailable or symlinked")
    declared = {entry.path: entry for entry in adoption.artifact_files}
    observed: set[str] = set()
    for candidate in artifact_root.rglob("*"):
        if candidate.is_symlink():
            raise GlugAdoptionError("artifact symlink is not allowed")
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(artifact_root).as_posix()
        observed.add(relative)
        entry = declared.get(relative)
        if entry is None:
            raise GlugAdoptionError("artifact contains an undeclared file")
        payload = candidate.read_bytes()
        if len(payload) != entry.bytes or hashlib.sha256(payload).hexdigest() != entry.sha256:
            raise GlugAdoptionError("artifact digest drift")
    if observed != set(declared):
        raise GlugAdoptionError("artifact is missing declared files")


def client_pin_receipt(adoption: GlugAdoption) -> Mapping[str, Any]:
    receipt = {
        "contract": "glug.mushy-pin.v1",
        "workspace": adoption.workspace_id,
        "source_commit": adoption.mushy_source_commit,
        "artifact_component": adoption.artifact_component,
        "artifact_byte_count": adoption.artifact_byte_count,
        "artifact_aggregate_sha256": adoption.artifact_aggregate_sha256,
    }
    _reject_secret_shaped(receipt)
    return receipt


def validate_client_pin_receipt(value: Mapping[str, Any]) -> None:
    _reject_secret_shaped(value)
    expected = frozenset({
        "contract", "workspace", "source_commit", "artifact_component",
        "artifact_byte_count", "artifact_aggregate_sha256",
    })
    _exact(value, expected, "client pin receipt")


def artifact_manifest_digest(files: list[ArtifactFile] | tuple[ArtifactFile, ...]) -> str:
    payload = [
        {"path": entry.path, "bytes": entry.bytes, "sha256": entry.sha256}
        for entry in files
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _reject_secret_shaped(value: Mapping[str, Any]) -> None:
    encoded = json.dumps(value, sort_keys=True)
    for key in value:
        if SECRET_KEY.search(str(key)):
            raise GlugAdoptionError("client receipt contains a secret-shaped key")
    if SECRET_VALUE.search(encoded) or LOCAL_PATH.search(encoded):
        raise GlugAdoptionError("client receipt contains secret-shaped or local-path content")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GlugAdoptionError(label + " must be an object")
    return value


def _exact(value: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown:
        raise GlugAdoptionError(label + " has unknown fields")
    if missing:
        raise GlugAdoptionError(label + " is missing fields")


def _sha40(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA40.fullmatch(value):
        raise GlugAdoptionError(label + " must be a lowercase commit SHA")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise GlugAdoptionError(label + " must be a lowercase SHA-256")
    return value


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise GlugAdoptionError(label + " must be a canonical identifier")
    return value


def _string_set(value: Any, label: str) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise GlugAdoptionError(label + " must be a non-empty list")
    result = frozenset(_safe_id(item, label) for item in value)
    if len(result) != len(value):
        raise GlugAdoptionError(label + " contains duplicates")
    return result


def _instant(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise GlugAdoptionError(label + " must be an RFC3339 UTC instant")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise GlugAdoptionError(label + " is invalid") from exc
    return parsed
