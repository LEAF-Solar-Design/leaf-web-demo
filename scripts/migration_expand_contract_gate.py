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
  * ONE drop is additive and needs no marker: widening a value-list CHECK.
    ``ALTER TABLE t DROP CONSTRAINT c;`` immediately followed by
    ``ALTER TABLE t ADD CONSTRAINT c CHECK (col IN (...));`` passes only when
    the earlier migrations define that same constraint on that same table as
    ``CHECK (col IN (...))`` over the same column and every old literal is
    still in the new list. Every value the previous image writes stays valid.
    Anything else about the pair (a removed literal, another column, another
    table or name, a statement between them, an unknown prior definition)
    leaves the DROP a contract-phase statement.
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
    # CATCH-ALL DROP, not an object-type list. Round-2 review showed a list
    # (TABLE, COLUMN, VIEW, ...) is a whack-a-mole: DROP TRIGGER, DROP
    # POLICY, DROP PROCEDURE and future object kinds slipped through. Every
    # `DROP <thing>` is contract-phase EXCEPT the two loosening ALTER COLUMN
    # forms (`DROP NOT NULL`, `DROP DEFAULT`), which are expand-safe.
    ("DROP", re.compile(
        r"\bDROP\s+(?!NOT\b|DEFAULT\b)\w", re.IGNORECASE)),
    # Any RENAME (TO / COLUMN / VALUE / CONSTRAINT / ...) breaks a reader of
    # the old name — same catch-all reasoning.
    ("RENAME", re.compile(r"\bRENAME\b", re.IGNORECASE)),
    # PostgreSQL allows omitting the COLUMN keyword: `ALTER TABLE t ALTER c
    # TYPE ...`. Match both spellings.
    ("ALTER COLUMN TYPE", re.compile(
        r"\bALTER\s+(?:COLUMN\s+)?\S+\s+(?:SET\s+DATA\s+)?TYPE\b",
        re.IGNORECASE)),
    ("SET NOT NULL", re.compile(r"\bSET\s+NOT\s+NULL\b", re.IGNORECASE)),
    ("TRUNCATE", re.compile(r"\bTRUNCATE\b", re.IGNORECASE)),
    ("DELETE FROM", re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE)),
    ("REVOKE", re.compile(r"\bREVOKE\b", re.IGNORECASE)),
]


def strip_sql_noise(text: str) -> str:
    """Remove -- line comments, /* */ block comments, and quoted literals.

    Single pass so a quote inside a comment (or a ``--`` inside a string)
    cannot confuse the result. Dollar-quoted bodies ($$...$$) are stripped
    too — a function body is not schema DDL this gate should read.
    """
    return _strip_sql_noise(text)[0]


