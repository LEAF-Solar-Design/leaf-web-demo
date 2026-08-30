"""PostgreSQL integration coverage for fleet unit-economics observations."""
from datetime import datetime, timezone
from decimal import Decimal
import uuid

import pytest
from psycopg.errors import ObjectNotInPrerequisiteState

from leaf_platform import unit_economics
from leaf_platform.db import cursor


START = datetime(2099, 1, 1, tzinfo=timezone.utc)
END = datetime(2099, 2, 1, tzinfo=timezone.utc)


def test_observation_replay_is_idempotent_and_reported(make_org):
    make_org(tier="hosted_pro")
    key = f"test-observation-{uuid.uuid4()}"
    category = f"test-hosting-{uuid.uuid4().hex}"
    facts = {
        "idempotency_key": key,
        "period_start": START,
        "period_end": END,
        "kind": "shared_fixed",
        "category": category,
        "amount_usd": Decimal("42.250000"),
        "quantity": None,
        "unit": None,
        "source": "integration-test",
        "source_ref": "invoice-test-only",
        "metadata": {"test": True},
    }
    first = unit_economics.append_observation(**facts)
    replay = unit_economics.append_observation(**facts)
    assert first["recorded"] is True
    assert replay == {"observation_key": first["observation_key"], "recorded": False}

    report = unit_economics.fleet_report(START, END)
    assert report["scope"] == "fleet"
    assert report["observations"]["shared_fixed"][category] == {
        "lines": 1,
        "amount_usd": 42.25,
    }
    assert report["decision_metrics"]["shared_fixed_cost_per_hosted_account_usd"] is not None


def test_observation_idempotency_key_rejects_fact_drift(make_org):
    make_org()
    key = f"test-observation-drift-{uuid.uuid4()}"
    facts = {
        "idempotency_key": key,
        "period_start": START,
        "period_end": END,
        "kind": "revenue",
        "category": "test-subscription-revenue",
        "amount_usd": Decimal("10"),
        "quantity": Decimal("1"),
        "unit": "subscription",
        "source": "integration-test",
        "source_ref": None,
        "metadata": {},
    }
    unit_economics.append_observation(**facts)
    with pytest.raises(unit_economics.LedgerConflict):
        unit_economics.append_observation(**{**facts, "amount_usd": Decimal("11")})


def test_observation_ledger_rejects_update(make_org):
    make_org()
    result = unit_economics.append_observation(
        idempotency_key=f"test-observation-immutable-{uuid.uuid4()}",
        period_start=START,
        period_end=END,
        kind="usage_variable",
        category="test-provider",
        amount_usd=Decimal("1"),
        quantity=Decimal("2"),
        unit="calls",
        source="integration-test",
        source_ref=None,
        metadata={},
    )
    with pytest.raises(ObjectNotInPrerequisiteState):
        with cursor() as cur:
            cur.execute(
                "UPDATE unit_economics_observations SET amount_usd = 2 "
                "WHERE observation_key = %(key)s",
                {"key": result["observation_key"]},
            )
