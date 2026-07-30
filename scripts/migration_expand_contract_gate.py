#!/usr/bin/env python3
"""Expand-contract migration gate (W14 admin self-edit lane, L6).

Blocks CONTRACT-phase schema changes from landing in one unmarked step. The
platform's migrations are applied forward-only and the ECS deploy overlaps old
and new tasks (staging canary -> prod, rollback = previous task-definition
revision), so a migration that drops/renames/retypes something the PREVIOUS
image still reads breaks the overlap window and forecloses the rollback. The
safe shape is expand -> migrate readers -> contract, and the contract step must
SAY which expand step it completes.

RULES (per ``NNNN_name.sql`` in ``platform/migrations``):

  * Additive statements (CREATE TABLE/INDEX, ADD COLUMN, GRANT, INSERT, ...)
    pass with no ceremony.
  * A CONTRACT-phase statement — DROP TABLE / DROP COLUMN / DROP INDEX /
    DROP VIEW / DROP CONSTRAINT / RENAME / ALTER COLUMN ... TYPE /
    SET NOT NULL / TRUNCATE / DELETE FROM — requires the file to carry an
    explicit marker naming the earlier expand migration it completes:

        -- expand-contract: contract-of=0017

    The referenced number must be an EARLIER migration that exists. The marker
    is the reviewer's hook: it turns "this drop looks scary" into "this drop
    completes 0017's expand, whose readers migrated in between".
  * Applied migrations are GRANDFATHERED by number (the ledger stores each
    file's sha256, so retro-editing an applied file to add a marker would
    break ``assert_schema_current``). The grandfather set is frozen below —
    never grow it; new work gets markers instead.

Comments (``--`` and ``/* */``) and string literals are stripped before
matching, so prose about dropping a column never trips the gate.

CLI:  python migration_expand_contract_gate.py [--migrations-dir PATH]
Exit 0 = every migration passes; exit 1 = violations (each printed).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_MIGRATIONS_DIR = REPO / "platform" / "migrations"

FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
MARKER_RE = re.compile(r"^\s*--\s*expand-contract:\s*contract-of=(\d{4})\s*$",
                       re.MULTILINE)

# Migrations applied before this gate existed (head 0022 on 2026-07-30). The
# ledger pins each file's sha256, so these can never be edited to carry
# markers. FROZEN: new numbers are never added here.
GRANDFATHERED = frozenset(range(1, 23))

# CONTRACT-phase statement detectors, applied to comment-and-string-stripped
# SQL. Each is (label, compiled regex).
CONTRACT_PATTERNS = [
    ("DROP TABLE", re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE)),
    ("DROP COLUMN", re.compile(r"\bDROP\s+COLUMN\b", re.IGNORECASE)),
    ("DROP INDEX", re.compile(r"\bDROP\s+INDEX\b", re.IGNORECASE)),
    ("DROP VIEW", re.compile(r"\bDROP\s+VIEW\b", re.IGNORECASE)),
    ("DROP CONSTRAINT", re.compile(r"\bDROP\s+CONSTRAINT\b", re.IGNORECASE)),
    ("RENAME", re.compile(r"\bRENAME\s+(?:TO|COLUMN)\b", re.IGNORECASE)),
    ("ALTER COLUMN TYPE", re.compile(
        r"\bALTER\s+COLUMN\s+\S+\s+(?:SET\s+DATA\s+)?TYPE\b", re.IGNORECASE)),
    ("SET NOT NULL", re.compile(r"\bSET\s+NOT\s+NULL\b", re.IGNORECASE)),
    ("TRUNCATE", re.compile(r"\bTRUNCATE\b", re.IGNORECASE)),
    ("DELETE FROM", re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE)),
]


def strip_sql_noise(text: str) -> str:
    """Remove -- line comments, /* */ block comments, and quoted literals.

    Single pass so a quote inside a comment (or a ``--`` inside a string)
    cannot confuse the result. Dollar-quoted bodies ($$...$$) are stripped
    too — a function body is not schema DDL this gate should read.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j == -1 else j  # keep the newline
        elif ch == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
        elif ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'" and not text.startswith("''", j):
                    break
                j += 2 if text.startswith("''", j) else 1
            i = min(j + 1, n)
            out.append("''")
        elif ch == '"':
            j = text.find('"', i + 1)
            i = n if j == -1 else j + 1
            out.append('""')
        elif ch == "$" and text.startswith("$$", i):
            j = text.find("$$", i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def check_migrations(migrations_dir: Path) -> list[str]:
    """Return violation strings (empty = pass)."""
    violations: list[str] = []
    if not migrations_dir.is_dir():
        return [f"migrations directory missing: {migrations_dir}"]

    numbered: dict[int, str] = {}
    files = sorted(p for p in migrations_dir.iterdir() if p.suffix == ".sql")
    for path in files:
        m = FILENAME_RE.match(path.name)
        if not m:
            violations.append(
                f"{path.name}: filename must match NNNN_lowercase_name.sql")
            continue
        number = int(m.group(1))
        if number in numbered:
            violations.append(
                f"{path.name}: duplicate migration number {number:04d} "
                f"(also {numbered[number]})")
            continue
        numbered[number] = path.name

    for number in sorted(numbered):
        name = numbered[number]
        text = (migrations_dir / name).read_text(encoding="utf-8")
        stripped = strip_sql_noise(text)
        hits = [label for label, pattern in CONTRACT_PATTERNS
                if pattern.search(stripped)]
        if number in GRANDFATHERED:
            continue  # applied before the gate; ledger-pinned bytes
        if not hits:
            continue
        markers = MARKER_RE.findall(text)
        if not markers:
            violations.append(
                f"{name}: contract-phase statement(s) [{', '.join(hits)}] "
                f"without an `-- expand-contract: contract-of=NNNN` marker. "
                f"Ship the expand step first, migrate readers, then land this "
                f"contract step naming that expand migration.")
            continue
        for marker in markers:
            ref = int(marker)
            if ref >= number:
                violations.append(
                    f"{name}: contract-of={marker} must reference an EARLIER "
                    f"migration (this file is {number:04d})")
            elif ref not in numbered:
                violations.append(
                    f"{name}: contract-of={marker} references a migration "
                    f"that does not exist")
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--migrations-dir", type=Path,
                        default=DEFAULT_MIGRATIONS_DIR)
    args = parser.parse_args(argv)
    violations = check_migrations(args.migrations_dir)
    if violations:
        print(f"MIGRATION GATE: FAIL ({len(violations)} violation(s))")
        for violation in violations:
            print(f"  - {violation}")
        return 1
    print(f"MIGRATION GATE: PASS ({args.migrations_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
