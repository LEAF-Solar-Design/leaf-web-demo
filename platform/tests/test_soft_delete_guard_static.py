"""Structural guard: no query reads a soft-deleted project by accident.

WHY THIS IS A STATIC TEST AND NOT A CODE REVIEW RULE. `projects` is
soft-deleted, so a dead project's row is still physically there, and every table
keyed by project_id (project_authority_modes, drawing_artifacts, jobs, ...)
outlives it -- their ON DELETE CASCADE foreign keys never fire, because a soft
delete never DELETEs. Any query that joins those tables without a liveness
predicate therefore selects dead projects and looks completely correct while
doing it. On 2026-08-28 exactly that shape picked a project soft-deleted two
days earlier and stalled a release lane ~40 minutes; a read-only sweep of
staging the same day found 20+ orphan authority rows across two orgs.

Migration 0050 answers with two views -- `live_projects` and
`live_project_authority_modes` -- that apply the predicate once. This test is
what makes them BINDING: a new query that reads the base tables without the
guard fails CI here, so the unguarded join is unreachable rather than merely
discouraged.

THE PREDICATE is `deleted_at IS NULL AND status <> 'deleted'`, the union of both
soft-delete writers: store.soft_delete_project sets deleted_at and leaves status
'active'; project_lifecycle.delete_project sets status = 'deleted'. Checking one
column catches half the deleted rows, which is how `p.status = 'active'`
(annotation_store) and `status <> 'deleted'` (project_lifecycle._project_row)
were both holes at once.

WHAT THIS CANNOT SEE, stated plainly so nobody reads a green run as more than
it is. (a) It only inspects statements that NAME `projects` or
`project_authority_modes`. 39 base tables are keyed by project_id and only
three (jobs, drawing_versions, built_tools) carry their own deleted_at; a query
that reads one of those alone -- `SELECT ... FROM evidence_bundles WHERE
project_id = %(p)s` -- is invisible here, and stays correct only because its
module gates the project first. (b) It scans `platform/` and `server/` Python.
The 2026-08-28 query lived in an ad-hoc ops script outside both, which no static
check in this repo can reach; docs/POSTGRES-CUTOVER.md carries that warning for
script authors instead.
"""
import ast
import pathlib
import re

import pytest

_PKG = pathlib.Path(__file__).resolve().parent.parent
_REPO = _PKG.parent
_SCANNED_ROOTS = (_PKG, _REPO / "server")

# `live_` prefixed names are the guarded views; the lookbehind keeps them from
# matching their own base table.
_PAM = re.compile(r"(?<!live_)project_authority_modes", re.IGNORECASE)
_PROJECTS = re.compile(r"(?<!live_)\bprojects\b", re.IGNORECASE)
_GUARDED = re.compile(r"deleted_at\s+IS\s+NULL", re.IGNORECASE)

# A write whose TARGET is the table itself is exempt: `projects` and
# project_authority_modes are where deletion and restoration are IMPLEMENTED, so
# they must be able to address dead rows. Deliberately anchored on the target
# table -- a statement that writes some OTHER table while READING projects (an
# INSERT ... SELECT FROM projects, the exact shape of the drawing-artifact
# bootstrap) is NOT exempt and still needs the guard.
def _writes_to(table: str) -> re.Pattern:
    return re.compile(
        rf"^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+{table}\b", re.IGNORECASE)


# Statements that read dead projects ON PURPOSE. Each entry is
# (module, function, distinguishing fragment) -> the reason it is exempt.
# Closed-world: an entry whose call site disappears fails this test, so the list
# cannot quietly rot into a blanket exemption.
_INTENTIONAL = {
    ("platform/campaign_capabilities.py", "_load",
     "SELECT * FROM projects WHERE org_id=%(org)s AND project_id=%(project)s FOR SHARE"):
        "the locked read observes the row so the live flag rejects deleted or "
        "inactive projects for normal work; recovery inspects accepted operations "
        "with live=False",
    ("platform/campaign_capabilities.py", "_host_scope",
     "SELECT status, deleted_at FROM projects WHERE org_id=%s AND project_id=%s"):
        "computes live from both project deletion fields, enrollment and job state; "
        "non-live rejects new work and permits only final readback settlement after "
        "apply+activate, as settle_host_operation requires",
    ("platform/db.py", "reconciliation_snapshot", "FROM project_authority_modes"):
        "the RAW table census the backfill comparison reads; the guarded count "
        "sits beside it as project_live, and their difference IS the orphan "
        "population this incident was about",
    ("platform/store.py", "_project_absence", "SELECT status, deleted_at FROM projects"):
        "its entire job is to look at the dead row and report deleted_at, so "
        "that a soft delete stops reading as a generic not-found",
    ("platform/store.py", "append_history_operation", "SELECT 1 FROM projects"):
        "a FOR UPDATE serialization lock, not an existence gate: the function "
        "already refused a dead project at _require_postgres_authority, and "
        "narrowing the lock to live rows would weaken it to lock-or-skip",
}


