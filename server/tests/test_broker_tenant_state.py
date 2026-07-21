"""
Binary acceptance for broker tenant-state trust (server/broker.py).

The broker is the sole APS-credential holder and the home of the tenant kill
switch, so its persisted tenant records decide two things that must never fail
open: whether a tenant is KILLED, and which TIER it runs at.

Contracts under test:
  * an ABSENT tenants file is safe-empty (first boot, nothing provisioned);
  * a PRESENT-but-corrupt one refuses to load rather than collapsing to {} —
    _load_tenants runs ONCE at import, so collapsing silently disarmed every
    kill flag for the whole process lifetime and, with no record left to carry a
    tier, promoted every tenant to the friction-free `demo` default;
  * the kill flag is a REAL boolean: a present-but-falsy value (null/0/"") used
    to read as ENABLED, which is the one direction a kill switch may not fail;
  * a corrupt tenant record resolves `restricted`, not `demo`.

Run:  cd server && python -m pytest tests/test_broker_tenant_state.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE PROJECT_ROOT lands on sys.path
# (repo-root platform/ package shadows it; mirrors test_wave4/5).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import importlib  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


def _broker_with_tenants(tmp_path, monkeypatch, body: str | None):
    """Import broker fresh against a tenants file with `body` (None = absent)."""
    p = tmp_path / "broker_tenants.json"
    if body is not None:
        p.write_text(body, encoding="utf-8")
    monkeypatch.setenv("BROKER_TENANTS", str(p))
    monkeypatch.setenv("BROKER_LEDGER", str(tmp_path / "ledger.jsonl"))
    sys.modules.pop("broker", None)
    return importlib.import_module("broker")


# --------------------------------------------------------------------------- #
# file-level trust
# --------------------------------------------------------------------------- #
def test_absent_tenants_file_is_safe_empty(tmp_path, monkeypatch):
    broker = _broker_with_tenants(tmp_path, monkeypatch, None)
    assert broker._load_tenants() == {}
    assert broker.tenant_disabled("anyone") is False


@pytest.mark.parametrize("corrupt", ['{truncated', '["not","a","mapping"]', 'null', '7'])
def test_present_corrupt_tenants_file_refuses_to_load(tmp_path, monkeypatch, corrupt):
    """Collapsing to {} disarmed every kill flag AND promoted every tenant to
    demo. Refusing to start beats starting with the kill switch gone."""
    p = tmp_path / "broker_tenants.json"
    p.write_text(corrupt, encoding="utf-8")
    monkeypatch.setenv("BROKER_TENANTS", str(p))
    monkeypatch.setenv("BROKER_LEDGER", str(tmp_path / "ledger.jsonl"))
    sys.modules.pop("broker", None)
    with pytest.raises(Exception) as exc:  # BrokerStateError, raised at import
        importlib.import_module("broker")
    assert "tenants" in str(exc.value).lower()
    sys.modules.pop("broker", None)


# --------------------------------------------------------------------------- #
# the kill flag itself
# --------------------------------------------------------------------------- #
def test_present_falsy_kill_flag_does_not_enable_the_tenant(tmp_path, monkeypatch):
    """`bool(rec.get("disabled"))` read null/0/"" as ENABLED — the one direction
    a kill switch must never fail."""
    broker = _broker_with_tenants(
        tmp_path, monkeypatch,
        '{"t-null": {"disabled": null}, "t-zero": {"disabled": 0},'
        ' "t-empty": {"disabled": ""}, "t-str": {"disabled": "false"},'
        ' "t-corrupt": "not-a-record"}')
    for tid in ("t-null", "t-zero", "t-empty", "t-str", "t-corrupt"):
        assert broker.tenant_disabled(tid) is True, tid


def test_real_booleans_and_absence_still_behave(tmp_path, monkeypatch):
    """Strictness must not over-tighten: a real False, an absent flag, and an
    unprovisioned tenant are all legitimately NOT killed."""
    broker = _broker_with_tenants(
        tmp_path, monkeypatch,
        '{"t-off": {"disabled": false}, "t-on": {"disabled": true},'
        ' "t-noflag": {"tier": "hosted_pro"}}')
    assert broker.tenant_disabled("t-off") is False
    assert broker.tenant_disabled("t-on") is True
    assert broker.tenant_disabled("t-noflag") is False
    assert broker.tenant_disabled("never-seen") is False


# --------------------------------------------------------------------------- #
# tier resolution
# --------------------------------------------------------------------------- #
def test_corrupt_tenant_record_resolves_restricted_not_demo(tmp_path, monkeypatch):
    broker = _broker_with_tenants(
        tmp_path, monkeypatch, '{"t-corrupt": ["garbage"], "t-real": {"tier": "hosted_pro"}}')
    import entitlements
    assert broker._provisioned_tier("t-corrupt") == entitlements.RESTRICTED_TIER
    assert broker._tenant_tier("t-corrupt") == entitlements.RESTRICTED_TIER
    # a real record still wins, and a genuinely unprovisioned tenant keeps the
    # documented friction-free demo default
    assert broker._provisioned_tier("t-real") == "hosted_pro"
    assert broker._tenant_tier("never-seen") == entitlements.DEFAULT_TIER
