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
        ' "t-corrupt": "not-a-record", "t-nullrec": null}')
    for tid in ("t-null", "t-zero", "t-empty", "t-str", "t-corrupt", "t-nullrec"):
        assert broker.tenant_disabled(tid) is True, tid


def test_present_null_record_is_corrupt_not_absent(tmp_path, monkeypatch):
    """`_tenants.get(tid)` returns None for BOTH an absent tenant and a present
    `{"victim": null}` — so a null RECORD (not just a null flag) slipped through
    as not-killed. A MISSING sentinel separates the two: absent enables, a
    present null is corrupt and fails CLOSED. Tier already resolved restricted,
    but /broker/extract has no tier gate, so the kill flag is the real guard."""
    broker = _broker_with_tenants(tmp_path, monkeypatch, '{"victim": null}')
    assert broker.tenant_disabled("victim") is True
    assert broker.tenant_disabled("never-provisioned") is False


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


# --------------------------------------------------------------------------- #
# the operational kill-state view (/broker/health) must AGREE with the guard
# --------------------------------------------------------------------------- #
def test_health_disabled_list_agrees_with_the_kill_switch(tmp_path, monkeypatch):
    """/broker/health is the documented authoritative view of who is killed, so
    it must route through tenant_disabled — not a second `v.get("disabled")`
    scan that reported a null flag as ENABLED and crashed (AttributeError) on a
    non-dict record, disagreeing with the guard that actually blocks runs."""
    broker = _broker_with_tenants(
        tmp_path, monkeypatch,
        '{"t-on": {"disabled": true}, "t-off": {"disabled": false},'
        ' "t-null": {"disabled": null}, "t-badrec": null, "t-corrupt": ["x"]}')
    listed = set(broker.health()["tenants_disabled"])
    # every tenant the guard kills appears; none it permits does; no crash
    for tid in ("t-on", "t-null", "t-badrec", "t-corrupt"):
        assert tid in listed and broker.tenant_disabled(tid) is True, tid
    assert "t-off" not in listed and broker.tenant_disabled("t-off") is False


def test_ops_write_refuses_a_corrupt_record_instead_of_typeerror(tmp_path, monkeypatch):
    """set_tenant_disabled used setdefault(tid, {}), which returns a present
    corrupt record unchanged and then raises a bare TypeError on item-assign.
    Refuse cleanly (BrokerStateError) rather than 500, and never log a kill as
    applied while the on-disk record stays corrupt. Absent + valid still work."""
    broker = _broker_with_tenants(
        tmp_path, monkeypatch, '{"t-bad": ["garbage"], "t-ok": {"disabled": false}}')
    with pytest.raises(broker.BrokerStateError):
        broker.set_tenant_disabled("t-bad", True)
    # a real record and a brand-new tenant both write fine
    broker.set_tenant_disabled("t-ok", True)
    assert broker.tenant_disabled("t-ok") is True
    broker.set_tenant_disabled("t-fresh", True)
    assert broker.tenant_disabled("t-fresh") is True
