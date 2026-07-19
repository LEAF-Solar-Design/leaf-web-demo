"""Wave-3 lane 3D (C6) — deletion/compliance columns + soft-delete + hard-PURGE.

Binding schema contract (DELETION-OFFBOARDING-DESIGN.md sec 4): the org / Project /
Job rows carry three deletion columns from day one so deletion-on-request cannot be
retrofitted:

    deleted_at          TIMESTAMPTZ NULL   soft-delete marker; NULL = live/visible
    purge_requested_at  TIMESTAMPTZ NULL   hard-PURGE accepted; opens the audit window
    purge_completed_at  TIMESTAMPTZ NULL   hard-PURGE cascade finished across all stores

Acceptance proven here (binary):
  * migration 0002 applies idempotently — all FIVE tables gain the three columns,
    a re-apply is a clean no-op;
  * a soft-deleted project is hidden from every default store read but RETAINED
    (recoverable) in the DB;
  * the offboard/hard-PURGE path stamps BOTH timestamps (requested <= completed)
    and fires the per-store purge hooks exactly once per ref;
  * org-scoping is preserved (soft_delete cannot cross tenants; store-guard stays green);
  * lane 1B's constant-time admin-token compare + live-auth gate survive on api.offboard.

Migration ordering: conftest applies 0001 only, and the store reads now reference
`deleted_at`, so 0002 must be applied before any read runs. This module sorts first
alphabetically (test_deletion_3d) and its session-autouse fixture applies 0002 (after
0001, idempotently) before its first test — and the Neon branch is persistent, so the
columns then exist for the whole session.

Run:  cd C:/tmp && python -m pytest C:/tmp/leaf-web-demo/platform/tests -q
"""
from __future__ import annotations

import pathlib
import uuid

import pytest

import leaf_platform.db as db
import leaf_platform.store as store
from leaf_platform.offboard import offboard_org

_PKG_DIR = pathlib.Path(__file__).resolve().parent.parent
_MIG_0002 = _PKG_DIR / "migrations" / "0002_deletion_columns.sql"

_TABLES = ("orgs", "projects", "drawing_versions", "jobs", "built_tools")
_COLS = ("deleted_at", "purge_requested_at", "purge_completed_at")


@pytest.fixture(scope="session", autouse=True)
def _apply_0002():
    """Ensure the deletion columns exist before any store read references them.

    Self-sufficient: applies 0001 (idempotent CREATE TABLE IF NOT EXISTS) then 0002
    (idempotent ADD COLUMN IF NOT EXISTS), independent of conftest's own migrate
    fixture ordering.
    """
    db.apply_migration()             # 0001 — guarantees the five tables exist
    db.apply_migration(_MIG_0002)    # 0002 — the deletion/compliance columns
    yield


def _columns(table: str) -> dict:
    with db.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %(t)s",
            {"t": table},
        )
        return {r["column_name"]: r["data_type"] for r in cur.fetchall()}


# --------------------------------------------------------------------------- #
# migration idempotency — 5 tables gain the 3 columns; re-apply is clean
# --------------------------------------------------------------------------- #
def test_migration_0002_idempotent_five_tables_gain_columns():
    # apply a SECOND time on top of the fixture's apply -> must be a clean no-op
    db.apply_migration(_MIG_0002)
    db.apply_migration(_MIG_0002)  # and a third, to be sure

    for table in _TABLES:
        cols = _columns(table)
        for c in _COLS:
            assert c in cols, f"{table}.{c} missing after migration 0002"
            assert cols[c] == "timestamp with time zone", (
                f"{table}.{c} should be TIMESTAMPTZ, got {cols[c]!r}"
            )


# --------------------------------------------------------------------------- #
# soft-delete: hidden from every default read, but RETAINED + recoverable
# --------------------------------------------------------------------------- #
def test_soft_delete_hides_from_reads_but_retains(make_org):
    org = make_org(name="Soft Delete Org")
    p = store.create_project(org.org_id, "to-be-hidden")

    # visible before soft-delete
    assert p.project_id in [x.project_id for x in store.list_projects(org.org_id)]
    assert store.get_project(org.org_id, p.project_id) is not None

    # soft-delete once (True), a second time is a no-op (False, already hidden)
    assert store.soft_delete_project(org.org_id, p.project_id) is True
    assert store.soft_delete_project(org.org_id, p.project_id) is False

    # hidden from every default read
    assert p.project_id not in [x.project_id for x in store.list_projects(org.org_id)]
    assert store.get_project(org.org_id, p.project_id) is None

    # but RETAINED: the row still exists, with deleted_at stamped (recoverable)
    with db.cursor() as cur:
        cur.execute(
            "SELECT deleted_at FROM projects WHERE project_id = %(pid)s",
            {"pid": p.project_id},
        )
        row = cur.fetchone()
    assert row is not None, "soft-deleted row must be RETAINED, not physically removed"
    assert row["deleted_at"] is not None, "deleted_at must be stamped on soft-delete"

    # recovery path proven: clearing deleted_at brings it back to default reads
    with db.cursor() as cur:
        cur.execute(
            "UPDATE projects SET deleted_at = NULL WHERE project_id = %(pid)s",
            {"pid": p.project_id},
        )
    assert store.get_project(org.org_id, p.project_id) is not None


def test_soft_delete_is_org_scoped(make_org):
    """A soft-delete cannot cross tenants (org-scoping preserved)."""
    owner = make_org(name="Owner Org")
    other = make_org(name="Other Org")
    p = store.create_project(owner.org_id, "owned")

    # another org cannot soft-delete it
    assert store.soft_delete_project(other.org_id, p.project_id) is False
    # still live for its real owner
    assert store.get_project(owner.org_id, p.project_id) is not None


