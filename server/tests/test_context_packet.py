"""
Binary acceptance for server/context_packet.py (agent spine §18, wire contract §4).

The ContextPacket is the app-assembled, PURE-READ grounding snapshot one
conversational turn ships to the harness. These tests pin its size disciplines:

  * catalog cap 60 + trailing {"more": N} marker (61 tools -> 60 + more:1);
  * serialized packet < 8000 chars, catalog truncated FIRST;
  * an oversized NON-catalog field raises ValueError loudly (mapped to
    BAD_PARAMS at the route — never an AssertionError 500);
  * drawing.layers is capped top-N by count with a {"more": N} marker;
  * entitlement booleans present (tier-driven, converse included);
  * grant projection carries {kind, degraded} ONLY — token material can never
    reach the packet even from a hostile/buggy harness status body;
  * building a packet never writes to the drawing store (no demo bootstrap).

Hermetic: no network (the harness grant read is monkeypatched), throwaway
JOBS_DB + LEAF_STORE_DIR.

Run:  cd server && python -m pytest tests/test_context_packet.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE PROJECT_ROOT lands on sys.path (mirrors wave4/5).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Dict, List  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# route the jobs SQLite DB to a throwaway dir BEFORE `jobs` imports anywhere.
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="cp-jobs-")) / "jobs.db"))

import context_packet  # noqa: E402
import deps  # noqa: E402

TENANT = "packet-tenant"
FAKE_TOKEN = "sk-ant-api03-FAKE-must-never-appear-in-a-packet"


def _tools(n: int, desc: str = "counts panels per layer") -> List[Dict[str, Any]]:
    return [{"name": f"tool-{i:03d}", "version": "1.0.0", "description": desc,
             "capabilities": ["drawing.read"]} for i in range(n)]


@pytest.fixture(autouse=True)
def hermetic(monkeypatch, tmp_path):
    """No harness env (grant read degrades, no network); throwaway drawing store."""
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    monkeypatch.delenv("LEAF_CONVERSE_HARNESS_URL", raising=False)
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    yield


def _build(monkeypatch, tools: List[Dict[str, Any]], **kwargs) -> Dict[str, Any]:
    monkeypatch.setattr(deps, "all_tools", lambda tid=None: list(tools))
    return context_packet.build_packet(TENANT, "demo", **kwargs)


# --------------------------------------------------------------------------- #
# catalog cap + more marker
# --------------------------------------------------------------------------- #
def test_catalog_cap_61_tools_gives_60_plus_more(monkeypatch):
    p = _build(monkeypatch, _tools(61))
    cat = p["catalog"]
    assert len(cat) == 61  # 60 entries + the {"more": N} marker
    assert cat[-1] == {"more": 1}
    for entry in cat[:-1]:
        assert set(entry) == {"name", "description", "capabilities"}
    assert cat[0]["name"] == "tool-000" and cat[59]["name"] == "tool-059"


def test_catalog_under_cap_has_no_more_marker(monkeypatch):
    p = _build(monkeypatch, _tools(5))
    assert len(p["catalog"]) == 5
    assert all("more" not in e for e in p["catalog"])


def test_catalog_hash_covers_full_catalog_not_the_cap(monkeypatch):
    h61 = _build(monkeypatch, _tools(61))["catalog_hash"]
    h62 = _build(monkeypatch, _tools(62))["catalog_hash"]
    assert h61.startswith("sha256:") and h61 != h62  # tool 61 is capped out yet changes the hash


def test_internal_tools_filtered_from_catalog(monkeypatch):
    tools = _tools(3) + [{"name": "qa-probe", "version": "1.0.0", "description": "internal",
                          "capabilities": [], "internal": True}]
    p = _build(monkeypatch, tools)
    assert all(e["name"] != "qa-probe" for e in p["catalog"])


# --------------------------------------------------------------------------- #
# 8000-char hard cap
# --------------------------------------------------------------------------- #
def test_hard_cap_truncates_catalog_first(monkeypatch):
    # 200 verbose tools would blow the cap; the catalog must give way, nothing else.
    p = _build(monkeypatch, _tools(200, desc="x" * 139))
    raw = json.dumps(p, separators=(",", ":"))
    assert len(raw) < context_packet.MAX_PACKET_CHARS
    cat = p["catalog"]
    assert cat and isinstance(cat[-1].get("more"), int)
    kept = len(cat) - 1
    assert kept < 60  # truncated below the nominal cap to fit
    assert cat[-1]["more"] == 200 - kept
    # non-catalog fields survived the squeeze
    assert "entitlements" in p and "drawing" in p and "grant" in p


def test_oversized_non_catalog_field_fails_loudly(monkeypatch):
    hint = {"lane": "run", "tool": None, "confidence": 0.1,
            "rationale": "y" * (context_packet.MAX_PACKET_CHARS + 100)}
    # ValueError, not AssertionError: survives `python -O` and maps to BAD_PARAMS
    with pytest.raises(ValueError, match="non-catalog field is oversized"):
        _build(monkeypatch, _tools(3), classifier_hint=hint)


def test_classifier_hint_passthrough(monkeypatch):
    hint = {"lane": "run", "tool": "tool-001", "confidence": 0.62, "rationale": "name match"}
    p = _build(monkeypatch, _tools(3), classifier_hint=hint)
    assert p["classifier_hint"] == hint
    assert _build(monkeypatch, _tools(3))["classifier_hint"] is None


# --------------------------------------------------------------------------- #
# entitlements
# --------------------------------------------------------------------------- #
def test_entitlement_booleans_present(monkeypatch):
    p = _build(monkeypatch, _tools(3))
    ents = p["entitlements"]
    for cap in ("run_read", "run_write", "build", "converse"):
        assert cap in ents, f"missing entitlement flag {cap!r}"
    assert all(isinstance(v, bool) for v in ents.values())
    # plain-str tenant => tier "demo" => converse granted (§9 policy)
    assert ents["converse"] is True


# --------------------------------------------------------------------------- #
# grant projection — token material can never enter the packet
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, body: Dict[str, Any]):
        self._body = body

    def json(self):
        return self._body


def test_grant_projection_never_carries_token(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.test")
    # hostile/buggy harness status body that leaks the token: the packet must not.
    leaky = {"linked": True, "kind": "api_key", "token": FAKE_TOKEN, "linked_at": "2026-07-20"}
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(leaky))
    p = _build(monkeypatch, _tools(3))
    assert p["grant"] == {"kind": "api_key", "degraded": False}
    raw = json.dumps(p)
    assert FAKE_TOKEN not in raw and "linked_at" not in json.dumps(p["grant"])


def test_grant_missing_when_unlinked(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.test")
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp({"linked": False}))
    assert _build(monkeypatch, _tools(3))["grant"] == {"kind": "missing", "degraded": False}


def test_grant_degraded_when_harness_unconfigured(monkeypatch):
    # autouse fixture cleared both harness env vars -> grant store unconsultable.
    assert _build(monkeypatch, _tools(3))["grant"] == {"kind": "missing", "degraded": True}


def test_grant_degraded_when_harness_unreachable(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.test")

    def _boom(*a, **k):
        raise ConnectionError("refused")
    monkeypatch.setattr("requests.get", _boom)
    assert _build(monkeypatch, _tools(3))["grant"] == {"kind": "missing", "degraded": True}


# --------------------------------------------------------------------------- #
# pure reads + drawing digest
# --------------------------------------------------------------------------- #
def test_pure_reads_unknown_drawing_no_store_writes(monkeypatch, tmp_path):
    store_dir = tmp_path / "store"
    monkeypatch.setenv("LEAF_STORE_DIR", str(store_dir))
    p = _build(monkeypatch, _tools(3))
    # empty digest, and CRUCIALLY the store was not bootstrapped (no writes)
    assert p["drawing"] == {"id": "demo", "head_version": None, "layers": [], "entity_total": 0}
    assert p["versions"] == []
    assert p["checkout"] == {"held_by": None, "expires_at": None}
    assert not store_dir.exists() or not any(store_dir.rglob("*"))


def test_drawing_digest_versions_tail_and_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    import write_loop
    backend = write_loop.default_backend()
    write_loop.ensure_demo_drawing(backend, TENANT, "demo")  # explicit TEST setup write

    p = _build(monkeypatch, _tools(3))
    d = p["drawing"]
    assert d["id"] == "demo" and d["head_version"] == 1
    assert d["entity_total"] > 0
    assert d["layers"] and all(set(l) == {"name", "count"} for l in d["layers"])
    assert sum(l["count"] for l in d["layers"]) == d["entity_total"]
    assert len(p["versions"]) == 1  # tail of a 1-version chain
    assert set(p["versions"][0]) == {"version", "ts", "tool"}
    assert p["checkout"] == {"held_by": None, "expires_at": None}

    # a held checkout surfaces as the {held_by, expires_at} line
    import store
    assert store.acquire_checkout(backend, TENANT, "demo", "alice", 600)
    p2 = _build(monkeypatch, _tools(3))
    assert p2["checkout"]["held_by"] == "alice"
    assert p2["checkout"]["expires_at"]


def test_drawing_layers_capped_top_n_by_count(monkeypatch, tmp_path):
    """drawing.layers is unbounded (one entry per distinct layer) — it must cap
    at LAYERS_CAP top-count entries with the catalog-style {"more": N} marker."""
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    import write_loop
    backend = write_loop.default_backend()
    write_loop.ensure_demo_drawing(backend, TENANT, "demo")  # explicit TEST setup write
    n_layers = context_packet.LAYERS_CAP + 5
    fake_intake = {"polylines": [{"layer": f"L-{i:03d}"}
                                 for i in range(n_layers) for _ in range(i + 1)],
                   "inserts": [], "faces3d": []}
    monkeypatch.setattr(write_loop, "read_intake", lambda *a, **k: (1, fake_intake))

    p = _build(monkeypatch, _tools(3))
    rows = p["drawing"]["layers"]
    assert len(rows) == context_packet.LAYERS_CAP + 1  # capped entries + marker
    assert rows[-1] == {"more": 5}
    counts = [r["count"] for r in rows[:-1]]
    assert counts == sorted(counts, reverse=True)  # top-N by count
    # entity_total still counts EVERYTHING, capped list or not
    assert p["drawing"]["entity_total"] == sum(i + 1 for i in range(n_layers))


def test_active_jobs_list_present_and_capped_shape(monkeypatch):
    p = _build(monkeypatch, _tools(3))
    assert isinstance(p["active_jobs"], list)
    assert len(p["active_jobs"]) <= context_packet.ACTIVE_JOBS_CAP


# --------------------------------------------------------------------------- #
# script runner
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
