"""Injected-store contract tests for scripts/reconcile_upload_authority.py.

Offline only: the store is an in-memory stand-in with commit-on-success
transactions, so these prove the reconciler's decisions (dry-run default,
idempotence, zero writes on every refusal), not PostgreSQL concurrency.
"""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reconcile_upload_authority.py"
_spec = importlib.util.spec_from_file_location("reconcile_upload_authority_under_test", SCRIPT)
ru = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ru)

TENANT = "acme"
DRAWING = "d-0001"
ATTEMPT = "0123456789abcdef"


def _marker(**overrides):
    marker = {
        "schema": 1, "status": "ready", "attempt": ATTEMPT, "source_ext": ".dxf",
        "extract_engine": None, "filename": "roof.dxf", "bytes": 12,
        "content_sha256": "a" * 64, "uploaded_at": "2026-09-01T00:00:00Z",
        "retention_expires_at": "2026-09-02T00:00:00Z", "tenant_kind": "guest",
        "error": None, "extracted_version": 1, "intake_ref": "cache/intake.json",
    }
    marker.update(overrides)
    return marker


def _write_marker(root, marker, *, raw=None):
    path = root / ru.marker_key(TENANT, DRAWING)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw if raw is not None else json.dumps(marker).encode("utf-8"))
    return path


class FakeSession:
    def __init__(self, store, read_only):
        self.store = store
        self.read_only = read_only
        self.pending = {}

    def lock(self, tenant_id, drawing_id):
        self.store.locks.append((tenant_id, drawing_id))
        if self.store.on_lock:
            self.store.on_lock()

    def upload_row(self, tenant_id, drawing_id):
        key = (tenant_id, drawing_id)
        row = self.pending.get(key, self.store.rows.get(key))
        return copy.deepcopy(row)

    def purge_receipt_count(self, tenant_id, drawing_id):
        return self.store.receipts.get((tenant_id, drawing_id), 0)

    def drawing_version_ready(self, tenant_id, drawing_id, version):
        return (tenant_id, drawing_id, version) in self.store.ready_versions

    def insert_upload_row(self, tenant_id, drawing_id, row):
        if self.read_only:
            raise AssertionError("write attempted in a read-only transaction")
        key = (tenant_id, drawing_id)
        assert key not in self.store.rows and key not in self.pending
        stored = {
            "attempt": row["attempt"], "status": row["status"],
            # Round-trip through JSON as jsonb would.
            "marker": json.loads(json.dumps(row["marker"])),
            "retention_expires_at": row["retention_expires_at"],
            "extraction_owner": None, "extraction_expires_at": None,
            "purge_owner": None, "purge_expires_at": None,
        }
        if self.store.mangle_insert:
            stored["marker"]["filename"] = "mangled.dxf"
        self.pending[key] = stored
        self.store.insert_calls += 1


class FakeStore:
    def __init__(self, *, rows=None, receipts=None, ready_versions=None):
        self.rows = rows or {}
        self.receipts = receipts or {}
        self.ready_versions = set(ready_versions if ready_versions is not None
                                  else {(TENANT, DRAWING, 1)})
        self.committed_writes = 0
        self.insert_calls = 0
        self.transactions = []
        self.locks = []
        self.on_lock = None
        self.mangle_insert = False

    def assert_ready(self):
        pass

    @contextlib.contextmanager
    def transaction(self, *, read_only):
        self.transactions.append(read_only)
        session = FakeSession(self, read_only)
        yield session
        # Commit only when the body returned normally; an exception skips this.
        self.rows.update(session.pending)
        self.committed_writes += len(session.pending)

    def state(self):
        return copy.deepcopy((self.rows, self.receipts, sorted(self.ready_versions)))


def _row(marker, **overrides):
    row = {
        "attempt": marker["attempt"], "status": marker["status"], "marker": marker,
        "retention_expires_at": marker["retention_expires_at"],
        "extraction_owner": None, "extraction_expires_at": None,
        "purge_owner": None, "purge_expires_at": None,
    }
    row.update(overrides)
    return row


def _run(tmp_path, store, **kwargs):
    params = {"mode": "backfill", "source_dir": tmp_path, "tenant_id": TENANT,
              "drawing_id": DRAWING, "store": store}
    params.update(kwargs)
    return ru.reconcile(**params)


def test_backfill_defaults_to_dry_run_with_zero_writes(tmp_path):
    _write_marker(tmp_path, _marker())
    store = FakeStore()
    receipt = _run(tmp_path, store)
    assert receipt["dry_run"] is True
    assert receipt["would_insert"] == 1
    assert receipt["inserted"] == 0
    assert receipt["parity"] is False
    assert store.transactions == [True]
    assert store.rows == {}
    assert store.committed_writes == 0 and store.insert_calls == 0


