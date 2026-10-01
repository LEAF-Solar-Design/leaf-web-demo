"""PostgreSQL proofs for retained cards, operation identity and read isolation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid

import psycopg
from psycopg.types.json import Jsonb
import pytest

from leaf_platform import db, engine_change_cards as cards


@pytest.fixture
def fields():
    return {
        "state": "accepted", "incident_fingerprint": "control-noop",
        "feature_id": "studio", "title": "Restore the control",
        "summary": "The control performs its documented action.",
        "change": {"pr_number": 42, "head_sha": "a" * 40},
        "evidence": {"receipt_ids": ["receipt-42"]},
        "acceptance": {"verdict": "accepted", "acceptor": "controller"},
    }


def test_migration_applies_idempotently_and_is_in_readiness():
    migration = Path(__file__).resolve().parents[1] / "migrations/0071_engine_change_cards.sql"
    with db.cursor() as cur:
        cur.execute(migration.read_text(encoding="utf-8"))
        cur.execute(migration.read_text(encoding="utf-8"))
        for table in ("engine_change_cards", "engine_change_card_reads"):
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s", (table,))
            assert db._REQUIRED_COLUMNS[table] <= {row["column_name"] for row in cur.fetchall()}


def _insert_copy(cur, row, **changes):
    values = {**row, **changes}
    names = ("card_id", "operation_id", "state", "incident_fingerprint", "feature_id",
             "title", "summary", "change", "evidence", "acceptance", "payload_sha256")
    cur.execute(
        "INSERT INTO engine_change_cards (" + ", ".join(names) + ") VALUES (" +
        ", ".join("%s" for _ in names) + ")",
        tuple(Jsonb(values[key]) if key in cards.JSON_FIELDS else values[key] for key in names),
    )


def test_operation_id_is_unique_in_postgres(fields):
    row = cards.upsert_card(str(uuid.uuid4()), fields)
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.cursor() as cur:
            _insert_copy(cur, row, card_id=uuid.uuid4())
    assert cards.get_card(row["card_id"]) == row


def test_state_check_constraint_refuses_unknown_state(fields):
    row = cards.upsert_card(str(uuid.uuid4()), fields)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.cursor() as cur:
            _insert_copy(cur, row, card_id=uuid.uuid4(), operation_id=str(uuid.uuid4()), state="unknown")


def test_reads_are_isolated_by_subject(fields):
    row = cards.upsert_card(str(uuid.uuid4()), fields)
    subject = str(uuid.uuid4())
    other = str(uuid.uuid4())
    first = cards.mark_read(row["card_id"], subject)
    assert first == cards.mark_read(row["card_id"], subject)
    assert first["subject"] == subject
    own = cards.list_cards(subject)
    foreign = cards.list_cards(other)
    assert next(card for card in own["cards"] if card["card_id"] == row["card_id"])["unread"] is False
    assert next(card for card in foreign["cards"] if card["card_id"] == row["card_id"])["unread"] is True
    assert foreign["unread_count"] == own["unread_count"] + 1


def test_concurrent_repeats_return_one_card(fields):
    operation_id = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: cards.upsert_card(operation_id, fields), range(8)))
    assert len({row["card_id"] for row in rows}) == 1
    assert all(row == rows[0] for row in rows)
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM engine_change_cards WHERE operation_id = %s", (operation_id,))
        assert cur.fetchone()["n"] == 1


def test_state_advances_and_payload_conflicts(fields):
    operation_id = str(uuid.uuid4())
    first = cards.upsert_card(operation_id, fields)
    for state in ("landed", "live", "reverted"):
        advanced = {**fields, "state": state}
        row = cards.upsert_card(operation_id, advanced)
        assert row["card_id"] == first["card_id"]
        assert row["state"] == state
        assert cards.upsert_card(operation_id, advanced) == row
    with pytest.raises(cards.CardConflict):
        cards.upsert_card(operation_id, fields)
    with pytest.raises(cards.CardConflict):
        cards.upsert_card(operation_id, {**fields, "state": "held", "title": "rewritten"})


def test_sql_cannot_rewrite_or_delete_accepted_facts(fields):
    row = cards.upsert_card(str(uuid.uuid4()), fields)
    for statement in (
        "UPDATE engine_change_cards SET title = 'rewrite' WHERE card_id = %s",
        "UPDATE engine_change_cards SET state = 'live' WHERE card_id = %s",
        "DELETE FROM engine_change_cards WHERE card_id = %s",
    ):
        with pytest.raises(psycopg.errors.RaiseException):
            with db.cursor() as cur:
                cur.execute(statement, (row["card_id"],))
    assert cards.get_card(row["card_id"]) == row


def test_concurrent_hold_requests_retain_first_subject(fields):
    row = cards.upsert_card(str(uuid.uuid4()), fields)
    subjects = (str(uuid.uuid4()), str(uuid.uuid4()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = list(pool.map(lambda subject: cards.request_hold(row["card_id"], subject), subjects))
    assert requests[0] == requests[1]
    assert requests[0]["hold_requested_by"] in subjects
    assert requests[0]["hold_requested_at"] is not None
    assert requests[0]["state"] == "accepted"
    assert cards.upsert_card(row["operation_id"], fields) == requests[0]


def test_jsonb_size_constraint_is_enforced_by_postgres(fields):
    row = cards.upsert_card(str(uuid.uuid4()), fields)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.cursor() as cur:
            _insert_copy(cur, row, card_id=uuid.uuid4(), operation_id=str(uuid.uuid4()), evidence={"oversize": "x" * 32768})
