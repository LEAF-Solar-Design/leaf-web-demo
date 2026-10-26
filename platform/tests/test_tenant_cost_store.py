"""Pure validation always runs; PostgreSQL coverage uses isolated fixture orgs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid

import pytest

from leaf_platform import tenant_cost_store as store


ORG = uuid.UUID("b15f227e-ecb7-4c58-82ad-05360542a56c")
START = datetime(2099, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(days=1)


def event(**changes):
    return {
        "org_id": ORG, "addon": "aps_solve", "mode": "managed",
        "vendor": "autodesk", "unit": "second", "quantity": Decimal("2"),
        "unit_cost_usd": Decimal("0.125"), "cost_usd": Decimal("0.25"),
        "cost_basis": "measured", "event_key": "a" * 64,
        "occurred_at": START, "provenance": {"adapter": "test", "facts": [True, None, 1]},
        **changes,
    }


def test_good_event_is_normalized():
    result = store.validate_cost_event(event(org_id=str(ORG), quantity=2.5))
    assert result["org_id"] == ORG
    assert result["quantity"] == Decimal("2.5")
    assert store.validate_cost_event(event(provenance={}))["provenance"] == {}


def test_unknown_keys_name_the_field():
    with pytest.raises(ValueError, match="secret"):
        store.validate_cost_event(event(secret="forbidden"))


def test_missing_fields_name_the_field():
    for field in store.EVENT_FIELDS:
        facts = event()
        del facts[field]
        with pytest.raises(ValueError, match=field):
            store.validate_cost_event(facts)


def test_enum_fields_fail_closed():
    for field in ("addon", "mode", "cost_basis"):
        for value in ("unknown", "", None, [], {}):
            with pytest.raises(ValueError, match=field):
                store.validate_cost_event(event(**{field: value}))
    for addon in store.ADDONS:
        assert store.validate_cost_event(event(addon=addon))["addon"] == addon


def test_text_and_org_fields_fail_closed():
    for field, values in (
        ("org_id", [None, 123, "not-a-uuid"]),
        ("vendor", [None, 42, "", " ", "v" * 101]),
        ("unit", [None, "", " ", "u" * 51]),
    ):
        for value in values:
            with pytest.raises(ValueError, match=field):
                store.validate_cost_event(event(**{field: value}))


def test_numbers_must_be_nonnegative_and_finite():
    for field in ("quantity", "unit_cost_usd", "cost_usd"):
        for value in (-1, float("inf"), float("-inf"), float("nan"),
                      Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), True, "1", None):
            with pytest.raises(ValueError, match=field):
                store.validate_cost_event(event(**{field: value}))
        assert store.validate_cost_event(event(**{field: 0}))[field] == 0


def test_money_must_fit_without_rounding():
    for field in ("unit_cost_usd", "cost_usd"):
        for value in (Decimal("1000000000000"), Decimal("0.0000001")):
            with pytest.raises(ValueError, match=field):
                store.validate_cost_event(event(**{field: value}))
        value = Decimal("999999999999.999999")
        assert store.validate_cost_event(event(**{field: value}))[field] == value


def test_event_key_is_lowercase_sha256():
    for value in (None, 42, "A" * 64, "a" * 63, "a" * 65, "g" * 64, "a" * 64 + "\n"):
        with pytest.raises(ValueError, match="event_key"):
            store.validate_cost_event(event(event_key=value))


def test_occurred_at_must_be_aware():
    for value in (START.replace(tzinfo=None), START.isoformat(), None):
        with pytest.raises(ValueError, match="occurred_at"):
            store.validate_cost_event(event(occurred_at=value))
    shifted = START.astimezone(timezone(timedelta(hours=3)))
    assert store.validate_cost_event(event(occurred_at=shifted))["occurred_at"] == START


def test_provenance_must_be_a_strict_json_object():
    cycle = {}
    cycle["cycle"] = cycle
    for value in ([], None, "{}", {"bad": object()}, {1: "bad"},
                  {"bad": float("nan")}, {"bad": (1, 2)}, cycle):
        with pytest.raises(ValueError, match="provenance"):
            store.validate_cost_event(event(provenance=value))


def test_provenance_size_boundary_counts_encoded_bytes():
    # Compact encoding of {"x":""} costs eight bytes; the bound is exclusive.
    accepted = {"x": "a" * (store.MAX_PROVENANCE_BYTES - 9)}
    assert store.validate_cost_event(event(provenance=accepted))["provenance"] == accepted
    for value in ({"x": "a" * (store.MAX_PROVENANCE_BYTES - 8)}, {"x": "\u00e9" * 10000}):
        with pytest.raises(ValueError, match="provenance"):
            store.validate_cost_event(event(provenance=value))


def test_byo_events_have_zero_platform_cost():
    assert store.validate_cost_event(event(mode="byo", cost_usd=0))["cost_usd"] == 0
    with pytest.raises(ValueError, match="cost_usd"):
        store.validate_cost_event(event(mode="byo"))


def test_event_key_is_deterministic_and_binds_each_input():
    values = [ORG, "aps_solve", "adapter", "job:123"]
    key = store.cost_event_key(*values)
    assert len(key) == 64 and int(key, 16) >= 0
    assert key == store.cost_event_key(str(ORG), *values[1:])
    for index, replacement in enumerate((uuid.uuid4(), "e2b_exec", "other", "job:124")):
        changed = list(values)
        changed[index] = replacement
        assert store.cost_event_key(*changed) != key
    assert store.cost_event_key(ORG, "aps_solve", "a:b", "c") != store.cost_event_key(ORG, "aps_solve", "a", "b:c")


def test_settings_validate_org_addon_mode_and_actor():
    for field, value in (("org_id", "bad"), ("addon", "bad"), ("mode", "bad"),
                         ("actor", ""), ("actor", " " * 3), ("actor", "a" * 201)):
        params = {"org_id": ORG, "addon": "aps_solve", "mode": "managed", "actor": "operator"}
        params[field] = value
        with pytest.raises(ValueError, match=field):
            store.validate_addon_mode(**params)


def test_byo_ref_requires_a_credential_name():
    for ref in (None, "", "value with spaces", "v" * 129, "token=secret", "name\n"):
        with pytest.raises(ValueError, match="byo_ref"):
            store.validate_addon_mode(ORG, "aps_solve", "byo", byo_ref=ref, actor="operator")
    for ref in ("vault/team:aps_key-1", "a" * 128):
        assert store.validate_addon_mode(ORG, "aps_solve", "byo", byo_ref=ref, actor="operator")["byo_ref"] == ref


def test_other_modes_require_null_ref():
    for mode in ("managed", "off"):
        assert store.validate_addon_mode(ORG, "aps_solve", mode, actor="operator")["byo_ref"] is None
        with pytest.raises(ValueError, match="byo_ref"):
            store.validate_addon_mode(ORG, "aps_solve", mode, byo_ref="vault/key", actor="operator")


def test_list_validation_precedes_database_access():
    for changes, field in (({"limit": 10001}, "limit"), ({"limit": 0}, "limit"),
                           ({"limit": True}, "limit"), ({"limit": 1.5}, "limit"),
                           ({"addon": "bad"}, "addon"), ({"org_id": "bad"}, "org_id"),
                           ({"start": START.replace(tzinfo=None)}, "start"),
                           ({"end": START}, "end")):
        params = {"org_id": ORG, "start": START, "end": END, **changes}
        with pytest.raises(ValueError, match=field):
            store.list_cost_events(None, **params)


def test_validation_does_not_mutate_input_or_share_provenance():
    facts = event()
    original = deepcopy(facts)
    normalized = store.validate_cost_event(facts)
    normalized["provenance"]["facts"].append("new")
    assert facts == original


def test_writes_validate_before_using_connection():
    with pytest.raises(ValueError, match="addon"):
        store.record_cost_event(None, event(addon="bad"))
    with pytest.raises(ValueError, match="byo_ref"):
        store.set_addon_mode(None, ORG, "aps_solve", "byo", actor="operator")


def test_settings_ignore_unknown_rows():
    rows = [
        {"addon": "future_addon", "mode": {"arbitrary": []}, "byo_ref": False},
        {"addon": "aps_solve", "mode": "byo", "byo_ref": "vault/team:aps"},
        {"addon": "__proto__"},
        {"addon": "constructor"},
        {"addon": "storage", "mode": "managed", "byo_ref": None},
        {"addon": "prototype"},
    ]
    original = deepcopy(rows)
    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params):
            calls.append((query, params))

        def fetchall(self):
            return rows

    class Connection:
        def cursor(self):
            return Cursor()

    settings = store.get_addon_settings(Connection(), str(ORG))
    assert calls == [("SELECT addon, mode, byo_ref FROM tenant_addon_settings WHERE org_id = %s", (ORG,))]
    assert set(settings) == set(store.ADDONS)
    assert settings["aps_solve"] == {"mode": "byo", "byo_ref": "vault/team:aps"}
    assert settings["storage"] == {"mode": "managed", "byo_ref": None}
    assert all(settings[addon] == {"mode": "off", "byo_ref": None}
               for addon in store.ADDONS if addon not in ("aps_solve", "storage"))
    settings["aps_solve"]["byo_ref"] = "changed"
    settings["e2b_exec"]["mode"] = "managed"
    assert settings["tool_ci"] == {"mode": "off", "byo_ref": None}
    assert rows == original


def test_db_insert_duplicate_and_conflicting_replays(make_org):
    from leaf_platform.db import connection

    org = make_org()
    facts = event(org_id=org.org_id, event_key=store.cost_event_key(org.org_id, "aps_solve", "test", "replay"))
    with connection() as conn:
        assert store.record_cost_event(conn, facts) == "inserted"
        assert store.record_cost_event(conn, facts) == "duplicate"
        assert store.record_cost_event(conn, {**facts, "cost_usd": 0.250000}) == "duplicate"
        for field, value in (("cost_usd", 1), ("quantity", 3), ("vendor", "other"),
                             ("provenance", {"changed": True}), ("addon", "e2b_exec")):
            with pytest.raises(store.CostEventConflict):
                store.record_cost_event(conn, {**facts, field: value})


def test_db_ledger_refuses_update_and_delete(make_org):
    from psycopg.errors import ObjectNotInPrerequisiteState
    from leaf_platform.db import connection

    org = make_org()
    facts = event(org_id=org.org_id, event_key=store.cost_event_key(org.org_id, "aps_solve", "test", "immutable"))
    with connection() as conn:
        store.record_cost_event(conn, facts)
        for sql in ("UPDATE tenant_cost_events SET cost_usd = 0 WHERE event_key = %s",
                    "DELETE FROM tenant_cost_events WHERE event_key = %s"):
            with pytest.raises(ObjectNotInPrerequisiteState):
                with conn.transaction():
                    conn.execute(sql, (facts["event_key"],))


def test_db_list_is_bounded_ordered_filtered_and_tenant_scoped(make_org):
    from leaf_platform.db import connection

    org, other = make_org(), make_org()
    with connection() as conn:
        for tenant, ref, offset, addon in ((org, "later", 2, "aps_solve"),
                                          (org, "tie-first", 1, "e2b_exec"),
                                          (org, "tie-second", 1, "aps_solve"),
                                          (org, "end", 86400, "aps_solve"),
                                          (other, "foreign", 0, "aps_solve")):
            store.record_cost_event(conn, event(
                org_id=tenant.org_id, addon=addon, occurred_at=START + timedelta(seconds=offset),
                event_key=store.cost_event_key(tenant.org_id, addon, "test", ref)))
        rows = store.list_cost_events(conn, org.org_id, START, END)
        assert len(rows) == 3
        assert [(row["occurred_at"], row["id"]) for row in rows] == sorted(
            (row["occurred_at"], row["id"]) for row in rows)
        assert [row["addon"] for row in rows] == ["e2b_exec", "aps_solve", "aps_solve"]
        assert len(store.list_cost_events(conn, org.org_id, START, END, limit=1)) == 1
        assert len(store.list_cost_events(conn, org.org_id, START, END, addon="aps_solve")) == 2


def test_db_settings_default_off_and_round_trip(make_org):
    from leaf_platform.db import connection

    org, other = make_org(), make_org()
    with connection() as conn:
        defaults = store.get_addon_settings(conn, org.org_id)
        assert set(defaults) == set(store.ADDONS)
        assert all(value == {"mode": "off", "byo_ref": None} for value in defaults.values())
        for mode, ref in (("byo", "vault/team:aps"), ("managed", None), ("off", None)):
            store.set_addon_mode(conn, org.org_id, "aps_solve", mode, byo_ref=ref, actor="operator")
            assert store.get_addon_settings(conn, org.org_id)["aps_solve"] == {"mode": mode, "byo_ref": ref}
        assert store.get_addon_settings(conn, other.org_id) == defaults
        with conn.cursor() as cur:
            cur.execute("SELECT updated_at, updated_by FROM tenant_addon_settings WHERE org_id = %s", (org.org_id,))
            row = cur.fetchone()
        assert row["updated_at"].tzinfo is not None
        assert row["updated_by"] == "operator"
