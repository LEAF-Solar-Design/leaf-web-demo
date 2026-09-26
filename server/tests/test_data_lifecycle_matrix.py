"""Source-only checks for the counsel packet's current-behaviour matrix."""

import ast
from collections import Counter
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "docs/DATA-LIFECYCLE-MATRIX.md"
HEADER = (
    "| Store | What it holds | Backing tables or objects | Export path today | "
    "Deletion path today | Retention implemented today | Retention period |"
)
REQUIRED_STORES = {
    "drawings", "uploads", "tenant_tool_repo", "claude_grant", "jobs_sessions",
    "stripe", "auth0_app_metadata", "bigquery_telemetry", "broker_ledger",
    "org_identity",
}


def _table_lines():
    return [
        line for line in MATRIX.read_text(encoding="utf-8").splitlines()
        if line.startswith("|")
    ]


def _rows():
    rows = []
    for line in _table_lines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line == HEADER or all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
            continue
        assert len(cells) == 7, f"Expected seven cells: {line}"
        rows.append(cells)
    assert rows, "Matrix has no data rows"
    return rows


def _store_id(row):
    match = re.match(r"`([a-z0-9_]+)`(?:\s|$)", row[0])
    assert match, f"Store cell must start with a code-span id: {row[0]}"
    return match.group(1)


def test_matrix_header_is_exact():
    lines = _table_lines()
    assert lines and lines[0] == HEADER
    assert lines.count(HEADER) == 1
    separators = [
        line for line in lines
        if all(re.fullmatch(r":?-{3,}:?", c.strip())
               for c in line.strip().strip("|").split("|"))
    ]
    assert len(separators) == 1, "Expected exactly one markdown table"
    assert lines[1] == separators[0]
    assert len(separators[0].strip("|").split("|")) == 7
    _rows()


def test_matrix_has_one_row_per_required_store():
    counts = Counter(_store_id(row) for row in _rows())
    assert REQUIRED_STORES <= counts.keys(), REQUIRED_STORES - counts.keys()
    assert all(count == 1 for count in counts.values()), counts


def _collect_offboard_tables(source):
    """Read static SQL and reject unreadable SQL-running method arguments."""
    tree = ast.parse(source)
    parents = {
        child: parent for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    scope_types = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                   ast.ClassDef, ast.Lambda)

    def scope_of(node):
        parent = parents.get(node)
        while parent is not None and not isinstance(parent, scope_types):
            parent = parents.get(parent)
        return parent

    def constant_string(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return constant_string(node.left) + constant_string(node.right)
        if isinstance(node, ast.JoinedStr):
            return "".join(constant_string(part) for part in node.values)
        if isinstance(node, ast.FormattedValue) and isinstance(node.value, ast.Constant):
            value = node.value.value
            if node.conversion != -1:
                value = {115: str, 114: repr, 97: ascii}[node.conversion](value)
            spec = constant_string(node.format_spec) if node.format_spec else ""
            return format(value, spec)
        raise ValueError("not a constant string")

    def assigned_strings(scope, name):
        values = []
        for part in ast.walk(scope):
            if scope_of(part) is not scope:
                continue
            if isinstance(part, ast.arg) and part.arg == name:
                raise ValueError(f"{name} is a parameter")
            if not (isinstance(part, ast.Name) and part.id == name
                    and isinstance(part.ctx, (ast.Store, ast.Del))):
                continue
            assignment = parents[part]
            if isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                values.append(constant_string(assignment.value))
            else:
                raise ValueError(f"unsupported assignment to {name}")
        return values

    tables = set()
    table_expression = (
        r"\b(?:DELETE\s+FROM|FROM|UPDATE|INSERT\s+INTO|TRUNCATE(?:\s+TABLE)?)"
        r"\s+([a-z_][a-z0-9_]*)\b"
    )
    table_pattern = re.compile(table_expression, flags=re.IGNORECASE)
    backstop_table_pattern = re.compile(table_expression)
    statement_start = re.compile(
        r"^[\s(]*(?:SELECT|WITH|DELETE|UPDATE|INSERT|TRUNCATE)\s"
    )
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(first.value)
    sql_methods = {"copy", "copy_expert", "copy_from", "copy_to"}
    # Inspect SQL arguments as text, never import the platform package. Every
    # known SQL-running method uses the same static-readability requirement.
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and (node.func.attr.startswith("execute")
                     or node.func.attr in sql_methods)):
            continue
        try:
            if not node.args:
                raise ValueError("missing positional SQL argument")
            argument = node.args[0]
            if isinstance(argument, ast.Name):
                scope = scope_of(node)
                values = assigned_strings(scope, argument.id)
                if not values and scope is not tree:
                    values = assigned_strings(tree, argument.id)
                if not values or len(set(values)) != 1:
                    raise ValueError(f"unresolved or ambiguous name {argument.id}")
                sql = values[0]
            else:
                sql = constant_string(argument)
        except (ValueError, TypeError, KeyError) as exc:
            raise AssertionError(
                f"Unreadable SQL argument at line {node.lineno}: {exc}"
            ) from exc
        tables.update(table_pattern.findall(sql))

    # Catch SQL passed through helpers or other methods too. Walking all string
    # constants includes the literal parts of joined and formatted strings.
    # Docstrings are prose; this fallback requires uppercase SQL keywords.
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node not in docstrings
                and statement_start.match(node.value)):
            tables.update(backstop_table_pattern.findall(node.value))
    return tables


