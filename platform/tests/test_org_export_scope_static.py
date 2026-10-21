"""P-153 ownership contract, using only synthetic data and a private module.

Run directly with python -I -B; no application package or conftest is loaded.
"""
import builtins
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import importlib.abc
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import sysconfig
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import uuid


SUBJECT = Path(__file__).resolve().parents[1] / "org_export.py"
STDLIB = Path(sysconfig.get_path("stdlib")).resolve()
PRIVATE = "_org_export_scope_fixture"
A = uuid.UUID("00000000-0000-0000-0000-000000000001")
B = uuid.UUID("00000000-0000-0000-0000-000000000002")
BINDING = uuid.UUID("00000000-0000-0000-0000-000000000003")
PROJECT_IDS = tuple(uuid.UUID(int=n) for n in (11, 12, 13))
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
BODY = "SYNTHETIC_FILE_BODY_MUST_NOT_APPEAR"
MARKER = "PRIVATE_ROW_METADATA"
KEY = "synthetic-export-key"


def _deny(*args, **kwargs):
    raise AssertionError("offline fixture denied external IO")


def _allowed_module(name):
    root = name.split(".", 1)[0]
    return root == PRIVATE or root in sys.stdlib_module_names


class _ImportGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not _allowed_module(fullname):
            raise AssertionError("offline fixture denied application import")
        return None


