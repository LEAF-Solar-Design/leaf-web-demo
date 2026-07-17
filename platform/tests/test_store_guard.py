"""Structural guard: every store read is org-scoped (acceptance criterion #5).

Statically asserts that no read function in leaf_platform.store issues a query
without an org_id predicate, and that every read takes org_id as its first
parameter. Also confirms the stdlib-`platform` shadow fix: `import platform`
resolves the genuine stdlib module inside the test process.
"""
import inspect
import re

import leaf_platform.store as store

_READ_PREFIXES = ("list_", "get_", "find_", "count_", "hydrate_")


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
        if "SELECT" not in src.upper():
            continue
        # a WHERE clause that mentions org_id must appear in the function body
        if not re.search(r"where[\s\S]*?org_id", src, re.IGNORECASE):
            offenders.append(name)
    assert not offenders, f"store read functions missing an org_id predicate: {offenders}"
