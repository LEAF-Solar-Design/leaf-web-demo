#!/usr/bin/env python3
"""Solar parity oracle: the done-check for the Branch2025 to Studio parity program.

Reads the parity ledger and the receipt tree, and answers one question: is the
requested parity scope met. Exit 0 pass, 1 parity not met, 2 usage error or
unreadable input.

Contract, stated here so every reader and every future edit conditions on it:

* FAILS CLOSED. Any exception, missing file, invalid JSON, unknown enum value or
  schema violation is exit 2 with one line on stderr. Silence is never a pass.
* BOUNDED. Every input carries a hard ceiling (ledger bytes, row count, receipt
  bytes, receipt file count). Nothing here reads an unbounded stream.
* Stdlib only, no network, no writes, deterministic output ordering.
* Single pass over rows and one bounded walk of the receipt tree: no quadratic
  scan, no per-row directory listing.
* A DECLARED DIVERGENCE is the one way a failing comparator settles a
  capability, and it is fail closed: the exact declared diff set, a committed
  finding under docs/parity/divergences/, and every receipt rule still passing.
  It is a claim about the PLUGIN, never a licence for Studio to be wrong.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path

SCHEMA_LEDGER = "leaf.solar-parity-ledger.v1"
SCHEMA_RECEIPT = "leaf.solar-parity-receipt.v1"

# Hard input ceilings. Refusing is always cheaper than reading.
MAX_LEDGER_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_FILES = 20000
MAX_REPORT_LINES = 200
MAX_DIVERGENCE_LINES = 20

# A declared divergence may only cite a document committed here, repo-relative.
DIVERGENCE_DIR = "docs/parity/divergences"

CLASSES = ("T", "F", "P", "V", "H", "A")
MATURITIES = ("production", "preview", "tutorial", "internal")
ENGINES = (
    "browser",
    "cloud-service",
    "server-builtin",
    "dotnet-worker",
    "autocad-lane",
    "platform",
    "none",
)
STATUSES = ("resolved", "unresolved")
VERDICTS = ("pass", "fail")
COMPARATORS = ("exact-counts-by-layer", "solar-w1-semantic")
WAVES = (0, 1, 2, 3, 4, 5)
REQUIRE_CHOICES = ("w0", "w1", "w2", "w3", "w4", "w5", "all-production")

DUTY_CLASSES = ("T", "F")
EXCLUDABLE_CLASSES = ("P", "V", "H")

KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

ROW_KEYS = (
    "global",
    "class",
    "maturity",
    "capability",
    "capability_version",
    "family",
    "interaction",
    "engine",
    "wave",
    "status",
    "exclusion_rationale",
    "alias_of",
    "evidence",
)

RECEIPT_KEYS = (
    "schema",
    "capability",
    "capability_version",
    "fixture",
    "plugin",
    "studio",
    "comparator",
    "synthetic_fields",
    "fallback_fields",
    "synthetic_flagged",
    "survived_reopen",
    "produced_at",
    "comparison",
    "divergence",
)

DIVERGENCE_KEYS = ("finding", "declared_diffs", "summary")


class InputError(Exception):
    """The input cannot be trusted. Always exit 2, never a verdict."""


# --------------------------------------------------------------------------
# field readers: every one of these raises InputError rather than coercing
# --------------------------------------------------------------------------


def _str_field(raw, key, where, *, required=False, allow_empty=True, default=""):
    if key not in raw or raw[key] is None:
        if required:
            raise InputError(f"{where}: missing required field {key!r}")
        return default
    value = raw[key]
    if not isinstance(value, str):
        raise InputError(f"{where}: field {key!r} must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise InputError(f"{where}: field {key!r} must not be empty")
    return value


def _enum_field(raw, key, where, allowed, *, required=False, default=None):
    if key not in raw or raw[key] is None:
        if required:
            raise InputError(f"{where}: missing required field {key!r}")
        return default
    value = raw[key]
    if not isinstance(value, str) or value not in allowed:
        raise InputError(f"{where}: field {key!r} has unknown value {value!r}, allowed {list(allowed)}")
    return value


def _bool_field(raw, key, where):
    if key not in raw or not isinstance(raw[key], bool):
        raise InputError(f"{where}: field {key!r} must be a boolean")
    return raw[key]


def _list_of_str(raw, key, where):
    if key not in raw or not isinstance(raw[key], list):
        raise InputError(f"{where}: field {key!r} must be an array")
    for item in raw[key]:
        if not isinstance(item, str):
            raise InputError(f"{where}: field {key!r} must contain only strings")
    return list(raw[key])


def _object_field(raw, key, where):
    if key not in raw or not isinstance(raw[key], dict):
        raise InputError(f"{where}: field {key!r} must be an object")
    return raw[key]


def _kebab_field(raw, key, where, *, required=True):
    value = _str_field(raw, key, where, required=required, allow_empty=False)
    if not KEBAB.match(value):
        raise InputError(f"{where}: field {key!r} must be kebab-case, got {value!r}")
    return value


def _wave_field(raw, where):
    if "wave" not in raw or raw["wave"] is None:
        return None
    value = raw["wave"]
    if isinstance(value, bool) or not isinstance(value, int) or value not in WAVES:
        raise InputError(f"{where}: field 'wave' must be null or an integer in {list(WAVES)}, got {value!r}")
    return value


def _reject_unknown(raw, known, where):
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise InputError(f"{where}: unknown field(s) {unknown}")


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def load_json(path, max_bytes, label):
    """Read one bounded JSON document. Fails closed on every I/O and parse fault."""
    try:
        stat = path.stat()
    except OSError as exc:
        raise InputError(f"{label} unreadable: {path}: {exc}") from exc
    if not path.is_file():
        raise InputError(f"{label} is not a file: {path}")
    if stat.st_size > max_bytes:
        raise InputError(f"{label} too large: {path} is {stat.st_size} bytes, limit {max_bytes}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"{label} unreadable: {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"{label} is not valid JSON: {path}: {exc}") from exc


def parse_ledger(path):
    """Return (registrations_expected, rows). Structure is validated here, policy is not."""
    doc = load_json(path, MAX_LEDGER_BYTES, "ledger")
    if not isinstance(doc, dict):
        raise InputError("ledger: root must be a JSON object")
    schema = doc.get("schema")
    if schema != SCHEMA_LEDGER:
        raise InputError(f"ledger: schema must be {SCHEMA_LEDGER!r}, got {schema!r}")
    expected = doc.get("registrations_expected")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise InputError("ledger: registrations_expected must be a non-negative integer")
    rows = doc.get("rows")
    if not isinstance(rows, list):
        raise InputError("ledger: rows must be an array")
    if len(rows) > MAX_ROWS:
        raise InputError(f"ledger: {len(rows)} rows exceeds the limit of {MAX_ROWS}")
    return expected, [parse_row(raw, index) for index, raw in enumerate(rows)]


def parse_row(raw, index):
    if not isinstance(raw, dict):
        raise InputError(f"ledger row {index}: must be a JSON object")
    where = f"ledger row {index}"
    identifier = _str_field(raw, "global", where, required=True, allow_empty=False)
    where = f"ledger row {index} ({identifier})"
    _reject_unknown(raw, ROW_KEYS, where)
    return {
        "global": identifier,
        "class": _enum_field(raw, "class", where, CLASSES, required=True),
        "maturity": _enum_field(raw, "maturity", where, MATURITIES, required=True),
        "capability": _kebab_field(raw, "capability", where),
        "capability_version": _str_field(raw, "capability_version", where, default="0"),
        "family": _str_field(raw, "family", where),
        "interaction": _str_field(raw, "interaction", where),
        "engine": _enum_field(raw, "engine", where, ENGINES, default="none"),
        "wave": _wave_field(raw, where),
        "status": _enum_field(raw, "status", where, STATUSES, required=True),
        "exclusion_rationale": _str_field(raw, "exclusion_rationale", where),
        "alias_of": _str_field(raw, "alias_of", where),
        "evidence": _str_field(raw, "evidence", where),
    }


def parse_divergence(doc, where):
    """Read the optional divergence block. Shape faults are input errors, never a verdict."""
    if "divergence" not in doc or doc["divergence"] is None:
        return None
    block = _object_field(doc, "divergence", where)
    where = f"{where} divergence"
    _reject_unknown(block, DIVERGENCE_KEYS, where)
    declared = _list_of_str(block, "declared_diffs", where)
    if not declared:
        raise InputError(f"{where}: field 'declared_diffs' must name at least one diff")
    return {
        "finding": _str_field(block, "finding", where, required=True, allow_empty=False),
        "declared_diffs": declared,
        "summary": _str_field(block, "summary", where, required=True, allow_empty=False),
    }


def parse_receipt(path, capability):
    """Read one receipt and check its shape. A receipt filed under the wrong capability is a fault."""
    doc = load_json(path, MAX_RECEIPT_BYTES, "receipt")
    where = f"receipt {path.name} under {capability}"
    if not isinstance(doc, dict):
        raise InputError(f"{where}: root must be a JSON object")
    _reject_unknown(doc, RECEIPT_KEYS, where)
    if doc.get("schema") != SCHEMA_RECEIPT:
        raise InputError(f"{where}: schema must be {SCHEMA_RECEIPT!r}, got {doc.get('schema')!r}")
    declared = _kebab_field(doc, "capability", where)
    if declared != capability:
        raise InputError(f"{where}: declares capability {declared!r} but is filed under {capability!r}")

    fixture = _object_field(doc, "fixture", where)
    plugin = _object_field(doc, "plugin", where)
    studio = _object_field(doc, "studio", where)
    comparator = _object_field(doc, "comparator", where)

    receipt = {
        "path": path,
        "capability": declared,
        "capability_version": _str_field(doc, "capability_version", where, required=True),
        "fixture": {
            "id": _str_field(fixture, "id", f"{where} fixture", required=True, allow_empty=False),
            "sha256": _str_field(fixture, "sha256", f"{where} fixture", required=True, allow_empty=False),
        },
        "plugin": {
            "build": _str_field(plugin, "build", f"{where} plugin", required=True),
            "state": _str_field(plugin, "state", f"{where} plugin", required=True, allow_empty=False),
            "receipt_sha256": _str_field(plugin, "receipt_sha256", f"{where} plugin", required=True),
        },
        "studio": {
            "capability_version": _str_field(studio, "capability_version", f"{where} studio", required=True),
            "engine": _enum_field(studio, "engine", f"{where} studio", ENGINES, required=True),
        },
        "comparator": {
            "name": _enum_field(comparator, "name", f"{where} comparator", COMPARATORS, required=True),
            "version": _str_field(comparator, "version", f"{where} comparator", required=True),
            "verdict": _enum_field(comparator, "verdict", f"{where} comparator", VERDICTS, required=True),
            "diffs": _list_of_str(comparator, "diffs", f"{where} comparator"),
        },
        "synthetic_fields": _list_of_str(doc, "synthetic_fields", where),
        "fallback_fields": _list_of_str(doc, "fallback_fields", where),
        "synthetic_flagged": _bool_field(doc, "synthetic_flagged", where),
        "survived_reopen": _bool_field(doc, "survived_reopen", where),
        "produced_at": _str_field(doc, "produced_at", where, required=True, allow_empty=False),
        "divergence": parse_divergence(doc, where),
    }
    if receipt["comparator"]["verdict"] == "pass" and receipt["comparator"]["diffs"]:
        raise InputError(f"{where}: passing comparator must have no diffs")
    if receipt["comparator"]["verdict"] == "pass" and receipt["divergence"] is not None:
        raise InputError(f"{where}: a divergence block requires comparator verdict 'fail'")
    if "comparison" in doc:
        # Load the sibling explicitly: PYTHONSAFEPATH excludes the script directory.
        spec = importlib.util.spec_from_file_location(
            "solar_w1_compare", Path(__file__).with_name("solar_w1_compare.py")
        )
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
            comparison = doc["comparison"]
            actual = module.compare_document(comparison)
            if comparison["capability"] != declared:
                raise ValueError("comparison capability disagrees with receipt")
            for side in ("plugin", "studio"):
                evidence = comparison[side]
                if evidence["fixture_sha256"] != receipt["fixture"]["sha256"]:
                    raise ValueError("comparison fixture disagrees with receipt")
                if evidence["survived_reopen"] != receipt["survived_reopen"]:
                    raise ValueError("comparison reopen state disagrees with receipt")
            if comparison["studio"]["versions"]["capability"] != receipt["capability_version"]:
                raise ValueError("comparison capability version disagrees with receipt")
            if actual != receipt["comparator"]:
                raise ValueError("comparator block disagrees with executable comparison")
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            raise InputError(f"{where}: invalid comparison: {exc}") from exc
    elif receipt["comparator"]["name"] == "solar-w1-semantic":
        raise InputError(f"{where}: solar-w1-semantic requires comparison evidence")
    return receipt


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------


def finding(code, detail, *, row_global=None, capability=None):
    item = {"code": code}
    if capability is not None:
        item["capability"] = capability
    else:
        item["global"] = row_global
    item["detail"] = detail
    return item


def finding_name(item):
    return item.get("capability") or item.get("global") or ""


def ledger_findings(expected, rows):
    """Checks that apply to the ledger itself, over ALL rows, in every scope."""
    findings = []
    if len(rows) != expected:
        findings.append(
            finding(
                "ROW_COUNT",
                f"ledger carries {len(rows)} rows, registrations_expected is {expected}",
            )
        )

    index = {}
    duplicates = []
    for row in rows:
        identifier = row["global"]
        if identifier in index:
            if identifier not in duplicates:
                duplicates.append(identifier)
        else:
            index[identifier] = row
    for identifier in duplicates:
        findings.append(finding("DUPLICATE_GLOBAL", "global appears more than once", row_global=identifier))

    for row in rows:
        identifier = row["global"]
        if row["status"] == "unresolved":
            findings.append(finding("UNRESOLVED", "row status is unresolved", row_global=identifier))
        if row["class"] == "A":
            target = index.get(row["alias_of"]) if row["alias_of"] else None
            if target is None:
                findings.append(
                    finding(
                        "ALIAS_DANGLING",
                        f"alias_of {row['alias_of']!r} names no row",
                        row_global=identifier,
                    )
                )
            elif target["class"] == "A":
                findings.append(
                    finding(
                        "ALIAS_DANGLING",
                        f"alias_of {row['alias_of']!r} names another alias row",
                        row_global=identifier,
                    )
                )
        if row["class"] in EXCLUDABLE_CLASSES and row["wave"] is None and not row["exclusion_rationale"].strip():
            findings.append(
                finding(
                    "EXCLUSION_MISSING",
                    f"class {row['class']} row with no wave carries no exclusion_rationale",
                    row_global=identifier,
                )
            )
        if row["class"] in DUTY_CLASSES and row["maturity"] == "production" and row["wave"] is None:
            findings.append(
                finding(
                    "WAVE_MISSING",
                    f"class {row['class']} production row carries no wave",
                    row_global=identifier,
                )
            )
    return findings


def has_duty(row):
    """Parity duty: class T or F at production maturity. Nothing else owes a receipt."""
    return row["class"] in DUTY_CLASSES and row["maturity"] == "production"


def in_scope(row, max_wave):
    if not has_duty(row):
        return False
    if max_wave is None:
        return True
    return row["wave"] is not None and row["wave"] <= max_wave


def scope_wave(require):
    """None means every duty row (all-production and the default report)."""
    if require is None or require == "all-production":
        return None
    return int(require[1:])


def receipt_index(receipts_dir):
    """One bounded walk of the receipt tree: capability -> sorted file list."""
    by_capability = {}
    if not receipts_dir.exists():
        return by_capability
    if not receipts_dir.is_dir():
        raise InputError(f"receipts path is not a directory: {receipts_dir}")
    total = 0
    try:
        for path in sorted(receipts_dir.rglob("*.json")):
            total += 1
            if total > MAX_RECEIPT_FILES:
                raise InputError(f"receipt tree exceeds the limit of {MAX_RECEIPT_FILES} files")
            capability = path.parent.name
            by_capability.setdefault(capability, []).append(path)
    except OSError as exc:
        raise InputError(f"receipt tree unreadable: {receipts_dir}: {exc}") from exc
    return by_capability


def receipt_violations(receipt, expected_versions):
    """Rules that disqualify an otherwise passing receipt. Order is fixed, not incidental."""
    codes = []
    if (
        receipt["capability_version"] not in expected_versions
        or receipt["studio"]["capability_version"] != receipt["capability_version"]
    ):
        codes.append("RECEIPT_STALE")
    if (receipt["synthetic_fields"] or receipt["fallback_fields"]) and receipt["synthetic_flagged"] is not True:
        codes.append("RECEIPT_SYNTHETIC")
    if receipt["plugin"]["state"] != "committed":
        codes.append("RECEIPT_NOT_COMMITTED")
    if receipt["survived_reopen"] is not True:
        codes.append("RECEIPT_NO_REOPEN")
    return codes


def finding_document_problem(root, reference):
    """Why this finding reference is not a committed document under DIVERGENCE_DIR, or None.

    Fails closed: a traversal segment, a backslash, a foreign directory and an
    absent file are all refusals, so a divergence can never cite a path the repo
    does not actually carry.
    """
    if "\\" in reference:
        return f"finding {reference!r} must use forward slashes"
    parts = reference.split("/")
    prefix = DIVERGENCE_DIR.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return f"finding {reference!r} must be a plain repo-relative path"
    if parts[: len(prefix)] != prefix or len(parts) <= len(prefix):
        return f"finding {reference!r} is not under {DIVERGENCE_DIR}/"
    if not reference.endswith(".md"):
        return f"finding {reference!r} is not a Markdown document"
    path = root.joinpath(*parts)
    # is_file() follows symlinks, so a committed link under DIVERGENCE_DIR could cite a file
    # anywhere. Refuse the link itself, and refuse any path that RESOLVES outside the resolved
    # directory, which also catches a symlinked parent directory.
    if path.is_symlink():
        return f"finding {reference!r} is a symlink, not a committed document"
    try:
        resolved = path.resolve(strict=True)
        base = root.joinpath(*prefix).resolve(strict=True)
    except (OSError, RuntimeError):
        return f"finding {reference!r} names no committed file under {root}"
    if not resolved.is_relative_to(base) or resolved == base:
        return f"finding {reference!r} resolves outside {DIVERGENCE_DIR}/"
    if not resolved.is_file():
        return f"finding {reference!r} names no committed file under {root}"
    return None


def divergence_problem(receipt, expected_versions, root):
    """Why this failing receipt does NOT settle its capability, or None when it does.

    All four conditions are checked, in a fixed order, and every one of them is a
    refusal by default: the receipt rules, the exact declared diff set, and the
    committed finding.
    """
    name = receipt["path"].name
    violations = receipt_violations(receipt, expected_versions)
    if violations:
        return f"{name} fails {', '.join(violations)}"
    produced = set(receipt["comparator"]["diffs"])
    declared = set(receipt["divergence"]["declared_diffs"])
    undeclared = sorted(produced - declared)
    unproduced = sorted(declared - produced)
    if undeclared or unproduced:
        parts = []
        if undeclared:
            parts.append(f"undeclared diff(s) {undeclared}")
        if unproduced:
            parts.append(f"declared diff(s) {unproduced} not produced")
        return f"{name} diff set disagrees: " + " and ".join(parts)
    problem = finding_document_problem(root, receipt["divergence"]["finding"])
    if problem:
        return f"{name} {problem}"
    return None


def capability_findings(capability, expected_versions, files, root):
    """Evaluate one capability once, however many rows share it.

    Returns (findings, divergence). A clean passing receipt always wins; a declared
    divergence settles the capability only when every condition in divergence_problem
    holds, and a declared divergence that fails one is named, never silently dropped.
    """
    if not files:
        return [finding("RECEIPT_MISSING", "no receipt under receipts/" + capability, capability=capability)], None

    # Parse every receipt before judging: a malformed sibling is a fault, not a pass.
    receipts = [parse_receipt(path, capability) for path in files]
    passing = [r for r in receipts if r["comparator"]["verdict"] == "pass"]
    declared = [r for r in receipts if r["divergence"] is not None]
    if not passing and not declared:
        return [
            finding(
                "RECEIPT_FAIL",
                f"all {len(receipts)} receipt(s) carry comparator verdict fail",
                capability=capability,
            )
        ], None

    codes = []
    for receipt in passing:
        violations = receipt_violations(receipt, expected_versions)
        if not violations:
            return [], None  # one clean passing receipt settles the capability
        for code in violations:
            if code not in codes:
                codes.append(code)

    refusals = []
    for receipt in declared:
        problem = divergence_problem(receipt, expected_versions, root)
        if problem is None:
            # Only reached with no clean passing receipt: a clean pass wins above.
            return [], {
                "capability": capability,
                "finding": receipt["divergence"]["finding"],
                "summary": receipt["divergence"]["summary"],
            }
        if problem not in refusals:
            refusals.append(problem)

    details = {
        "RECEIPT_STALE": "every passing receipt names a capability_version the ledger does not expect, "
        "or disagrees with its own studio capability_version",
        "RECEIPT_SYNTHETIC": "every passing receipt carries synthetic or fallback fields without synthetic_flagged",
        "RECEIPT_NOT_COMMITTED": "every passing receipt was produced against a plugin state other than committed",
        "RECEIPT_NO_REOPEN": "every passing receipt failed to record survived_reopen",
    }
    found = [finding(code, details[code], capability=capability) for code in sorted(codes)]
    if refusals:
        found.append(
            finding(
                "RECEIPT_DIVERGENCE_INVALID",
                "no declared divergence holds: " + "; ".join(refusals),
                capability=capability,
            )
        )
    return found, None


UI_FLAGS = ("VITE_CAD_EDIT", "VITE_SOLAR_FLOW_RAIL", "VITE_SOLAR_SETTINGS_FORM")
MAX_UI_BYTES = 65536
MAX_UI_DECLARATIONS = 256


def _ui_text(path):
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_UI_BYTES + 1)
        if len(data) > MAX_UI_BYTES:
            raise InputError("UI source exceeds 65536 bytes")
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"UI source unreadable: {path.name}: {exc}") from exc


def read_baked_flags(dockerfile_text):
    """Interpret the supported default build configuration, never the environment."""
    args, env = {}, {}
    global_args, stages = {}, set()
    before_from = True
    active = False
    seen = False
    built = False
    pending = ""
    flag_kinds = {}
    for physical in dockerfile_text.splitlines():
        line = physical.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        pending += line[:-1] + " " if continued else line
        if continued:
            continue
        line, pending = pending, ""
        instruction, _, body = line.partition(" ")
        instruction = instruction.upper()
        if instruction == "FROM":
            before_from = False
            stage = re.fullmatch(r"(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", body, re.I)
            if stage is None:
                raise InputError("unsupported FROM instruction")
            named = re.search(r"\bAS\s+build\s*$", body, re.I) is not None
            if named and ("$" in stage[1] or stage[1].lower() in stages):
                raise InputError("stage inheritance not supported")
            if stage[2] is not None:
                stages.add(stage[2].lower())
            if named and seen:
                raise InputError("duplicate build stage")
            active = named
            seen |= named
            continue
        global_arg = before_from and instruction == "ARG"
        if not global_arg and (not active or built):
            continue
        if instruction == "RUN" and re.match(r"npm\s+run\s+build(?:\s|$)", body):
            built = True
            continue
        if instruction not in ("ARG", "ENV"):
            if any(flag in line for flag in UI_FLAGS):
                raise InputError("unsupported flag instruction")
            continue
        if any(flag in body for flag in UI_FLAGS) and "\\" in body:
            raise InputError("unsupported escape in flag assignment")
        if any(flag in body for flag in UI_FLAGS) and ("'" in body or '"' in body):
            raise InputError("unsupported quoting in flag assignment")
        try:
            words = shlex.split(body)
        except ValueError as exc:
            raise InputError("invalid build assignment") from exc
        if instruction == "ARG":
            if len(words) != 1:
                raise InputError("unsupported ARG assignment")
            key, separator, value = words[0].partition("=")
            assignments = [(key, value if separator else global_args.get(key, ""))]
        elif all("=" in word for word in words) and words:
            assignments = [word.split("=", 1) for word in words]
        else:
            if any(flag in body for flag in UI_FLAGS):
                raise InputError("unsupported ENV assignment")
            continue
        preceding = global_args if global_arg else {**args, **env}
        assigned_flags = set()
        for key, value in assignments:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise InputError("invalid build assignment name")
            if key in UI_FLAGS:
                if key in assigned_flags:
                    raise InputError("ambiguous flag assignment")
                assigned_flags.add(key)
                # A flag reads only a literal or its own same-stage ARG: helper variables,
                # ARG-built defaults and repeated assignments are refused, never interpreted.
                if "$" in value and (instruction == "ARG" or value != "${" + key + "}"):
                    raise InputError("unsupported flag reference")
                if not global_arg:
                    kinds = flag_kinds.setdefault(key, [])
                    if instruction in kinds or (instruction == "ARG" and "ENV" in kinds):
                        raise InputError("ambiguous flag assignment")
                    kinds.append(instruction)
            substituted = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                                 lambda match: preceding.get(match[1], ""), value)
            if key in UI_FLAGS and ("$" in substituted or re.search(r"[^A-Za-z0-9_.:/-]", substituted)):
                raise InputError("unsupported flag value")
            (global_args if global_arg else args if instruction == "ARG" else env)[key] = substituted
    if pending or not seen or not built:
        raise InputError("unsupported or missing build stage")
    return {flag: env.get(flag) == "1" for flag in UI_FLAGS}


def _js_tokens(text):
    """A small lexer for literal tables, preserving strings while removing comments."""
    pattern = re.compile(r"\s+|//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|[A-Za-z_$][\w$]*|\d+|[^\s]")
    return [match[0] for match in pattern.finditer(text)
            if not match[0].isspace() and not match[0].startswith(("//", "/*"))]


def _js_group(tokens, start, opener, closer):
    if start >= len(tokens) or tokens[start] != opener:
        raise InputError("unsupported flow source syntax")
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index] == opener:
            depth += 1
        elif tokens[index] == closer:
            depth -= 1
            if depth == 0:
                return tokens[start + 1:index], index + 1
    raise InputError("unterminated flow source group")


def _js_literal(token):
    if len(token) < 2 or token[0] not in ("'", '"') or token[-1] != token[0] or "\\" in token:
        raise InputError("flow bindings require unescaped literal strings")
    return token[1:-1]


def _js_arguments(tokens):
    arguments, current, stack = [], [], []
    pairs = {"(": ")", "[": "]", "{": "}"}
    for token in tokens:
        if token == "," and not stack:
            if not current:
                raise InputError("empty flow argument")
            arguments.append(current)
            current = []
            continue
        if token in pairs:
            stack.append(pairs[token])
        elif token in pairs.values():
            if not stack or stack.pop() != token:
                raise InputError("unbalanced flow arguments")
        current.append(token)
    if stack:
        raise InputError("unbalanced flow arguments")
    if current:
        arguments.append(current)
    return arguments


def _js_strings(tokens):
    body, end = _js_group(tokens, 0, "[", "]")
    if end != len(tokens):
        raise InputError("unsupported capability expression")
    result = []
    for argument in _js_arguments(body):
        if len(argument) != 1:
            raise InputError("unsupported capability expression")
        name = _js_literal(argument[0])
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
            raise InputError("invalid flow capability name")
        result.append(name)
    return result


def read_flow_bindings(flow_text):
    tokens = _js_tokens(flow_text)
    limits = [i for i in range(len(tokens) - 2)
              if tokens[i:i + 3] == ["const", "MAX_FLOW_STEPS", "="]]
    if len(limits) != 1:
        raise InputError("missing or ambiguous MAX_FLOW_STEPS declaration")
    limit_start = limits[0]
    if (limit_start == 0 or tokens[limit_start - 1] != "export" or
            limit_start + 3 >= len(tokens) or
            not re.fullmatch(r"[0-9]+", tokens[limit_start + 3]) or
            (limit_start + 4 < len(tokens) and
             tokens[limit_start + 4] not in (";", "export", "const", "function"))):
        raise InputError("MAX_FLOW_STEPS must be an exported integer literal")
    max_flow_steps = int(tokens[limit_start + 3])
    starts = [i for i in range(len(tokens) - 2)
              if tokens[i:i + 3] == ["const", "SOLAR_FLOWS", "="]]
    if len(starts) != 1:
        raise InputError("missing or ambiguous SOLAR_FLOWS declaration")
    start = starts[0] + 3
    if tokens[start:start + 4] != ["Object", ".", "freeze", "("]:
        raise InputError("unsupported SOLAR_FLOWS declaration")
    table, end = _js_group(tokens, start + 4, "[", "]")
    if tokens[end:end + 1] != [")"]:
        raise InputError("unsupported SOLAR_FLOWS declaration")
    if end + 1 < len(tokens) and tokens[end + 1] not in (";", "export", "const", "function"):
        raise InputError("unsupported SOLAR_FLOWS expression")
    tools, rooftop = set(), False
    for entry in _js_arguments(table):
        if entry[:2] != ["flowEntry", "("]:
            raise InputError("unsupported flow entry")
        body, end = _js_group(entry, 1, "(", ")")
        arguments = _js_arguments(body)
        if end != len(entry) or len(arguments) != 4 or any(len(a) != 1 for a in arguments[:3]):
            raise InputError("unsupported flow entry")
        identifier, label, maturity = [_js_literal(a[0]) for a in arguments[:3]]
        stages = arguments[3]
        if stages == ["null"]:
            if identifier != "rooftop":
                raise InputError("unsupported null flow")
            rooftop = True
            continue
        body, end = _js_group(stages, 0, "[", "]")
        if end != len(stages):
            raise InputError("unsupported stages expression")
        for stage in _js_arguments(body):
            if stage[0] not in ("flowStage", "panelStage", "panelCatalogStage"):
                raise InputError("unsupported flow stage")
            body, end = _js_group(stage, 1, "(", ")")
            args = _js_arguments(body)
            allowed = (2, 3) if stage[0] == "flowStage" else (3,) if stage[0] == "panelStage" else (4,)
            if end != len(stage) or len(args) not in allowed or any(len(a) != 1 for a in args[:2]):
                raise InputError("unsupported flow stage")
            for a in args[:2]:
                _js_literal(a[0])
            arrays = [_js_strings(a) for a in args[2:]]
            if stage[0] == "flowStage" and arrays:
                tools.update(arrays[0])
    if rooftop:
        # Pin the complete selection function, not just a wave comparison substring.
        expected = """function solarFlowSteps(families) {
          const seen = new Set()
          const picked = []
          for (const family of Array.isArray(families) ? families : []) {
            const rows = family && typeof family === 'object' && Array.isArray(family.capabilities) ? family.capabilities : []
            for (const row of rows) {
              if (!row || typeof row !== 'object' || typeof row.name !== 'string' || row.name.length === 0) continue
              if (seen.has(row.name)) continue
              const result = solarView(row)
              if (result.state !== 'valid' || (result.view.wave !== 1 && row.name !== 'solar-string-conductors')) continue
              seen.add(row.name)
              picked.push({ row, order: row.name === 'solar-string-conductors' ? 75 : result.view.order })
            }
          }
          return picked.sort(compareSteps).slice(0, MAX_FLOW_STEPS).map(({ row }) => row)
        }"""
        signature = ["function", "solarFlowSteps", "(", "families", ")", "{"]
        matches = [i for i in range(len(tokens)) if tokens[i:i + 6] == signature]
        if len(matches) != 1:
            raise InputError("unsupported Rooftop selection rule")
        body, end = _js_group(tokens, matches[0] + 5, "{", "}")
        if tokens[matches[0]:end] != _js_tokens(expected):
            raise InputError("unsupported Rooftop selection rule")
        selects = [i for i in range(len(tokens) - 1)
                   if tokens[i:i + 2] == ["function", "solarFlowSelect"]]
        if len(selects) != 1:
            raise InputError("unsupported Rooftop null branch")
        parameters, end = _js_group(tokens, selects[0] + 2, "(", ")")
        selection, end = _js_group(tokens, end, "{", "}")
        branch = _js_tokens("if (flow.stages === null) { steps = solarFlowSteps(families) if (steps.length === 0) reasonKey = 'rooftop_steps_missing' }")
        if not any(selection[i:i + len(branch)] == branch for i in range(len(selection))):
            raise InputError("unsupported Rooftop null branch")
    return {"tools": sorted(tools), "rooftop": rooftop, "max_flow_steps": max_flow_steps}


def _ui_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate Solar declaration JSON key")
        result[key] = value
    return result


def _ui_constant(value):
    raise InputError("invalid Solar declaration JSON number")


def read_ui_sources(source_root):
    flags = read_baked_flags(_ui_text(source_root / "deploy" / "Dockerfile.web"))
    bindings = read_flow_bindings(_ui_text(source_root / "web" / "src" / "solar" / "solarFlowModel.js"))
    directory = source_root / "server" / "solar_tools"
    try:
        paths = []
        for path in directory.glob("*.json"):
            paths.append(path)
            if len(paths) > MAX_UI_DECLARATIONS:
                raise InputError("excessive Solar declarations")
        if not directory.is_dir() or not paths:
            raise InputError("missing or excessive Solar declarations")
        declarations, names = [], set()
        for path in sorted(paths):
            try:
                doc = json.loads(_ui_text(path), object_pairs_hook=_ui_pairs, parse_constant=_ui_constant)
            except (ValueError, RecursionError) as exc:
                raise InputError("invalid Solar declaration JSON") from exc
            if not isinstance(doc, dict) or doc.get("schema") != "leaf.solar-tool.v1":
                raise InputError("invalid Solar declaration schema")
            name = _kebab_field(doc, "name", "Solar declaration")
            if name in names:
                raise InputError("duplicate Solar declaration name")
            names.add(name)
            ledger = _list_of_str(doc, "ledger", name)
            if any(not KEBAB.fullmatch(capability) for capability in ledger):
                raise InputError("invalid declaration ledger mapping")
            for field, lower, upper in (("wave", 1, 5), ("order", 0, 9999)):
                if type(doc.get(field)) is not int or not lower <= doc[field] <= upper:
                    raise InputError(f"invalid declaration {field}")
            _str_field(doc, "family", name, required=True)
            _enum_field(doc, "entitlement", name, ("run_read", "run_write", "solve"), required=True)
            interaction = _object_field(doc, "interaction", name)
            _enum_field(interaction, "mode", name, ("form", "none", "pick"), required=True)
            declarations.append({"name": name, "ledger": ledger, "wave": doc["wave"]})
    except OSError as exc:
        raise InputError("Solar declarations unreadable") from exc
    if bindings["rooftop"]:
        rooftop_tools = {doc["name"] for doc in declarations
                         if doc["wave"] == 1 or doc["name"] == "solar-string-conductors"}
        if len(rooftop_tools) > bindings["max_flow_steps"]:
            raise InputError("rooftop steps exceed the browser flow limit")
    return {"flags": flags, "bindings": bindings, "declarations": declarations}


def capability_reachability(scoped, sources):
    mapped = {}
    bindings = sources["bindings"]
    for declaration in sources["declarations"]:
        name = declaration["name"]
        if name in bindings["tools"] or (bindings["rooftop"] and
                (declaration["wave"] == 1 or name == "solar-string-conductors")):
            for capability in declaration["ledger"]:
                mapped.setdefault(capability, set()).add(name)
    disabled = sorted(flag for flag in UI_FLAGS if not sources["flags"][flag])
    return [{"capability": capability,
             "state": ("unreachable" if disabled else "reachable") if mapped.get(capability) else "unverified",
             "tools": sorted(mapped.get(capability, [])),
             "blocked_flags": disabled.copy() if mapped.get(capability) else []}
            for capability in sorted(scoped)]


def evaluate(expected, rows, receipts_dir, require, root, *, ui_sources=None):
    max_wave = scope_wave(require)
    findings = ledger_findings(expected, rows)

    # capability -> the set of versions the in-scope duty rows expect
    scoped = {}
    for row in rows:
        if in_scope(row, max_wave):
            scoped.setdefault(row["capability"], set()).add(row["capability_version"])

    files_by_capability = receipt_index(receipts_dir) if scoped else {}

    failing_capabilities = set()
    diverged = []
    for capability in sorted(scoped):
        found, divergence = capability_findings(
            capability, scoped[capability], files_by_capability.get(capability, []), root
        )
        if found:
            failing_capabilities.add(capability)
            findings.extend(found)
        elif divergence is not None:
            diverged.append(divergence)

    reachability = capability_reachability(
        scoped, read_ui_sources(repo_root()) if ui_sources is None else ui_sources
    )
    for item in reachability:
        if item["state"] == "unreachable":
            findings.append(finding("UI_UNREACHABLE", "disabled guided-flow flags: " +
                                    ", ".join(item["blocked_flags"]), capability=item["capability"]))
    findings.sort(key=lambda item: (item["code"], finding_name(item)))

    duty_rows = [row for row in rows if has_duty(row)]
    scoped_rows = [row for row in duty_rows if in_scope(row, max_wave)]
    diverged_capabilities = {item["capability"] for item in diverged}
    counts = {
        "rows": len(rows),
        "registrations_expected": expected,
        "by_class": {cls: sum(1 for row in rows if row["class"] == cls) for cls in CLASSES},
        "by_wave": {
            **{str(wave): sum(1 for row in rows if row["wave"] == wave) for wave in WAVES},
            "null": sum(1 for row in rows if row["wave"] is None),
        },
        "duty_rows_total": len(duty_rows),
        "duty_rows_in_scope": len(scoped_rows),
        "duty_rows_passing": sum(1 for row in scoped_rows if row["capability"] not in failing_capabilities),
        # Diverged rows and capabilities are PASSING as well: they have met the spec's
        # bar. They are counted apart so a reader always sees what was not reproduced.
        "duty_rows_diverged": sum(1 for row in scoped_rows if row["capability"] in diverged_capabilities),
        "capabilities_in_scope": len(scoped),
        "capabilities_passing": len(scoped) - len(failing_capabilities),
        "capabilities_diverged": len(diverged_capabilities),
        **{"capabilities_" + state: sum(item["state"] == state for item in reachability)
           for state in ("reachable", "unreachable", "unverified")},
    }
    return {
        "ok": not findings,
        "require": require if require is not None else "all-production",
        "counts": counts,
        "divergences": sorted(diverged, key=lambda item: item["capability"]),
        "findings": findings,
        "reachability": reachability,
    }


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def human_report(result, ledger_path, receipts_dir):
    counts = result["counts"]
    header = [
        "solar parity status",
        f"ledger:   {ledger_path}",
        f"receipts: {receipts_dir}",
        f"require:  {result['require']}",
        "",
        f"rows: {counts['rows']} of {counts['registrations_expected']} expected",
        "by class: " + "  ".join(f"{cls}={counts['by_class'][cls]}" for cls in CLASSES),
        "by wave:  "
        + "  ".join(f"{wave}={counts['by_wave'][str(wave)]}" for wave in WAVES)
        + f"  null={counts['by_wave']['null']}",
        f"receipt parity duty rows: {counts['duty_rows_total']} total, "
        f"{counts['duty_rows_in_scope']} in scope, {counts['duty_rows_passing']} passing",
        f"receipt parity capabilities in scope: {counts['capabilities_in_scope']}, "
        f"{counts['capabilities_passing']} passing",
    ]
    divergences = result.get("divergences") or []
    if divergences:
        header.append("")
        header.append(
            f"declared divergences: {counts['capabilities_diverged']} capabilities, "
            f"{counts['duty_rows_diverged']} duty rows (counted as passing, never reproduced)"
        )
        shown = divergences[:MAX_DIVERGENCE_LINES]
        for item in shown:
            header.append(f"  {item['capability']}  {item['finding']}: {item['summary']}")
        if len(divergences) > len(shown):
            header.append(f"  ... {len(divergences) - len(shown)} more")
    reachability = result.get("reachability", [])
    header.extend([
        "", "Guided-flow reachability (source configuration)",
        "  ".join(f"{state}: {counts.get('capabilities_' + state, 0)}"
                  for state in ("reachable", "unreachable", "unverified")),
        "Source configuration does not prove a deployed image or runtime readiness.",
        "Capability  Receipt  Reachability",
    ])
    failed = {item.get("capability") for item in result["findings"] if item["code"] != "UI_UNREACHABLE"}
    diverged = {item["capability"] for item in divergences}
    for item in reachability[:80]:
        capability = item["capability"]
        receipt = "fail" if capability in failed else "declared divergence" if capability in diverged else "pass"
        header.append(f"{capability}  {receipt}  {item['state']}")
    if len(reachability) > 80:
        header.append(f"  ... {len(reachability) - 80} capability rows omitted")
    header.extend(["", f"findings: {len(result['findings'])}"])
    footer = ["", "verdict: " + ("PASS" if result["ok"] else "FAIL")]
    room = MAX_REPORT_LINES - len(header) - len(footer)
    lines = list(header)
    findings = result["findings"]
    if len(findings) > room:
        shown, hidden = findings[: max(room - 1, 0)], len(findings) - max(room - 1, 0)
    else:
        shown, hidden = findings, 0
    for item in shown:
        name = finding_name(item)
        lines.append(f"  {item['code']}  {name}: {item['detail']}" if name else f"  {item['code']}: {item['detail']}")
    if hidden:
        lines.append(f"  ... {hidden} more")
    lines.extend(footer)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def repo_root():
    return Path(__file__).resolve().parent.parent


def build_parser():
    parser = argparse.ArgumentParser(
        prog="solar_parity_status.py",
        description="Report Branch2025 to Studio parity against the ledger and the receipt tree.",
    )
    parser.add_argument("--ledger", type=Path, default=None, help="path to the parity ledger JSON")
    parser.add_argument("--receipts", type=Path, default=None, help="path to the receipt tree root")
    parser.add_argument("--require", choices=REQUIRE_CHOICES, default=None, help="parity scope to require")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help=f"repo the declared divergence findings are read from, under {DIVERGENCE_DIR}/ "
        "(default: this checkout)",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON object and nothing else")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    root = repo_root()
    ledger_path = args.ledger if args.ledger is not None else root / "docs" / "parity" / "solar-ledger.json"
    receipts_dir = args.receipts if args.receipts is not None else root / "docs" / "parity" / "receipts"
    finding_root = args.repo_root if args.repo_root is not None else root

    try:
        expected, rows = parse_ledger(ledger_path)
        result = evaluate(expected, rows, receipts_dir, args.require, finding_root)
    except InputError as exc:
        print(f"solar-parity-status: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # fail closed: never a traceback, never a verdict
        print(f"solar-parity-status: unexpected failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(human_report(result, ledger_path, receipts_dir))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
