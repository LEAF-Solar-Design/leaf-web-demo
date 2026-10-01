"""Structural guard: every store read is org-scoped (acceptance criterion #5).

Statically asserts that no read function in leaf_platform.store issues a query
without an org_id predicate, and that every read takes org_id as its first
parameter. Also confirms the stdlib-`platform` shadow fix: `import platform`
resolves the genuine stdlib module inside the test process.
"""
import ast
import inspect
import re

import leaf_platform.store as store

_READ_PREFIXES = ("list_", "get_", "find_", "count_", "hydrate_")

# Documented equivalents of the org_id predicate, one per named function. Every
# other function must still bind org_id itself.
_EQUIVALENT_ORG_PREDICATES = {
    # project_repository_authorities names its org column organization_id and is
    # read by the full authority tuple (tenant_id AND organization_id AND project_id).
    "resolve_project_repository_authority":
        r"where[\s\S]*?tenant_id\s*=[\s\S]*?organization_id\s*=[\s\S]*?project_id\s*=",
}


def _public_functions():
    for name, fn in inspect.getmembers(store, inspect.isfunction):
        if getattr(fn, "__module__", None) != store.__name__:
            continue
        if name.startswith("_"):
            continue
        yield name, fn


def test_stdlib_platform_not_shadowed():
    import platform  # the stdlib module, not our package

    assert hasattr(platform, "system") and hasattr(platform, "python_implementation")
    assert "leaf-web-demo" not in (getattr(platform, "__file__", "") or "").replace("\\", "/")


def test_read_functions_take_org_id_first():
    reads = [(n, f) for n, f in _public_functions() if n.startswith(_READ_PREFIXES)]
    assert reads, "expected read functions in store.py"
    for name, fn in reads:
        params = list(inspect.signature(fn).parameters)
        assert params and params[0] == "org_id", (
            f"read function {name}() must take org_id as its first parameter, got {params}"
        )


def test_every_select_binds_org_id_predicate():
    """Any function that issues a SELECT must scope it with a WHERE ... org_id clause."""
    offenders = []
    for name, fn in _public_functions():
        src = inspect.getsource(fn)
        tree = ast.parse(src)
        execute_calls = (
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
        )
        sql_literals = "\n".join(
            literal.value
            for call in execute_calls
            for literal in ast.walk(call)
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str)
        )
        if not re.search(r"\bSELECT\b", sql_literals, re.IGNORECASE):
            continue
        # a WHERE clause that mentions org_id must appear in the function body
        predicate = _EQUIVALENT_ORG_PREDICATES.get(name, r"where[\s\S]*?org_id")
        if not re.search(predicate, sql_literals, re.IGNORECASE):
            offenders.append(name)
    assert not offenders, f"store read functions missing an org_id predicate: {offenders}"