def test_apply_imports_once_and_second_run_is_identical(tmp_path):
    _write_marker(tmp_path, _marker())
    store = FakeStore()
    first = _run(tmp_path, store, apply=True)
    assert first["dry_run"] is False
    assert first["inserted"] == 1 and first["parity"] is True
    assert store.transactions == [False]
    after_first = store.state()

    second = _run(tmp_path, store, apply=True)
    assert second["inserted"] == 0 and second["would_insert"] == 0
    assert second["parity"] is True
    assert store.state() == after_first
    assert store.committed_writes == 1

    parity = _run(tmp_path, store, mode="parity")
    assert parity["parity"] is True and parity["dry_run"] is True
    assert store.state() == after_first
    assert store.transactions == [False, False, True]


def test_failed_marker_needs_no_drawing_version(tmp_path):
    _write_marker(tmp_path, _marker(status="failed", extracted_version=None, intake_ref=None,
                                    error={"error_code": "X", "message": "m"}))
    store = FakeStore(ready_versions=set())
    receipt = _run(tmp_path, store, apply=True)
    assert receipt["inserted"] == 1 and receipt["status"] == "failed"
    assert _run(tmp_path, store, apply=True)["inserted"] == 0


def test_parity_reports_missing_row_without_writing(tmp_path):
    _write_marker(tmp_path, _marker())
    store = FakeStore()
    receipt = _run(tmp_path, store, mode="parity")
    assert receipt["parity"] is False
    assert store.transactions == [True]
    assert store.committed_writes == 0


def test_stored_datetime_retention_compares_equal(tmp_path):
    from datetime import datetime, timezone
    marker = _marker()
    _write_marker(tmp_path, marker)
    stored = _row(marker, retention_expires_at=datetime(2026, 9, 2, tzinfo=timezone.utc))
    store = FakeStore(rows={(TENANT, DRAWING): stored})
    assert _run(tmp_path, store, mode="parity")["parity"] is True


def _existing(marker, **overrides):
    return {(TENANT, DRAWING): _row(marker, **overrides)}


REFUSALS = {
    "source_extracting": (dict(marker=_marker(status="extracting")), {}),
    "source_purging": (dict(marker=_marker(status="purging")), {}),
    "source_postgres_compat": (dict(raw=b'{"authority":"postgres"}'), {}),
    "source_bad_schema": (dict(marker=_marker(schema=2)), {}),
    "source_bool_schema": (dict(marker=_marker(schema=True)), {}),
    "source_bad_attempt": (dict(marker=_marker(attempt="XYZ")), {}),
    "source_unknown_kind": (dict(marker=_marker(tenant_kind="robot")), {}),
    "source_naive_retention": (dict(marker=_marker(retention_expires_at="2026-09-02T00:00:00")), {}),
    "source_ready_without_version": (dict(marker=_marker(extracted_version=None)), {}),
    "source_duplicate_key": (dict(raw=b'{"schema":1,"schema":1}'), {}),
    "source_nan": (dict(raw=b'{"schema":1,"bytes":NaN}'), {}),
    "source_not_object": (dict(raw=b'[1]'), {}),
    "source_missing": (dict(skip=True), {}),
    "target_extracting": (dict(marker=_marker()),
                          dict(rows=_existing(_marker(status="extracting"), status="extracting"))),
    "target_extraction_lease": (dict(marker=_marker()),
                                dict(rows=_existing(_marker(), extraction_owner="w-1",
                                                    extraction_expires_at="2026-09-01T00:00:00Z"))),
    "target_purge_lease": (dict(marker=_marker()),
                           dict(rows=_existing(_marker(), status="purging", purge_owner="p-1"))),
    "target_purged": (dict(marker=_marker()),
                      dict(rows=_existing(_marker(status="purged"), status="purged"))),
    "target_other_attempt": (dict(marker=_marker()),
                             dict(rows=_existing(_marker(attempt="fedcba9876543210")))),
    "target_marker_conflict": (dict(marker=_marker()),
                               dict(rows=_existing(_marker(filename="other.dxf")))),
    "target_purge_receipts": (dict(marker=_marker()), dict(receipts={(TENANT, DRAWING): 1})),
    "ready_drawing_not_reconciled": (dict(marker=_marker()), dict(ready_versions=set())),
}


