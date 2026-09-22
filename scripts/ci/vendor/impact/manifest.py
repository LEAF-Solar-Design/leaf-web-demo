"""ASPECTS v1 authoring contract.

load_manifest(path) and validate_manifest(data) return independent, defaults-filled
dicts. manifest_digest accepts that loaded dict. coverage accepts tracked paths;
contract_counts summarizes endpoint revisions. No host state is read here.
"""

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from itertools import islice
import json
from pathlib import Path
import re

import yaml

MAX_BYTES = 1024 * 1024
MAX_PATHS = 200_000
CONCERNS = (
    "contracts", "persistence", "auth_tenancy", "logging", "telemetry", "alarms",
    "deployment", "runtime_state", "restart_rearm", "rollback", "tests", "docs_runbooks",
)
OUTCOMES = ("changed", "unchanged-compatible", "not-applicable", "deferred", "unresolved")
FACETS = (
    "web-api", "web-ui", "backend", "ios", "cad-engine", "deployment", "contracts",
    "ci", "release", "persistence", "integration", "agent", "control-plane", "runtime-host",
)
ID_PATTERN = r"[a-z0-9][a-z0-9-]{0,63}"
REPOSITORY_PATTERN = r"(?:local|[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})"
SHA_PATTERN = r"[0-9a-f]{40}"
REQUIRED = {"schema_version", "project_id", "repository", "authored_at_revision", "components"}


class ManifestError(ValueError):
    """An input violates the bounded ASPECTS contract."""


def read_text(path):
    # Every input read is bounded to 1 MiB, including a file that grows during read.
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ManifestError(f"{path}: exceeds 1 MiB limit")
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise ManifestError(f"{path}: must be UTF-8") from exc


def mapping(value, path, required=(), optional=()):
    # Fails closed on unknown keys at every mapping level.
    if not isinstance(value, dict):
        raise ManifestError(f"{path}: expected mapping")
    allowed = set(required) | set(optional)
    for key in value:
        if not isinstance(key, str) or key not in allowed:
            raise ManifestError(f"{path}: unknown key {key!r}")
    for key in sorted(set(required) - value.keys()):
        raise ManifestError(f"{path}.{key}: required key missing")
    return value


def string(value, path, maximum, pattern=None):
    # Strings accept only their stated grammar and 1..maximum characters.
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ManifestError(f"{path}: expected string of 1 to {maximum} characters")
    if pattern is not None and re.fullmatch(pattern, value) is None:
        raise ManifestError(f"{path}: must match {pattern}")
    return value


def choice(value, path, choices):
    if not isinstance(value, str) or value not in choices:
        raise ManifestError(f"{path}: expected one of {', '.join(choices)}")
    return value


def sequence(value, path, minimum, maximum):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ManifestError(f"{path}: expected list of {minimum} to {maximum} items")
    return value


def strings(value, path, minimum=0, maximum=20, length=512):
    for i, item in enumerate(sequence(value, path, minimum, maximum)):
        string(item, f"{path}[{i}]", length)


@lru_cache(maxsize=None)
def glob_to_regex(pattern):
    # Globs are bounded to 512 characters and compiled once per cached pattern.
    string(pattern, "glob", 512, r"[A-Za-z0-9_./*?@+\-]+")
    if pattern.startswith("/") or ".." in pattern.split("/"):
        raise ManifestError("glob: leading / and .. segments are forbidden")
    parts = pattern.split("/")
    tokens = []
    i = 0
    while i < len(parts):
        part = parts[i]
        if part == "**":
            while i + 1 < len(parts) and parts[i + 1] == "**":
                i += 1
            if i + 1 < len(parts):
                tokens.append(r"(?:[^/]+/)*")
            elif len(tokens) == 0:
                tokens.append(r"(?:[^/]+(?:/[^/]+)*)?")
            else:
                tokens.append(r"[^/]+(?:/[^/]+)*")
        else:
            tokens.append("".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in part))
            if i + 1 < len(parts):
                tokens.append("/")
        i += 1
    return re.compile(r"\A" + "".join(tokens) + r"\Z")


