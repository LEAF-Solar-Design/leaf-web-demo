"""Test harness for the platform package.

THE stdlib-`platform` shadow fix (verified empirically; see platform/README.md):
this directory tree contains a package named ``platform/`` which shadows Python's
stdlib ``platform`` module whenever the repo root is on ``sys.path``. This conftest:

  1. Evicts any shadowing ``platform`` entries from sys.path and sys.modules and
     re-resolves the genuine stdlib ``platform`` module — so ``import platform``
     inside the test process returns the stdlib (verified by test_store_guard).
  2. Registers the package under the NON-colliding alias ``leaf_platform`` (loaded
     straight from its directory), which every test imports via.

There is deliberately NO ``platform/tests/__init__.py``: with it present, pytest
imports the tests as a subpackage of ``platform`` and collides with the cached
stdlib module (`'platform' is not a package`). Omitting it keeps the tests as
top-level modules. This is the single sanctioned deviation from the file list.

DB: reads DATABASE_URL (env or platform/.env.local), applies migration 0001 once
per session, and hands out fresh-UUID orgs per test (so tests are mutually isolated
by org scoping — no global truncation, sibling-safe on a shared branch).
"""
import importlib.util
import os
import pathlib
import sys

import pytest

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
_PKG_DIR = _TESTS_DIR.parent            # .../platform
_REPO_ROOT = _PKG_DIR.parent            # .../leaf-web-demo


def _abs(p: str) -> str:
    return os.path.abspath(p or ".")


# --- 1. defend the stdlib `platform` module --------------------------------- #
# `python -m pytest` puts the repo root (CWD) on sys.path where platform/ shadows
# the stdlib; the console `pytest` script does not. Handle both.
sys.path[:] = [p for p in sys.path if _abs(p) != str(_REPO_ROOT)]
_cur = sys.modules.get("platform")
if _cur is not None and str(getattr(_cur, "__file__", "") or "").startswith(str(_PKG_DIR)):
    del sys.modules["platform"]
import platform as _stdlib_platform  # noqa: E402,F401  re-resolves to the stdlib

# --- 2. expose the package under a non-colliding alias ---------------------- #
if "leaf_platform" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "leaf_platform",
        _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)],
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["leaf_platform"] = _mod
    _spec.loader.exec_module(_mod)

_DB_CONFIGURED = bool(os.environ.get("DATABASE_URL") or (_PKG_DIR / ".env.local").exists())
if _DB_CONFIGURED:
    from leaf_platform import db as _db  # noqa: E402
    from leaf_platform import store as _store  # noqa: E402


def pytest_ignore_collect(collection_path, config):
    """Keep dependency-free static checks runnable on a clean checkout.

    The existing integration suite intentionally imports psycopg and needs a
    PostgreSQL URL.  Without either, avoid importing it at collection time while
    preserving the ``*_static.py`` proof modules (ledger, hashing, replay):
    those load their targets by file path, import nothing DB-shaped, and are
    exactly the tests that must run everywhere.
    """
    path = pathlib.Path(str(collection_path))
    if not _DB_CONFIGURED and path.parent == _TESTS_DIR \
            and not path.name.endswith("_static.py"):
        return True
    return False


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """Apply every idempotent platform migration once per PostgreSQL session."""
    if not _DB_CONFIGURED:
        yield
        return
    _db.apply_migration()
    yield
    _db.reset_pool()


@pytest.fixture
def make_org():
    """Factory: create a fresh org (unique UUID) so tests don't collide on shared data."""
    if not _DB_CONFIGURED:
        pytest.skip("PostgreSQL integration test requires DATABASE_URL")
    created = []

    def _make(name="Test Org", tier="hosted_starter"):
        org = _store.create_org(name=name, tier=tier)
        created.append(org)
        return org

    return _make


@pytest.fixture
def client():
    """A TestClient over a minimal app that mounts ONLY the platform router.

    This also demonstrates the documented one-line integration mount.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from leaf_platform.api import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)