# --------------------------------------------------------------------------- #
# hard-PURGE: stamps BOTH timestamps + fires hooks exactly once per ref
# --------------------------------------------------------------------------- #
def _seed(org_id, tag: str):
    p = store.create_project(org_id, f"{tag} project")
    store.create_drawing_version(
        org_id, p.project_id,
        oss_object=f"oss/{tag}/v1.dwg", intake_ref=f"intake/{tag}/v1.json", created_by="agent",
    )
    store.create_built_tool(
        org_id, p.project_id, "count-by-layer",
        manifest={"name": "count-by-layer", "version": "1.0.0"},
        source_ref=f"mushy/{tag}/count-by-layer",
    )
    return p


def test_purge_stamps_both_timestamps_and_fires_hooks_once(make_org):
    org = make_org(name="Purge Org")
    _seed(org.org_id, "p")

    key_calls, blob_calls = [], []
    # a DUPLICATE secret ref proves the dedup fires the hook exactly once per store ref
    secret_refs = [
        f"leaf/{org.org_id}/cred-1",
        f"leaf/{org.org_id}/cred-2",
        f"leaf/{org.org_id}/cred-1",
    ]
    result = offboard_org(
        org.org_id,
        key_purge_hook=key_calls.append,
        blob_purge_hook=blob_calls.append,
        secret_ref_provider=lambda oid: secret_refs,
    )

    # both timestamps stamped, and the request edge precedes completion
    assert result.purge_requested_at is not None
    assert result.purge_completed_at is not None
    assert result.purge_requested_at <= result.purge_completed_at

    # persisted on the org tombstone
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, purge_requested_at, purge_completed_at, offboarded_at "
            "FROM orgs WHERE org_id = %(o)s",
            {"o": org.org_id},
        )
        row = cur.fetchone()
    assert row["status"] == "deleted"
    assert row["purge_requested_at"] is not None
    assert row["purge_completed_at"] is not None
    assert row["offboarded_at"] is not None

    # key hook fired exactly once per DISTINCT secret ref (no double-fire)
    expected_keys = {f"leaf/{org.org_id}/cred-1", f"leaf/{org.org_id}/cred-2"}
    assert set(key_calls) == expected_keys
    assert len(key_calls) == len(set(key_calls)) == len(expected_keys)

    # blob hook fired exactly once per owned out-of-band ref
    expected_blobs = {"oss/p/v1.dwg", "intake/p/v1.json", "mushy/p/count-by-layer"}
    assert set(blob_calls) == expected_blobs
    assert len(blob_calls) == len(set(blob_calls)) == len(expected_blobs)


def test_purge_requested_at_first_wins_on_reinvoke(make_org):
    """Re-invoking the purge keeps the ORIGINAL request time (COALESCE, audit window)."""
    org = make_org(name="Reinvoke Org")
    r1 = offboard_org(org.org_id, key_purge_hook=lambda ref: None, blob_purge_hook=lambda ref: None)
    assert r1.purge_requested_at is not None

    r2 = offboard_org(org.org_id, key_purge_hook=lambda ref: None, blob_purge_hook=lambda ref: None)
    assert r2.purge_requested_at == r1.purge_requested_at, "request edge must not move"
    assert r2.purge_completed_at >= r1.purge_completed_at
    assert r2.deleted_projects == 0  # already purged, nothing left


# --------------------------------------------------------------------------- #
# store helpers stamp the org tombstone; unknown org -> None
# --------------------------------------------------------------------------- #
def test_mark_purge_helpers_stamp_org(make_org):
    org = make_org(name="Helper Org")

    req = store.mark_purge_requested(org.org_id)
    assert req is not None
    # first-wins
    assert store.mark_purge_requested(org.org_id) == req

    comp = store.mark_purge_completed(org.org_id)
    assert comp is not None and comp >= req

    # unknown org -> None (no row updated)
    assert store.mark_purge_requested(uuid.uuid4()) is None
    assert store.mark_purge_completed(uuid.uuid4()) is None


# --------------------------------------------------------------------------- #
# API end-to-end: offboard response surfaces the purge window (through api.offboard)
# --------------------------------------------------------------------------- #
def test_api_offboard_surfaces_purge_timestamps_and_preserves_1b(client, make_org, monkeypatch):
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)  # off-auth demo path
    monkeypatch.setenv("PLATFORM_ADMIN_TOKEN", "s3cret-admin-token")
    org = make_org(name="API Purge Org")

    # 1B preserved: wrong token still constant-time-rejected with 403
    r_wrong = client.delete(f"/api/orgs/{org.org_id}", headers={"X-Admin-Token": "nope"})
    assert r_wrong.status_code == 403, r_wrong.text

    # correct token proceeds and the response carries the purge window
    r = client.delete(f"/api/orgs/{org.org_id}", headers={"X-Admin-Token": "s3cret-admin-token"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "deleted"
    assert body["org_id"] == str(org.org_id)
    assert body["purge_requested_at"] is not None
    assert body["purge_completed_at"] is not None


def test_1b_admin_compare_and_auth_gate_source_preserved():
    """Static proof that lane 1B's F16 + F6 hardening survived this lane's edit."""
    src = (_PKG_DIR / "api.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in src, "1B constant-time compare must survive"
    assert "if x_admin_token != admin_token" not in src, "the timing-leaky compare must stay gone"
    assert "require_auth_when_live" in src, "1B live-auth gate on POST /api/orgs must survive"
