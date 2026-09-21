"""Bounded change records, atomic persistence, receipts, and dispositions."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

import yaml

try:
    from .index import home_path
    from .manifest import (CONCERNS, OUTCOMES, ID_PATTERN, REPOSITORY_PATTERN,
                           ManifestError, choice, mapping, sequence, string)
except ImportError:
    from index import home_path
    from manifest import (CONCERNS, OUTCOMES, ID_PATTERN, REPOSITORY_PATTERN,
                          ManifestError, choice, mapping, sequence, string)

MAX_BYTES = 8 * 1024 * 1024
CHANGE_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
SUBJECT = r"[A-Za-z0-9._/@+:-]{1,200}"
ROW_ID = rf"(?:{'|'.join(CONCERNS)}):{SUBJECT}@[0-9a-f]{{12}}"
TASK_ID = r"[A-Za-z0-9._#/-]{1,128}"


@dataclass
class Row:
    id: str
    concern: str
    subject: str
    family: str | None = None
    required: bool = True
    outcome: str = "unresolved"
    evidence: str | None = None
    reason: str | None = None
    task: str | None = None
    superseded_by: str | None = None


@dataclass
class ChangeRecord:
    change_id: str
    repository: str
    project_id: str
    manifest_digest: str
    checker_version: str
    schema_version: int = 1
    base: str | None = None
    head: str | None = None
    touched: dict = field(default_factory=lambda: {key: [] for key in
                         ("paths", "components", "contracts", "targets", "companions")})
    rows: list = field(default_factory=list)
    dispositions: list = field(default_factory=list)
    carried: list = field(default_factory=list)


@dataclass
class Receipt:
    change_id: str
    repository: str
    project_id: str
    base: str
    head: str
    manifest_digest: str
    base_manifest_digest: str | None
    checker_version: str
    host: str
    changed_paths: list[str]
    touched: dict
    rows: list[dict]
    summary: dict
    verdict: str
    transaction: dict | None = None
    schema_version: int = 1


def row_id(concern, subject, digest):
    value = f"{concern}:{subject}@{digest[:12]}"
    string(value, "row.id", 256, ROW_ID)
    return value


def new_record(change_id, manifest, digest, version):
    return asdict(ChangeRecord(change_id, manifest["repository"], manifest["project_id"], digest, version))


def new_row(concern, subject, digest, **kwargs):
    return asdict(Row(row_id(concern, subject, digest), concern, subject, **kwargs))


def sort_rows(rows):
    return sorted(rows, key=lambda row: (CONCERNS.index(row["concern"]), row["subject"], row["id"]))


def _nullable(value, path, maximum, pattern=None):
    if value is not None:
        string(value, path, maximum, pattern)


def validate_record(data):
    # Every mapping rejects unknown keys; outcomes may be incomplete until check.
    keys = tuple(ChangeRecord.__dataclass_fields__)
    mapping(data, "record", keys)
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ManifestError("record.schema_version: expected integer 1")
    string(data["change_id"], "change_id", 128, CHANGE_ID)
    string(data["repository"], "repository", 201, REPOSITORY_PATTERN)
    string(data["project_id"], "project_id", 64, ID_PATTERN)
    string(data["manifest_digest"], "manifest_digest", 64, r"[0-9a-f]{64}")
    string(data["checker_version"], "checker_version", 64, r"[0-9]+\.[0-9]+\.[0-9]+")
    _nullable(data["base"], "base", 40, r"[0-9a-f]{40}")
    _nullable(data["head"], "head", 40, r"(?:[0-9a-f]{40}|worktree)")
    mapping(data["touched"], "touched", ("paths", "components", "contracts", "targets", "companions"))
    for key, values in data["touched"].items():
        for value in sequence(values, f"touched.{key}", 0, 5000):
            string(value, f"touched.{key}", 512, ID_PATTERN if key in ("components", "contracts", "targets") else None)
            if key in ("paths", "companions") and (value.startswith("/") or re.match(r"[A-Za-z]:", value)
                                                    or "\\" in value or ".." in value.split("/")):
                raise ManifestError(f"touched.{key}: expected relative forward-slash path")
    seen = set()
    for row in sequence(data["rows"], "rows", 0, 500):
        mapping(row, "row", tuple(Row.__dataclass_fields__))
        string(row["id"], "row.id", 256, ROW_ID)
        choice(row["concern"], "row.concern", CONCERNS)
        string(row["subject"], "row.subject", 200, SUBJECT)
        if not row["id"].startswith(f"{row['concern']}:{row['subject']}@"):
            raise ManifestError("row.id: does not match concern and subject")
        if row["id"] in seen:
            raise ManifestError(f"duplicate row id: {row['id']}")
        seen.add(row["id"])
        _nullable(row["family"], "row.family", 64, r"[a-z0-9][a-z0-9_-]{0,63}")
        if type(row["required"]) is not bool:
            raise ManifestError("row.required: expected bool")
        choice(row["outcome"], "row.outcome", OUTCOMES)
        _nullable(row["evidence"], "row.evidence", 512)
        _nullable(row["reason"], "row.reason", 1000)
        _nullable(row["task"], "row.task", 128, TASK_ID)
        # Dispositions name the resolving change; legacy row-id links also remain valid.
        _nullable(row["superseded_by"], "row.superseded_by", 256, rf"(?:{CHANGE_ID}|{ROW_ID})")
    for item in sequence(data["dispositions"], "dispositions", 0, 500):
        mapping(item, "disposition", ("row", "action", "by", "at", "detail"))
        string(item["row"], "disposition.row", 256, ROW_ID)
        choice(item["action"], "disposition.action", ("dismiss", "resolve", "defer"))
        string(item["by"], "disposition.by", 128)
        string(item["detail"], "disposition.detail", 1000)
        string(item["at"], "disposition.at", 40)
        try:
            stamp = datetime.fromisoformat(item["at"].replace("Z", "+00:00"))
            if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
                raise ValueError("not UTC")
        except ValueError as exc:
            raise ManifestError("disposition.at: expected ISO 8601 UTC") from exc
    for item in sequence(data["carried"], "carried", 0, 500):
        mapping(item, "carried", ("row", "from_change_id", "outcome"))
        string(item["row"], "carried.row", 256, ROW_ID)
        string(item["from_change_id"], "carried.from_change_id", 128, CHANGE_ID)
        choice(item["outcome"], "carried.outcome", OUTCOMES)
    return deepcopy(data)


def load_record(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ManifestError("record exceeds 8 MiB limit")
    try:
        return validate_record(yaml.safe_load(raw.decode("utf-8")))
    except (UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise ManifestError(f"invalid record: {exc}") from exc


def atomic_write(path, text):
    # A same-directory temporary file guarantees readers see a complete replacement.
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ManifestError("output exceeds 8 MiB limit")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=destination.parent, prefix=".impact-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def save_record(path, data):
    validated = validate_record(asdict(data) if isinstance(data, ChangeRecord) else data)
    atomic_write(path, yaml.safe_dump(validated, sort_keys=False, allow_unicode=False))


def write_receipt(path, receipt):
    data = asdict(receipt) if isinstance(receipt, Receipt) else receipt
    atomic_write(path, json.dumps(data, sort_keys=True, indent=1, ensure_ascii=True) + "\n")
    meta = {"generated_at": datetime.now(timezone.utc).isoformat(),
            "session": os.environ.get("CLAUDE_CODE_SESSION_ID")}
    atomic_write(str(path) + ".meta.json", json.dumps(meta, sort_keys=True, indent=1) + "\n")


def prior_records():
    # The bounded scan skips unreadable records and sorts the resulting paths.
    from itertools import islice
    root = home_path() / "pair-runs"
    paths = sorted(islice(root.glob("*/impact/record.yaml"), 5000))
    for path in paths:
        try:
            yield path, load_record(path)
        except (OSError, ValueError, yaml.YAMLError, RecursionError):
            continue


def carried_rows(repository, change_id):
    from heapq import nsmallest
    def entries():
        for _, previous in prior_records():
            if previous["repository"] != repository or previous["change_id"] == change_id:
                continue
            for row in previous["rows"]:
                if row["outcome"] == "unresolved" and row["superseded_by"] is None:
                    yield {"row": row["id"], "from_change_id": previous["change_id"], "outcome": "unresolved"}
    return nsmallest(200, entries(), key=lambda item: (item["from_change_id"], item["row"]))


def dispose(args):
    data = load_record(args.record)
    row = next((row for row in data["rows"] if row["id"] == args.row), None)
    if row is None:
        print("no such row")
        return 1
    action = args.verb
    if action == "dismiss":
        string(args.reason, "reason", 989)
        row.update(outcome="not-applicable", reason=f"dismissed: {args.reason}")
        detail = args.reason
    elif action == "resolve":
        string(args.evidence, "evidence", 512)
        row.update(outcome=args.outcome, evidence=args.evidence)
        detail = args.evidence
    else:
        string(args.task, "task", 128, TASK_ID)
        row.update(outcome="deferred", task=args.task)
        detail = args.task
    data["dispositions"].append({"row": args.row, "action": action,
                                 "by": os.environ.get("CLAUDE_CODE_SESSION_ID", "unknown"),
                                 "at": datetime.now(timezone.utc).isoformat(), "detail": detail})
    save_record(args.record, data)
    for path, previous in prior_records():
        if (path.resolve() == Path(args.record).resolve() or previous["repository"] != data["repository"]
                or previous["change_id"] == data["change_id"]):
            continue
        modified = False
        for other in previous["rows"]:
            if other["id"] == args.row and other["outcome"] == "unresolved":
                other["superseded_by"] = data["change_id"]
                modified = True
        if modified:
            save_record(path, previous)
    print(f"disposition: {action} {args.row}")
    return 0