def test_collector_reads_a_variable_held_query():
    source = (
        "def purge(cur, org_id):\n"
        '    purge_sql = "DELETE FROM customer_notes WHERE org_id = %(org_id)s"\n'
        '    cur.execute(purge_sql, {"org_id": org_id})\n'
    )
    assert _collect_offboard_tables(source) == {"customer_notes"}
    for assignment in (
        'purge_sql = "DELETE FROM " + "customer_notes"',
        'purge_sql: str = ("DELETE FROM " "customer_notes")',
        'purge_sql = f"DELETE FROM {\'customer_notes\'}"',
    ):
        for source in (
            assignment + "\ndef purge(cur):\n    cur.execute(purge_sql)\n",
            "def purge(cur):\n    " + assignment + "\n    cur.execute(purge_sql)\n",
        ):
            assert _collect_offboard_tables(source) == {"customer_notes"}


def test_collector_refuses_an_unreadable_query_argument():
    for argument, setup in (
        ("build_sql(org_id)", ""),
        ("queries.purge", ""),
        ('queries["purge"]', ""),
        ("purge_sql", ""),
        ("purge_sql", "    purge_sql = 42\n"),
        ("purge_sql", "    purge_sql = build_sql(org_id)\n"),
        ("purge_sql", '    purge_sql = "DELETE FROM customer_notes"\n'
                      '    purge_sql = "DELETE FROM other_notes"\n'),
        ('f"DELETE FROM {table}"', ""),
    ):
        source = "def purge(cur, org_id):\n" + setup + f"    cur.execute({argument})\n"
        line = len(source.splitlines())
        try:
            _collect_offboard_tables(source)
        except AssertionError as exc:
            assert f"line {line}:" in str(exc), str(exc)
        else:
            raise AssertionError(f"Collector accepted unreadable SQL: {argument}")


def test_collector_reads_executemany():
    source = (
        "def purge_customer_notes(cur, org_id):\n"
        "    cur.executemany(\n"
        '        "DELETE FROM customer_notes WHERE org_id = %s",\n'
        "        [(org_id,)],\n"
        "    )\n"
    )
    assert _collect_offboard_tables(source) == {"customer_notes"}


def test_collector_counts_sql_passed_to_any_other_method():
    for call in (
        'cur.copy_expert("DELETE FROM customer_notes WHERE org_id = %s", rows)',
        'run_sql(cur, "DELETE FROM customer_notes WHERE org_id = %s")',
        'cur.execute("delete from customer_notes")',
        'run_sql(cur, "  ((SELECT * FROM customer_notes)")',
        'run_sql(cur, "INSERT INTO customer_notes VALUES (1)")',
        'run_sql(cur, "TRUNCATE TABLE customer_notes")',
        'run_sql(cur, "TRUNCATE customer_notes")',
    ):
        source = "def purge(cur, rows):\n    " + call + "\n"
        assert _collect_offboard_tables(source) == {"customer_notes"}