def match_path(patterns, path):
    return any(glob_to_regex(pattern).fullmatch(path) is not None for pattern in patterns)


def globs(value, path, minimum=0, maximum=50):
    for i, pattern in enumerate(sequence(value, path, minimum, maximum)):
        string(pattern, f"{path}[{i}]", 512)
        try:
            glob_to_regex(pattern)
        except ManifestError as exc:
            raise ManifestError(f"{path}[{i}]: {exc}") from exc


def identified(items, path, minimum, maximum):
    seen = set()
    for i, item in enumerate(sequence(items, path, minimum, maximum)):
        location = f"{path}[{i}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{location}: expected mapping")
        identifier = string(item.get("id"), f"{location}.id", 64, ID_PATTERN)
        if identifier in seen:
            raise ManifestError(f"{location}.id: duplicate id {identifier}")
        seen.add(identifier)
        yield item, location


def endpoint(value, path, consumer=False):
    mapping(value, path, ("repo", "paths", "revision"), ("route", "extractor", "status") if consumer else ("route", "extractor"))
    string(value["repo"], f"{path}.repo", 201, rf"(?:self|{REPOSITORY_PATTERN})")
    globs(value["paths"], f"{path}.paths", 1)
    string(value["revision"], f"{path}.revision", 40, rf"(?:HEAD|unresolved|{SHA_PATTERN})")
    if "route" in value:
        string(value["route"], f"{path}.route", 256)
    if "extractor" in value:
        string(value["extractor"], f"{path}.extractor", 64, r"[a-z0-9-]{1,64}")
    if "status" in value:
        choice(value["status"], f"{path}.status", ("agrees", "mismatch", "unresolved"))


def validate_manifest(data):
    # Fails closed on unknown keys; only schema version 1 is accepted.
    mapping(data, "manifest", REQUIRED, ("unmapped", "targets", "contracts", "companions", "concerns", "repos", "integration_branch"))
    data = deepcopy(data)
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ManifestError("schema_version: expected integer 1")
    string(data["project_id"], "project_id", 64, ID_PATTERN)
    string(data["repository"], "repository", 201, REPOSITORY_PATTERN)
    string(data["authored_at_revision"], "authored_at_revision", 40, SHA_PATTERN)
    for key in ("unmapped", "targets", "contracts", "companions"):
        data.setdefault(key, [])
    for component, path in identified(data["components"], "components", 1, 200):
        mapping(component, path, ("id", "paths", "facets"))
        globs(component["paths"], f"{path}.paths", 1)
        for i, facet in enumerate(sequence(component["facets"], f"{path}.facets", 0, 12)):
            choice(facet, f"{path}.facets[{i}]", FACETS)
    globs(data["unmapped"], "unmapped", 0, 500)
    repos = data.setdefault("repos", {})
    if not isinstance(repos, dict) or len(repos) > 100:
        raise ManifestError("repos: expected mapping of at most 100 entries")
    for key, value in repos.items():
        string(key, f"repos.{key}", 201, REPOSITORY_PATTERN)
        if not isinstance(value, str) or not 1 <= len(value) <= 260 or any(char in value for char in "\0\n\r"):
            raise ManifestError(f"repos.{key}: expected path string of 1 to 260 characters")
    if "integration_branch" in data:
        branch = string(data["integration_branch"], "integration_branch", 100, r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,99}")
        if ".." in branch or branch.endswith(("/", ".lock")):
            raise ManifestError("integration_branch: not a branch name")
    target_text = ("manifest", "dockerfile", "deploy_command", "note", "gap")
    for target, path in identified(data["targets"], "targets", 0, 50):
        mapping(target, path, ("id", "exposure", "deploy_shape"), (*target_text, "paths", "restart_semantics", "evidence", "companions"))
        choice(target["exposure"], f"{path}.exposure", ("ga_public", "pre_ga", "unknown"))
        choice(target["deploy_shape"], f"{path}.deploy_shape", ("ecs-fargate", "vercel", "codebuild", "systemd", "installer", "e2b-template", "manual", "other"))
        if "paths" in target:
            globs(target["paths"], f"{path}.paths")
        for key in target_text:
            if key in target:
                string(target[key], f"{path}.{key}", 512)
        if "restart_semantics" in target:
            string(target["restart_semantics"], f"{path}.restart_semantics", 500)
        for key in ("evidence", "companions"):
            if key in target:
                strings(target[key], f"{path}.{key}")
    contract_text = {"evidence_producer": 512, "adoption_law": 512, "known_broken": 256, "reason_source": 512, "note": 1000}
    for contract, path in identified(data["contracts"], "contracts", 0, 100):
        mapping(contract, path, ("id", "kind", "producer", "consumers"), (*contract_text, "artifact", "companions", "outcome"))
        choice(contract["kind"], f"{path}.kind", ("http", "data-schema", "metric-dimension-set", "file", "other"))
        endpoint(contract["producer"], f"{path}.producer")
        for i, consumer in enumerate(sequence(contract["consumers"], f"{path}.consumers", 1, 20)):
            endpoint(consumer, f"{path}.consumers[{i}]", consumer=True)
        for key, maximum in contract_text.items():
            if key in contract:
                string(contract[key], f"{path}.{key}", maximum)
        for key in ("artifact", "companions"):
            if key in contract:
                strings(contract[key], f"{path}.{key}")
        if "outcome" in contract:
            choice(contract["outcome"], f"{path}.outcome", OUTCOMES)
    for i, companion in enumerate(sequence(data["companions"], "companions", 0, 200)):
        path = f"companions[{i}]"
        mapping(companion, path, ("path", "moves_with"), ("enforced_by", "outcome"))
        globs([companion["path"]], f"{path}.path", 1, 1)
        strings(companion["moves_with"], f"{path}.moves_with", 1)
        if "enforced_by" in companion:
            string(companion["enforced_by"], f"{path}.enforced_by", 512)
        if "outcome" in companion:
            choice(companion["outcome"], f"{path}.outcome", OUTCOMES)
    concerns = data.setdefault("concerns", {})
    mapping(concerns, "concerns", (), CONCERNS)
    for name in CONCERNS:
        concern = concerns.setdefault(name, {})
        path = f"concerns.{name}"
        mapping(concern, path, (), ("evidence", "monitor_obligation", "outcome_default"))
        strings(concern.setdefault("evidence", []), f"{path}.evidence")
        if type(concern.setdefault("monitor_obligation", False)) is not bool:
            raise ManifestError(f"{path}.monitor_obligation: expected bool")
        choice(concern.setdefault("outcome_default", "unresolved"), f"{path}.outcome_default", OUTCOMES)
    return data