def _strip_sql_noise(text: str) -> tuple[str, dict[int, str]]:
    """strip_sql_noise plus each string literal's value, keyed by the offset of
    its ``''`` placeholder in the stripped text."""
    out: list[str] = []
    literals: dict[int, str] = {}
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j == -1 else j  # keep the newline
        elif ch == "/" and text.startswith("/*", i):
            # PostgreSQL block comments NEST (sql-syntax-lexical): closing at
            # the first */ would leave the outer comment's tail — e.g.
            # `DROP/* a /* b */ NOT */TRIGGER` — as bogus tokens that break
            # the DROP...TRIGGER adjacency and evade the detectors. Track
            # depth so the WHOLE outer comment strips to one separator.
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            i = j
            # A block comment is a TOKEN SEPARATOR in SQL: `DROP/**/TRIGGER`
            # is valid DDL. Removing it without a replacement would glue the
            # tokens into DROPTRIGGER and evade every \b...\s+ pattern.
            out.append(" ")
        elif ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'" and not text.startswith("''", j):
                    break
                j += 2 if text.startswith("''", j) else 1
            literals[sum(map(len, out))] = text[i + 1:j].replace("''", "'")
            i = min(j + 1, n)
            out.append("''")
        elif ch == '"':
            j = text.find('"', i + 1)
            i = n if j == -1 else j + 1
            out.append('""')
        elif ch == "$" and text.startswith("$$", i):
            j = text.find("$$", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")  # same separator reasoning as block comments
        else:
            out.append(ch)
            i += 1
    return "".join(out), literals


_IDENT = r"[A-Za-z_][A-Za-z_0-9$]*"
_VALUE_LIST = r"\(\s*((?:''\s*,\s*)*'')\s*\)"
_VALUE_CHECK = rf"CHECK\s*\(\s*({_IDENT})\s+IN\s*{_VALUE_LIST}\s*\)"
_CREATE_TABLE = re.compile(
    rf"\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({_IDENT})\s*\(", re.IGNORECASE)
_INLINE_CHECK = re.compile(rf"\bCONSTRAINT\s+({_IDENT})\s+{_VALUE_CHECK}", re.IGNORECASE)
_DROP_CHECK = re.compile(
    rf"\s*ALTER\s+TABLE\s+({_IDENT})\s+(DROP)\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?({_IDENT})\s*;",
    re.IGNORECASE)
_ADD_CHECK = re.compile(
    rf"\s*ALTER\s+TABLE\s+({_IDENT})\s+ADD\s+CONSTRAINT\s+({_IDENT})\s+{_VALUE_CHECK}\s*;",
    re.IGNORECASE)


def _literal_values(literals: dict[int, str], start: int, placeholders: str):
    """The literal values behind a run of ``''`` placeholders, or None."""
    values = []
    for match in re.finditer("''", placeholders):
        value = literals.get(start + match.start())
        if value is None:
            return None
        values.append(value)
    return frozenset(values) if len(set(values)) == len(values) else None


def check_widening_drops(stripped: str, literals: dict[int, str],
                         definitions: dict[tuple[str, str], tuple[str, frozenset]],
                         tables: set[str]) -> set[int]:
    """Offsets of DROP keywords that only widen a known value-list CHECK.

    ``definitions`` maps (table, constraint) to (column, allowed literals) as the
    earlier statements left it, and is advanced by this file's statements.
    ``tables`` holds every table a CREATE TABLE has already named. It fails
    closed: a statement that names a tracked constraint in any shape this
    function does not fully recognize forgets that constraint; a second CREATE
    TABLE of a known table (a no-op under IF NOT EXISTS, so its text proves
    nothing), a DROP TABLE or any RENAME naming a tracked table forgets that
    table's constraints; and a file with a tagged dollar body or a SQL compound
    body forgets everything.
    """
    if re.search(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\bBEGIN\s+ATOMIC\b", stripped, re.IGNORECASE):
        definitions.clear()
        return set()
    allowed: set[int] = set()
    statements = list(re.finditer(r"[^;]*;", stripped))
    for index, statement in enumerate(statements):
        sql, base = statement.group(), statement.start()
        defined: dict[tuple[str, str], tuple[str, frozenset]] = {}
        table = _CREATE_TABLE.match(sql)
        if table and table[1].lower() in tables:
            for key in [key for key in definitions if key[0] == table[1].lower()]:
                del definitions[key]
        elif table:
            tables.add(table[1].lower())
            for check in _INLINE_CHECK.finditer(sql):
                values = _literal_values(literals, base + check.start(3), check[3])
                if values is not None:
                    defined[(table[1].lower(), check[1].lower())] = (check[2].lower(), values)
        add = _ADD_CHECK.fullmatch(sql)
        if add:
            values = _literal_values(literals, base + add.start(4), add[4])
            if values is not None:
                defined[(add[1].lower(), add[2].lower())] = (add[3].lower(), values)
        drop = _DROP_CHECK.fullmatch(sql)
        pair = None
        if drop and index + 1 < len(statements):
            following = statements[index + 1]
            readd = _ADD_CHECK.fullmatch(following.group())
            key = (drop[1].lower(), drop[3].lower())
            prior = definitions.get(key)
            if readd and prior and (readd[1].lower(), readd[2].lower()) == key:
                values = _literal_values(literals, following.start() + readd.start(4), readd[4])
                if values is not None and readd[3].lower() == prior[0] and prior[1] <= values:
                    pair = key
                    allowed.add(base + drop.start(2))
        # Forget any tracked constraint this statement names without redefining it,
        # except the DROP half of a recognized widening pair.
        for key in list(definitions):
            if key in defined or key == pair:
                continue
            reshapes_table = (re.search(r"\bDROP\s+TABLE\b|\bRENAME\b", sql, re.IGNORECASE)
                              and re.search(rf"\b{re.escape(key[0])}\b", sql, re.IGNORECASE))
            if reshapes_table or re.search(rf"\b{re.escape(key[1])}\b", sql, re.IGNORECASE):
                del definitions[key]
        definitions.update(defined)
    return allowed


def initial_table_revokes(stripped: str) -> set[int]:
    """Offsets of narrowly supported, same-migration privilege initialization.

    Only complete top-level statements count. Noise stripping removes ordinary
    function/DO bodies; unsupported tagged bodies and SQL compound bodies get
    no exception rather than guessing where their top-level statements begin.
    """
    if re.search(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\bBEGIN\s+ATOMIC\b",
                 stripped, re.IGNORECASE):
        return set()
    identifier = r"[A-Za-z_][A-Za-z_0-9$]*"
    target = rf"({identifier})\.({identifier})"
    create = re.compile(rf"\s*CREATE\s+TABLE\s+{target}\s*\(.*\)\s*;",
                        re.IGNORECASE | re.DOTALL)
    revoke = re.compile(
        rf"\s*REVOKE\s+ALL\s+(?:PRIVILEGES\s+)?ON\s+(?:TABLE\s+)?"
        rf"{target}\s+FROM\s+PUBLIC\s*;", re.IGNORECASE)
    created: set[tuple[str, str]] = set()
    allowed: set[int] = set()
    for statement in re.finditer(r"[^;]*;", stripped):
        sql = statement.group()
        match = create.fullmatch(sql)
        if match:
            created.add((match[1].lower(), match[2].lower()))
        match = revoke.fullmatch(sql)
        if match and (match[1].lower(), match[2].lower()) in created:
            allowed.add(statement.start() + len(sql) - len(sql.lstrip()))
    return allowed


def check_migrations(migrations_dir: Path) -> list[str]:
    """Return violation strings (empty = pass)."""
    violations: list[str] = []
    if not migrations_dir.is_dir():
        return [f"migrations directory missing: {migrations_dir}"]

    numbered: set[int] = set()
    migrations: list[tuple[int, Path]] = []
    files = sorted(p for p in migrations_dir.iterdir() if p.suffix == ".sql")
    for path in files:
        m = FILENAME_RE.match(path.name)
        if not m:
            violations.append(
                f"{path.name}: filename must match NNNN_lowercase_name.sql")
            continue
        number = int(m.group(1))
        # The producer ledger identifies migrations by full filename and SHA.
        # Numeric prefixes order references, but do not identify a SQL body.
        numbered.add(number)
        migrations.append((number, path))

    check_definitions: dict[tuple[str, str], tuple[str, frozenset]] = {}
    created_tables: set[str] = set()
    for number, path in migrations:
        name = path.name
        text = path.read_text(encoding="utf-8")
        stripped, literals = _strip_sql_noise(text)
        initial_revokes = initial_table_revokes(stripped)
        widening_drops = check_widening_drops(
            stripped, literals, check_definitions, created_tables)
        exempt = {"REVOKE": initial_revokes, "DROP": widening_drops}
        hits = [label for label, pattern in CONTRACT_PATTERNS
                if any(match.start() not in exempt.get(label, ())
                       for match in pattern.finditer(stripped))]
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