@pytest.mark.parametrize("apply", [False, True], ids=["dry_run", "apply"])
@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_every_refusal_writes_nothing(tmp_path, case, apply):
    source, store_kwargs = REFUSALS[case]
    if not source.get("skip"):
        _write_marker(tmp_path, source.get("marker"), raw=source.get("raw"))
    store = FakeStore(**copy.deepcopy(store_kwargs))
    before = store.state()
    with pytest.raises(ValueError):
        _run(tmp_path, store, apply=apply)
    assert store.state() == before
    assert store.committed_writes == 0
    assert store.insert_calls == 0


@pytest.mark.parametrize("kwargs", [
    dict(scope="purge_receipts", apply=True),
    dict(scope="everything"),
    dict(mode="reverse"),
    dict(mode="parity", apply=True),
    dict(tenant_id="../escape"),
    dict(drawing_id="Upper"),
], ids=["purge_scope", "unknown_scope", "reverse_mode", "parity_apply", "bad_tenant", "bad_drawing"])
def test_argument_refusals_never_open_a_transaction(tmp_path, kwargs):
    _write_marker(tmp_path, _marker())
    store = FakeStore()
    before = store.state()
    with pytest.raises(ValueError):
        _run(tmp_path, store, **kwargs)
    assert store.transactions == []
    assert store.state() == before


def test_post_import_mismatch_rolls_back(tmp_path):
    _write_marker(tmp_path, _marker())
    store = FakeStore()
    store.mangle_insert = True
    with pytest.raises(ValueError, match="post-import conflict"):
        _run(tmp_path, store, apply=True)
    assert store.rows == {} and store.committed_writes == 0


def test_source_change_during_reconciliation_rolls_back(tmp_path):
    path = _write_marker(tmp_path, _marker())
    store = FakeStore()
    store.on_lock = lambda: path.write_bytes(json.dumps(_marker(filename="new.dxf")).encode())
    with pytest.raises(ValueError, match="source marker changed"):
        _run(tmp_path, store, apply=True)
    assert store.rows == {} and store.committed_writes == 0


def test_lock_id_is_shared_with_the_drawing_reconciler():
    text = (ROOT / "scripts" / "reconcile_drawing_authority.py").read_text(encoding="utf-8")
    assert 'hashlib.sha256(f"{tenant_id}/{drawing_id}".encode()).digest()[:8], "big", signed=True' in text
    assert isinstance(ru.advisory_lock_id(TENANT, DRAWING), int)


def test_cli_refuses_without_database_url(tmp_path, monkeypatch, capsys):
    _write_marker(tmp_path, _marker())
    monkeypatch.delenv("DATABASE_URL", raising=False)
    code = ru.main(["--mode", "backfill", "--source-dir", str(tmp_path),
                    "--tenant-id", TENANT, "--drawing-id", DRAWING])
    assert code == 2
    assert "explicit DATABASE_URL required" in capsys.readouterr().err


def test_cli_refuses_purge_receipt_scope(tmp_path, capsys):
    _write_marker(tmp_path, _marker())
    code = ru.main(["--mode", "backfill", "--source-dir", str(tmp_path), "--tenant-id", TENANT,
                    "--drawing-id", DRAWING, "--scope", "purge_receipts"])
    assert code == 2
    assert "purge-receipt scope" in capsys.readouterr().err


def test_inventory_records_partial_upload_coverage():
    inventory = json.loads((ROOT / "platform" / "authority-inventory.json").read_text(encoding="utf-8"))
    upload = next(a for a in inventory["authorities"] if a["id"] == "upload_metadata")
    for phase in ("backfill", "parity"):
        claim = upload[phase]
        assert claim["command"].startswith("python /app/scripts/reconcile_upload_authority.py ")
        assert claim["status"] not in {"complete", "implemented"}
        assert "partial" in claim["status"]
        assert "purge receipt" in claim["note"].lower()
    assert "--apply" in upload["backfill"]["command"]
    assert "--apply" not in upload["parity"]["command"]
    for environment in ("staging", "production"):
        assert upload["current_selection"][environment]["status"] != "verified"


def test_image_ships_and_guards_the_reconciler():
    dockerfile = (ROOT / "deploy" / "Dockerfile.app").read_text(encoding="utf-8")
    assert ("COPY scripts/reconcile_upload_authority.py "
            "/app/scripts/reconcile_upload_authority.py") in dockerfile
    for flag in ("-f", "-s", "-r"):
        assert f"test {flag} /app/scripts/reconcile_upload_authority.py" in dockerfile
    manifest = (ROOT / "scripts" / "platform_release_manifest.py").read_text(encoding="utf-8")
    assert '"scripts/reconcile_upload_authority.py",' in manifest
