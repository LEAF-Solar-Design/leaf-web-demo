"""da/test_workitem_limits.py: every WorkItem carries an explicit processing limit.

APS applies a 100-second engine default to a WorkItem submitted without
`limitProcessingTimeSec`, which kills any real-sized drawing mid-extract
(observed 2026-08-10: a 26MB intake died at exactly 100s with
status=failedLimitProcessingTime while the 137KB demo passed in ~3s). These
tests pin the guard: the submit body always names the limit, the env override
works, and the clamp keeps the value inside `_poll_workitem`'s 900s budget so
the submit can never abandon a WorkItem APS still finishes and bills.

Pure-python: dry_run returns before any network call, no APS, no credential.

  cd C:/tmp/leaf-web-demo/da && python -m pytest test_workitem_limits.py -q
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import client  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("APS_WORKITEM_PROCESSING_LIMIT_S", raising=False)


def test_dry_run_body_carries_default_limit():
    out = client.submit_workitem("Owner.Activity+prod", {"HostDwg": {}}, dry_run=True)
    assert out["body"]["limitProcessingTimeSec"] == 600


def test_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("APS_WORKITEM_PROCESSING_LIMIT_S", "300")
    out = client.submit_workitem("Owner.Activity+prod", {"HostDwg": {}}, dry_run=True)
    assert out["body"]["limitProcessingTimeSec"] == 300


@pytest.mark.parametrize("raw,expected", [
    ("30", 60),        # floor: below 60 is clamped up
    ("100000", 900),   # ceiling: never exceeds the 900s poll budget
    ("not-a-number", 600),  # garbage falls back to the default
])
def test_limit_is_clamped_to_the_poll_budget(monkeypatch, raw, expected):
    monkeypatch.setenv("APS_WORKITEM_PROCESSING_LIMIT_S", raw)
    assert client.workitem_processing_limit_s() == expected