def test_collector_ignores_prose_strings():
    source = (
        '"""Remove rows from the store for the org"""\n'
        '"Values from settings"\n'
    )
    assert _collect_offboard_tables(source) == set()
    source = (
        '"""Delete the org rows from the DB"""\n'
        'def configure():\n'
        '    """Update settings from the store"""\n'
    )
    assert _collect_offboard_tables(source) == set()
    source = (
        '"""DELETE FROM module_docs"""\n'
        'class Settings:\n'
        '    """SELECT * FROM class_docs"""\n'
        '    def configure(self):\n'
        '        """UPDATE function_docs SET value = 1"""\n'
        '    async def refresh(self):\n'
        '        """TRUNCATE TABLE async_docs"""\n'
        'message = "Delete the org rows from the DB"\n'
        'message = "delete from the store"\n'
        'message = "Update settings from the store"\n'
        'message = "SELECT(value) FROM settings"\n'
        'message = "SELECT value from settings"\n'
    )
    assert _collect_offboard_tables(source) == set()


def test_matrix_covers_every_table_offboard_touches():
    source = (ROOT / "platform/offboard.py").read_text(encoding="utf-8")
    tables = _collect_offboard_tables(source)
    assert tables, "No offboarding SQL tables found"
    documented = {
        span for row in _rows() for span in re.findall(r"`([^`]+)`", row[2])
    }
    assert tables <= documented, f"Undocumented offboarding tables: {tables - documented}"


def test_matrix_names_the_telemetry_dataset():
    telemetry = (ROOT / "docs/PLATFORM_TELEMETRY.md").read_text(encoding="utf-8")
    match = re.search(r"Dataset `([a-z0-9_]+)`", telemetry)
    assert match, "Telemetry contract does not name a dataset"
    row = next(row for row in _rows() if _store_id(row) == "bigquery_telemetry")
    assert match.group(1) in re.findall(r"`([^`]+)`", row[2])
    assert "425-day dataset default table expiration" in row[5]


def test_retention_period_is_left_to_operator_and_counsel():
    for row in _rows():
        assert row[6] == "operator/counsel decision", _store_id(row)
        assert row[5], f"Missing implemented retention: {_store_id(row)}"
    opening = MATRIX.read_text(encoding="utf-8").split("\n\n", 1)[0]
    assert "CURRENT behaviour only" in opening
    assert "Retention periods and erasure rights" in opening
    assert "operator's and counsel's decision (P-002, P-004)" in opening


def test_export_and_deletion_references_resolve():
    for row in _rows():
        store_id = _store_id(row)
        if store_id in {"org_identity", "drawings"}:
            assert "`platform/offboard.py::offboard_org`" in row[4]
        for cell in row[3:5]:
            if cell == "none today":
                continue
            references = [
                span for span in re.findall(r"`([^`]+)`", cell) if "::" in span
            ]
            assert references, f"Missing source reference for {store_id}: {cell}"
            for reference in references:
                match = re.fullmatch(r"([^:]+)::([A-Za-z_]\w*)", reference)
                assert match, f"Malformed reference: {reference}"
                path, symbol = match.groups()
                relative = Path(path)
                assert not relative.is_absolute() and ".." not in relative.parts
                target = (ROOT / relative).resolve()
                assert ROOT in target.parents, f"Reference escapes repository: {reference}"
                assert target.is_file(), f"Missing source file: {reference}"
                source = target.read_text(encoding="utf-8")
                if target.suffix == ".py":
                    definition = (
                        rf"^(?:(?:async[ \t]+)?def[ \t]+{re.escape(symbol)}[ \t]*\("
                        rf"|class[ \t]+{re.escape(symbol)}\b"
                        rf"|{re.escape(symbol)}[ \t]*=)"
                    )
                    assert re.search(definition, source, re.MULTILINE), reference
                else:
                    assert re.search(rf"\b{re.escape(symbol)}\b", source), reference
