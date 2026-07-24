"""Strict, platform-owned mutability policy for tenant workspaces.

The policy file is deliberately loaded for each call.  A release policy decides
which tenant repository bytes may be proposed, so stale cached policy is an
authorization risk.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Optional, Union


SERVER_DIR = Path(__file__).resolve().parent
DEFAULT_POLICY_FILE = SERVER_DIR / "platform_release_policy.json"
WORKSPACE_REFERENCE_CONTRACT = "leaf.workspace.v1"
MUTABILITY = frozenset({"frozen", "slushy", "tenant_owned"})
DENIED = "denied"

_TOP_LEVEL_FIELDS = frozenset({"version", "workspace_contracts", "releases"})
_CONTRACT_FIELDS = frozenset({"id", "sha256"})
_RELEASE_FIELDS = frozenset({
    "release_id", "workspace_contract", "workspace_contract_sha256", "rules",
})
_RULE_FIELDS = frozenset({"path", "mutability"})
_WORKSPACE_REFERENCE_FIELDS = frozenset({
    "contract", "workspace_contract", "desired_platform_release",
})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,199}$")


class PlatformReleasePolicyError(ValueError):
    """A platform policy or a tenant workspace reference is invalid."""


# Short alias for callers that use the repository's other policy-loader idiom.
PolicyError = PlatformReleasePolicyError

@dataclasses.dataclass(frozen=True)
class PathRule:
    pattern: str
    mutability: str


@dataclasses.dataclass(frozen=True)
class PlatformRelease:
    release_id: str
    workspace_contract: str
    workspace_contract_sha256: str
    rules: tuple[PathRule, ...]


@dataclasses.dataclass(frozen=True)
class PlatformReleasePolicy:
    version: int
    workspace_contracts: Mapping[str, str]
    releases: Mapping[str, PlatformRelease]


def policy_file() -> Path:
    """Return the platform-controlled policy location at request time."""
    configured = os.environ.get("LEAF_PLATFORM_RELEASE_POLICY_FILE")
    return Path(configured) if configured else DEFAULT_POLICY_FILE


def load_policy(path: Optional[Union[str, Path]] = None) -> PlatformReleasePolicy:
    """Load a policy afresh and reject every malformed or missing bundle."""
    target = Path(path) if path is not None else policy_file()
    if target.is_symlink():
        raise PlatformReleasePolicyError("platform policy file must not be a symlink")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlatformReleasePolicyError(
            f"platform policy is unavailable or invalid JSON at {target}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise PlatformReleasePolicyError("platform policy top level must be an object")
    return _parse_policy(raw)


def validate_workspace_reference(
    policy: PlatformReleasePolicy, reference: Mapping[str, Any]
) -> PlatformRelease:
    """Validate a tenant-owned reference without letting it carry policy bytes."""
    if not isinstance(policy, PlatformReleasePolicy):
        raise PlatformReleasePolicyError("platform policy is required")
    if not isinstance(reference, Mapping):
        raise PlatformReleasePolicyError("workspace reference must be an object")
    _require_exact_fields(reference, _WORKSPACE_REFERENCE_FIELDS, "workspace reference")

    contract = reference["contract"]
    workspace_contract = reference["workspace_contract"]
    release_id = reference["desired_platform_release"]
    if contract != WORKSPACE_REFERENCE_CONTRACT:
        raise PlatformReleasePolicyError("workspace reference has an unknown contract")
    _require_id(workspace_contract, "workspace reference.workspace_contract")
    _require_id(release_id, "workspace reference.desired_platform_release")

    release = policy.releases.get(release_id)
    if release is None:
        raise PlatformReleasePolicyError("workspace reference selects an unknown release")
    if workspace_contract != release.workspace_contract:
        raise PlatformReleasePolicyError("workspace reference contract does not match release")
    known_digest = policy.workspace_contracts.get(workspace_contract)
    if known_digest is None:
        raise PlatformReleasePolicyError("workspace reference selects a missing contract")
    if known_digest != release.workspace_contract_sha256:
        raise PlatformReleasePolicyError("workspace contract digest does not match release")
    return release


def classify_path(
    policy: PlatformReleasePolicy,
    release_id: str,
    path: Union[str, Path],
    *,
    root: Optional[Union[str, Path]] = None,
) -> str:
    """Return a mutability class, or ``denied`` when no platform rule matches.

    ``root`` is optional for virtual repository paths.  Callers that resolve a
    real checkout should pass it so symlink escapes are rejected before a path
    reaches a repository operation.
    """
    if not isinstance(policy, PlatformReleasePolicy):
        raise PlatformReleasePolicyError("platform policy is required")
    _require_id(release_id, "release id")
    release = policy.releases.get(release_id)
    if release is None:
        raise PlatformReleasePolicyError("unknown platform release")
    normalized = normalize_path(path, root=root)
    for rule in release.rules:
        if _matches(rule.pattern, normalized):
            return rule.mutability
    return DENIED


def normalize_path(path: Union[str, Path], *, root: Optional[Union[str, Path]] = None) -> str:
    """Accept one canonical repository-relative path and reject aliases."""
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw:
        raise PlatformReleasePolicyError("path must be a non-empty string")
    if "\\" in raw:
        raise PlatformReleasePolicyError("path must use forward slashes")
    if unicodedata.normalize("NFC", raw) != raw:
        raise PlatformReleasePolicyError("path must use NFC Unicode normalization")
    if Path(raw).is_absolute() or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise PlatformReleasePolicyError("path must be repository-relative")
    segments = raw.split("/")
    if any(not segment for segment in segments):
        raise PlatformReleasePolicyError("path must not contain empty segments")
    if any(segment in {".", ".."} for segment in segments):
        raise PlatformReleasePolicyError("path traversal is not allowed")
    if any(segment.casefold() != segment for segment in segments):
        raise PlatformReleasePolicyError("path must use canonical lowercase spelling")

    if root is not None:
        root_path = Path(root)
        candidate = root_path.joinpath(*segments)
        _reject_symlink_escape(root_path, candidate)
    return raw


def _parse_policy(raw: Mapping[str, Any]) -> PlatformReleasePolicy:
    _require_exact_fields(raw, _TOP_LEVEL_FIELDS, "platform policy")
    version = raw["version"]
    if isinstance(version, bool) or version != 1:
        raise PlatformReleasePolicyError("unsupported platform policy version")

    contracts_raw = raw["workspace_contracts"]
    if not isinstance(contracts_raw, list) or not contracts_raw:
        raise PlatformReleasePolicyError("workspace_contracts must be a non-empty list")
    contracts: dict[str, str] = {}
    for index, item in enumerate(contracts_raw):
        if not isinstance(item, Mapping):
            raise PlatformReleasePolicyError(f"workspace_contracts[{index}] must be an object")
        _require_exact_fields(item, _CONTRACT_FIELDS, f"workspace_contracts[{index}]")
        contract_id = item["id"]
        digest = item["sha256"]
        _require_id(contract_id, f"workspace_contracts[{index}].id")
        _require_sha256(digest, f"workspace_contracts[{index}].sha256")
        if contract_id in contracts:
            raise PlatformReleasePolicyError("duplicate workspace contract id")
        if any(existing.casefold() == contract_id.casefold() for existing in contracts):
            raise PlatformReleasePolicyError("workspace contract case alias ambiguity")
        contracts[contract_id] = digest

    releases_raw = raw["releases"]
    if not isinstance(releases_raw, list) or not releases_raw:
        raise PlatformReleasePolicyError("releases must be a non-empty list")
    releases: dict[str, PlatformRelease] = {}
    for index, item in enumerate(releases_raw):
        release = _parse_release(item, index, contracts)
        if release.release_id in releases:
            raise PlatformReleasePolicyError("duplicate platform release id")
        if any(existing.casefold() == release.release_id.casefold() for existing in releases):
            raise PlatformReleasePolicyError("platform release case alias ambiguity")
        releases[release.release_id] = release
    return PlatformReleasePolicy(version=version, workspace_contracts=contracts, releases=releases)


def _parse_release(
    raw: Any, index: int, contracts: Mapping[str, str]
) -> PlatformRelease:
    label = f"releases[{index}]"
    if not isinstance(raw, Mapping):
        raise PlatformReleasePolicyError(f"{label} must be an object")
    _require_exact_fields(raw, _RELEASE_FIELDS, label)
    release_id = raw["release_id"]
    contract = raw["workspace_contract"]
    digest = raw["workspace_contract_sha256"]
    _require_id(release_id, f"{label}.release_id")
    _require_id(contract, f"{label}.workspace_contract")
    _require_sha256(digest, f"{label}.workspace_contract_sha256")
    known_digest = contracts.get(contract)
    if known_digest is None:
        raise PlatformReleasePolicyError(f"{label} references a missing workspace contract")
    if digest != known_digest:
        raise PlatformReleasePolicyError(f"{label} workspace contract digest mismatch")

    rules_raw = raw["rules"]
    if not isinstance(rules_raw, list) or not rules_raw:
        raise PlatformReleasePolicyError(f"{label}.rules must be a non-empty list")
    rules = tuple(_parse_rule(item, f"{label}.rules[{rule_index}]")
                  for rule_index, item in enumerate(rules_raw))
    _reject_ambiguous_rules(rules, label)
    return PlatformRelease(
        release_id=release_id,
        workspace_contract=contract,
        workspace_contract_sha256=digest,
        rules=rules,
    )


def _parse_rule(raw: Any, label: str) -> PathRule:
    if not isinstance(raw, Mapping):
        raise PlatformReleasePolicyError(f"{label} must be an object")
    _require_exact_fields(raw, _RULE_FIELDS, label)
    pattern = raw["path"]
    mutability = raw["mutability"]
    if not isinstance(pattern, str) or not pattern:
        raise PlatformReleasePolicyError(f"{label}.path must be a non-empty string")
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        if not prefix:
            raise PlatformReleasePolicyError(f"{label}.path cannot match the repository root")
        pattern = normalize_path(prefix) + "/**"
    else:
        pattern = normalize_path(pattern)
    if mutability not in MUTABILITY:
        raise PlatformReleasePolicyError(f"{label}.mutability is invalid")
    return PathRule(pattern=pattern, mutability=mutability)


def _reject_ambiguous_rules(rules: tuple[PathRule, ...], label: str) -> None:
    seen: set[str] = set()
    aliases: set[str] = set()
    for rule in rules:
        if rule.pattern in seen:
            raise PlatformReleasePolicyError(f"{label} contains a duplicate path pattern")
        alias = unicodedata.normalize("NFC", rule.pattern).casefold()
        if alias in aliases:
            raise PlatformReleasePolicyError(f"{label} contains a case or Unicode alias")
        if any(_patterns_overlap(rule.pattern, prior.pattern) for prior in rules[:len(seen)]):
            raise PlatformReleasePolicyError(f"{label} contains ambiguous overlapping path patterns")
        seen.add(rule.pattern)
        aliases.add(alias)


def _patterns_overlap(left: str, right: str) -> bool:
    return _matches(left, _sample_path(right)) or _matches(right, _sample_path(left))


def _sample_path(pattern: str) -> str:
    return pattern[:-3] + "/x" if pattern.endswith("/**") else pattern


def _matches(pattern: str, path: str) -> bool:
    return path.startswith(pattern[:-2]) if pattern.endswith("/**") else path == pattern


def _require_exact_fields(value: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    keys = set(value)
    unknown = keys - allowed
    missing = allowed - keys
    if unknown:
        raise PlatformReleasePolicyError(f"{label} has unknown fields {sorted(unknown)}")
    if missing:
        raise PlatformReleasePolicyError(f"{label} is missing fields {sorted(missing)}")


def _require_id(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise PlatformReleasePolicyError(f"{label} must be a canonical lowercase identifier")


def _require_sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise PlatformReleasePolicyError(f"{label} must be a lowercase SHA-256 digest")


def _reject_symlink_escape(root: Path, candidate: Path) -> None:
    if root.is_symlink():
        raise PlatformReleasePolicyError("workspace root must not be a symlink")
    try:
        root_resolved = root.resolve(strict=True)
    except OSError as exc:
        raise PlatformReleasePolicyError(f"workspace root cannot be resolved: {exc}") from exc
    current = root_resolved
    for segment in candidate.relative_to(root).parts:
        current = current / segment
        if current.is_symlink():
            raise PlatformReleasePolicyError("workspace path traverses a symlink")
    try:
        candidate.resolve(strict=False).relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise PlatformReleasePolicyError("workspace path escapes its root") from exc