@contextmanager
def _guards():
    """Reversible guards also work when collected alongside other gate tests."""
    original_import = builtins.__import__
    original_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open

    def check_path(file):
        if isinstance(file, int):
            _deny()
        path = Path(os.fsdecode(file)).resolve()
        if path == SUBJECT:
            return
        if STDLIB in path.parents and not {"site-packages", "dist-packages"}.intersection(path.parts):
            return
        _deny()

    def guarded_open(opener):
        def open_file(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                _deny()
            check_path(file)
            return opener(file, mode, *args, **kwargs)
        return open_file

    def os_open(file, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            _deny()
        check_path(file)
        return original_os_open(file, flags, *args, **kwargs)

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level:
            if not globals or not _allowed_module(globals.get("__package__", "")):
                _deny()
        elif not _allowed_module(name):
            _deny()
        return original_import(name, globals, locals, fromlist, level)

    finder = _ImportGuard()
    with ExitStack() as stack:
        stack.enter_context(patch.object(sys, "dont_write_bytecode", True))
        stack.enter_context(patch.object(builtins, "open", guarded_open(original_open)))
        stack.enter_context(patch.object(io, "open", guarded_open(original_io_open)))
        stack.enter_context(patch.object(os, "open", os_open))
        stack.enter_context(patch.object(builtins, "__import__", guarded_import))
        for obj, name in ((socket, "socket"), (socket, "create_connection"),
                          (socket, "getaddrinfo"), (subprocess, "Popen"),
                          (os, "system"), (os, "popen")):
            stack.enter_context(patch.object(obj, name, _deny))
        sys.meta_path.insert(0, finder)
        try:
            yield
        finally:
            sys.meta_path.remove(finder)


def _validate_key(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid synthetic key")
    return value


class _Unavailable(LookupError):
    pass


class _Forbidden(PermissionError):
    pass


class _Conflict(RuntimeError):
    pass


class _Clock:
    @staticmethod
    def now(tz):
        assert tz is timezone.utc
        return NOW


class _Lifecycle:
    def __init__(self, failures=None):
        self.calls = []
        self.failures = failures or {}

    def export_project(self, org_id, project_id, binding_id, *, idempotency_key):
        self.calls.append((org_id, project_id, binding_id, idempotency_key))
        if project_id in self.failures:
            raise self.failures[project_id]
        return {
            "export_sha256": "a" * 64, "file_count": 2, "member_count": 1,
            "receipt": {"receipt_id": "synthetic-receipt"}, "replayed": False,
            "export": {"files": [BODY]},
        }


class _PoisonRow:
    def __init__(self, owner=A, *, missing=False):
        if not missing:
            self.org_id = owner
        self.metadata_reads = 0

    @property
    def project_id(self):
        self.metadata_reads += 1
        raise AssertionError(MARKER)

    @property
    def name(self):
        self.metadata_reads += 1
        raise AssertionError(MARKER)


class OrgExportScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules_before = set(sys.modules)
        package = ModuleType(PRIVATE)
        package.__path__ = []
        store = ModuleType(PRIVATE + ".store")
        store.list_projects = _deny
        dependency = ModuleType(PRIVATE + ".project_lifecycle")
        dependency._validate_idempotency_key = _validate_key
        dependency.LifecycleUnavailable = _Unavailable
        dependency.LifecycleForbidden = _Forbidden
        dependency.LifecycleConflict = _Conflict
        package.store = store
        package.project_lifecycle = dependency
        subject = ModuleType(PRIVATE + ".org_export")
        subject.__package__ = PRIVATE
        subject.__file__ = str(SUBJECT)
        modules = {PRIVATE: package, store.__name__: store,
                   dependency.__name__: dependency, subject.__name__: subject}
        cls.module_patch = patch.dict(sys.modules, modules)
        cls.module_patch.start()
        cls.addClassCleanup(cls.module_patch.stop)
        with _guards():
            source = SUBJECT.read_text(encoding="utf-8")
            exec(compile(source, str(SUBJECT), "exec"), subject.__dict__)
        cls.subject = subject
        cls.store = store
        cls.loaded_modules = set(sys.modules) - cls.modules_before

    def setUp(self):
        self.enterContext(_guards())
        self.enterContext(patch.object(self.subject, "datetime", _Clock))
        self.actor = SimpleNamespace(org_id=A, binding_id=BINDING)
        self.lifecycle = _Lifecycle()
        self.listed = []

    def _export(self, rows, **kwargs):
        def listing(org_id):
            self.listed.append(org_id)
            self.assertEqual(org_id, A)
            return rows
        return self.subject.export_org(
            A, kwargs.pop("actor", self.actor), idempotency_key=KEY,
            lifecycle=self.lifecycle, list_projects=listing, **kwargs,
        )

    def _reject(self, rows, **kwargs):
        with self.assertNoLogs(self.subject.LOGGER):
            with self.assertRaises(self.subject.OrgExportForbidden) as caught:
                self._export(rows, **kwargs)
        self.assertEqual(str(caught.exception), "project does not belong to this organisation")
        self.assertNotIn(MARKER, str(caught.exception))
        self.assertEqual(self.lifecycle.calls, [])
        self.assertTrue(all(row.metadata_reads == 0 for row in rows))

    @staticmethod
    def _rows():
        return [SimpleNamespace(org_id=A, project_id=pid, name=f"synthetic-{i}")
                for i, pid in enumerate(PROJECT_IDS)]

    def test_actor_mismatch_stops_before_listing(self):
        with self.assertRaises(self.subject.OrgExportForbidden):
            self._export([], actor=SimpleNamespace(org_id=B, binding_id=BINDING))
        self.assertEqual(self.listed, [])
        self.assertEqual(self.lifecycle.calls, [])

    def test_foreign_owner_stops_before_metadata_or_exports(self):
        self._reject([_PoisonRow(A), _PoisonRow(B)])

    def test_unknown_owner_fails_closed(self):
        for row in (_PoisonRow(missing=True), _PoisonRow(None), _PoisonRow(str(A))):
            with self.subTest(owner=getattr(row, "org_id", "missing")):
                self._reject([row])

    def test_foreign_owner_beyond_cap_is_rejected(self):
        self._reject([_PoisonRow(A), _PoisonRow(A), _PoisonRow(B)], max_projects=2)

    def test_same_org_manifest_preserves_contract(self):
        manifest = self._export(self._rows(), max_projects=2)
        reference = {"export_sha256": "a" * 64, "file_count": 2,
                     "member_count": 1, "receipt_id": "synthetic-receipt",
                     "replayed": False}
        self.assertEqual(manifest, {
            "org_id": str(A), "generated_at": NOW.isoformat(), "truncated": True,
            "projects": [{"project_id": str(pid), "name": f"synthetic-{i}",
                          "export_ref": reference} for i, pid in enumerate(PROJECT_IDS[:2])],
        })
        self.assertEqual(self.lifecycle.calls, [(A, pid, BINDING, KEY) for pid in PROJECT_IDS[:2]])
        self.assertEqual(self.listed, [A])
        self.assertNotIn(BODY, repr(manifest))

    def test_same_org_failure_remains_typed(self):
        sentinel = "SYNTHETIC_PRIVATE_EXCEPTION"
        for exception, code in ((_Unavailable(), "not_found"), (_Forbidden(), "forbidden"),
                                (_Conflict(), "conflict"), (ValueError(), "invalid")):
            with self.subTest(code=code):
                self.lifecycle = _Lifecycle({PROJECT_IDS[0]: exception,
                                             PROJECT_IDS[1]: RuntimeError(sentinel)})
                with self.assertLogs(self.subject.LOGGER, level="WARNING") as logs:
                    manifest = self._export(self._rows())
                rows = manifest["projects"]
                self.assertEqual(rows[0], {"project_id": str(PROJECT_IDS[0]),
                                          "name": "synthetic-0", "error": code})
                self.assertEqual(rows[1], {"project_id": str(PROJECT_IDS[1]),
                                          "name": "synthetic-1", "error": "export_failed"})
                self.assertIn("export_ref", rows[2])
                self.assertEqual(len(self.lifecycle.calls), 3)
                self.assertNotIn(sentinel, repr(manifest) + repr(logs.output))

    def test_empty_owned_listing_is_empty_not_incomplete(self):
        self.assertEqual(self._export([]), {
            "org_id": str(A), "generated_at": NOW.isoformat(),
            "projects": [], "truncated": False,
        })
        self.assertEqual(self.lifecycle.calls, [])
        self.assertEqual(self.listed, [A])

    def test_offline_fixture_guards_remain_active(self):
        attempts = (
            lambda: self.store.list_projects(A),
            lambda: self.subject.export_org(A, self.actor, idempotency_key=KEY),
            lambda: socket.socket(),
            lambda: socket.create_connection(("127.0.0.1", 1)),
            lambda: subprocess.Popen(["synthetic-command-never-run"]),
            lambda: os.system("synthetic-command-never-run"),
            lambda: open(SUBJECT, "w"),
            lambda: io.open(SUBJECT, "a"),
            lambda: os.open(SUBJECT, os.O_WRONLY | os.O_TRUNC),
            lambda: open(SUBJECT.parent / "__init__.py"),
        )
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                with self.assertRaises(AssertionError):
                    attempt()
        for name in ("leaf_platform", "server", "boto3", "botocore", "psycopg", "sqlalchemy"):
            with self.subTest(module=name):
                with self.assertRaises(AssertionError):
                    __import__(name)
        loaded = self.loaded_modules | (set(sys.modules) - self.modules_before)
        self.assertTrue(all(_allowed_module(name) for name in loaded), sorted(loaded))
        if __name__ == "__main__":
            self.assertFalse(any(name.split(".", 1)[0] in {
                "leaf_platform", "server", "boto3", "botocore", "psycopg", "sqlalchemy",
            } for name in sys.modules))


if __name__ == "__main__":
    if unittest.defaultTestLoader.loadTestsFromTestCase(OrgExportScopeTests).countTestCases() != 8:
        raise AssertionError("expected eight ownership contract tests")
    unittest.main()