def _statements():
    """Yield (module, function, single-line SQL) for every cursor .execute()."""
    found = []
    for root in _SCANNED_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if "tests" in path.parts:
                continue
            module = path.relative_to(_REPO).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - not our source to fix
                continue
            scope = []

            class Visitor(ast.NodeVisitor):
                def visit_FunctionDef(self, node):
                    scope.append(node.name)
                    self.generic_visit(node)
                    scope.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_Call(self, node):
                    func = node.func
                    if isinstance(func, ast.Attribute) and func.attr == "execute":
                        sql = "".join(
                            literal.value for literal in ast.walk(node)
                            if isinstance(literal, ast.Constant)
                            and isinstance(literal.value, str)
                        )
                        if sql.strip():
                            found.append((module, scope[-1] if scope else "<module>",
                                          " ".join(sql.split())))
                    self.generic_visit(node)

            Visitor().visit(tree)
    assert found, "expected to find SQL statements to scan"
    return found


def unguarded(statements):
    """Return every statement that reads a base table without the guard.

    Exposed as a function so the self-check below can prove the checker still
    catches the incident's own query, rather than trusting that an empty
    offender list means the code is clean.
    """
    offenders = []
    for module, function, sql in statements:
        for pattern, table in ((_PAM, "project_authority_modes"), (_PROJECTS, "projects")):
            if not pattern.search(sql):
                continue
            if _writes_to(table).match(sql) or _GUARDED.search(sql):
                continue
            key = next(
                (k for k in _INTENTIONAL
                 if k[0] == module and k[1] == function and k[2] in sql),
                None,
            )
            if key is not None:
                continue
            offenders.append((module, function, table, sql[:160]))
    return offenders


def test_no_query_reads_a_soft_deleted_project_without_the_guard():
    offenders = unguarded(_statements())
    assert not offenders, (
        "these statements read a project-keyed table without the live-project "
        "guard, so they can select soft-deleted projects. Join live_projects / "
        "live_project_authority_modes (migration 0050), or add "
        "`AND <alias>.deleted_at IS NULL`. If the read is deliberate, add it to "
        f"_INTENTIONAL with a reason:\n" + "\n".join(map(str, offenders))
    )


def test_the_checker_still_catches_the_incidents_own_query():
    """A guard that cannot fail is not a guard."""
    incident = (
        "SELECT p.project_id FROM projects p JOIN project_authority_modes pam "
        "ON pam.org_id = p.org_id AND pam.project_id = p.project_id "
        "WHERE p.org_id = %(org)s AND pam.authority_mode = 'postgres_canonical' LIMIT 1"
    )
    caught = unguarded([("platform/fake.py", "mint_fixture", incident)])
    assert {row[2] for row in caught} == {"project_authority_modes", "projects"}

    repaired = (
        "SELECT pam.project_id FROM live_project_authority_modes pam "
        "WHERE pam.org_id = %(org)s AND pam.authority_mode = 'postgres_canonical' LIMIT 1"
    )
    assert unguarded([("platform/fake.py", "mint_fixture", repaired)]) == []


@pytest.mark.parametrize("key", sorted(_INTENTIONAL))
def test_every_intentional_exemption_still_has_a_call_site(key):
    """Closed world: a stale exemption is a silent hole, so it fails here."""
    module, function, fragment = key
    assert any(
        m == module and f == function and fragment in sql
        for m, f, sql in _statements()
    ), (
        f"exemption {key} no longer matches any statement. Delete it -- an "
        "exemption kept past its call site quietly widens into a blanket one."
    )


def test_migration_0050_names_the_predicate_once():
    raw = (_PKG / "migrations" / "0050_live_project_guard.sql").read_text(encoding="utf-8")
    # The rationale comment quotes the predicate too; count the DDL only.
    sql = "\n".join(line for line in raw.splitlines()
                    if not line.lstrip().startswith("--"))
    for required in (
        "CREATE OR REPLACE VIEW live_projects",
        "CREATE OR REPLACE VIEW live_project_authority_modes",
        "deleted_at IS NULL AND status <> 'deleted'",
        "idx_projects_live_identity",
    ):
        assert required in sql
    # Both views and the supporting index must carry the SAME predicate. Two
    # spellings that drift apart would be the original bug with extra steps.
    assert "p.deleted_at IS NULL AND p.status <> 'deleted'" in sql, (
        "live_project_authority_modes must apply the aliased form of the "
        "identical predicate")
    assert sql.count("deleted_at IS NULL AND status <> 'deleted'") == 2, (
        "expected exactly the live_projects view and idx_projects_live_identity "
        "to carry the unaliased predicate")
