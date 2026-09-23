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


def evaluate(expected, rows, receipts_dir, require, root):
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
    }
    return {
        "ok": not findings,
        "require": require if require is not None else "all-production",
        "counts": counts,
        "divergences": sorted(diverged, key=lambda item: item["capability"]),
        "findings": findings,
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
        f"duty rows: {counts['duty_rows_total']} total, "
        f"{counts['duty_rows_in_scope']} in scope, {counts['duty_rows_passing']} passing",
        f"capabilities in scope: {counts['capabilities_in_scope']}, "
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
