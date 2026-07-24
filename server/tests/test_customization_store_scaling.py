from __future__ import annotations

import json
import sqlite3

from customization_store import SQLiteCustomizationStore


def _payload(tenant_id: str, change_set_id: str) -> dict[str, str]:
    return {"tenant_id": tenant_id, "change_set_id": change_set_id}


def test_confirmation_lookup_is_tenant_scoped_and_single_use(tmp_path):
    database = tmp_path / "customization.db"
    store = SQLiteCustomizationStore(database)

    for index in range(40):
        store.put_confirmation(
            confirmation_id=f"tenant-b-{index}",
            payload=_payload("tenant-b", f"change-{index}"),
            signature=f"signature-b-{index}",
        )
    store.put_confirmation(
        confirmation_id="tenant-a-target",
        payload=_payload("tenant-a", "change-target"),
        signature="signature-a",
    )
    store.put_confirmation(
        confirmation_id="tenant-b-target",
        payload=_payload("tenant-b", "change-target"),
        signature="signature-b",
    )

    found = store.find_unconsumed_confirmation(
        tenant_id="tenant-a", change_set_id="change-target"
    )
    assert found is not None
    assert found["confirmation_id"] == "tenant-a-target"
    assert found["payload"] == _payload("tenant-a", "change-target")
    assert store.consume_confirmation(
        confirmation_id="tenant-a-target", signature="signature-a"
    )
    assert store.find_unconsumed_confirmation(
        tenant_id="tenant-a", change_set_id="change-target"
    ) is None
    assert store.find_unconsumed_confirmation(
        tenant_id="tenant-b", change_set_id="change-target"
    )["confirmation_id"] == "tenant-b-target"


def test_confirmation_lookup_uses_composite_index(tmp_path):
    database = tmp_path / "customization.db"
    store = SQLiteCustomizationStore(database)
    store.initialize()

    with sqlite3.connect(database) as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT confirmation_id, payload_json, signature, consumed "
            "FROM customization_confirmations "
            "WHERE tenant_id = ? AND change_set_id = ? AND consumed = 0 "
            "ORDER BY created_at DESC LIMIT 1",
            ("tenant-a", "change-a"),
        ).fetchall()

    assert any(
        "customization_confirmation_lookup_idx" in detail
        and "tenant_id=?" in detail
        and "change_set_id=?" in detail
        for *_, detail in plan
    ), plan


def test_initialize_backfills_legacy_confirmation_bindings(tmp_path):
    database = tmp_path / "legacy.db"
    payload = _payload("tenant-legacy", "change-legacy")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE customization_confirmations ("
            "confirmation_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, "
            "signature TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO customization_confirmations "
            "(confirmation_id, payload_json, signature) VALUES (?, ?, ?)",
            ("legacy", json.dumps(payload), "legacy-signature"),
        )

    store = SQLiteCustomizationStore(database)
    store.initialize()
    store.initialize()

    found = store.find_unconsumed_confirmation(
        tenant_id="tenant-legacy", change_set_id="change-legacy"
    )
    assert found is not None
    assert found["confirmation_id"] == "legacy"
    with sqlite3.connect(database) as conn:
        binding = conn.execute(
            "SELECT tenant_id, change_set_id FROM customization_confirmations "
            "WHERE confirmation_id = 'legacy'"
        ).fetchone()
    assert binding == ("tenant-legacy", "change-legacy")