def load_manifest(path):
    try:
        return validate_manifest(yaml.safe_load(read_text(path)))
    except (OSError, yaml.YAMLError, RecursionError) as exc:
        raise ManifestError(f"{path}: {exc}") from exc


def manifest_digest(loaded):
    canonical = json.dumps(loaded, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Coverage:
    tracked: int
    mapped: int
    listed: int
    uncovered_paths: list[str]

    @property
    def uncovered(self):
        return len(self.uncovered_paths)


def coverage(loaded, paths, manifest_path="ASPECTS.yaml"):
    # Coverage is bounded to 200k paths and reports sorted uncovered paths.
    paths = list(islice(paths, MAX_PATHS + 1))
    if len(paths) > MAX_PATHS:
        raise ManifestError("tree listing exceeds 200,000 paths limit")
    patterns = [p for component in loaded["components"] for p in component["paths"]]
    mapped = listed = 0
    uncovered = []
    for path in sorted(paths):
        if path == Path(manifest_path).name:
            listed += 1
        elif match_path(patterns, path):
            mapped += 1
        elif match_path(loaded["unmapped"], path):
            listed += 1
        else:
            uncovered.append(path)
    return Coverage(len(paths), mapped, listed, uncovered)


def contract_counts(loaded):
    counts = {"rows": 0, "pinned": 0, "unresolved": 0, "head": 0}
    for contract in loaded["contracts"]:
        for item in [contract["producer"], *contract["consumers"]]:
            counts["rows"] += 1
            revision = item["revision"]
            counts["head" if revision == "HEAD" else "unresolved" if revision == "unresolved" else "pinned"] += 1
    return counts
