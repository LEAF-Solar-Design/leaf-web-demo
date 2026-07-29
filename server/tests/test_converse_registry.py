"""Binary acceptance for server/converse_registry.py and GET /api/converse/registry.

The picker is a trust surface: everything it lists, the user expects to be able
to run. So the properties under test are (a) every entry carries a
`client_action` the UI can dispatch on — no dead affordances — (b) tools are
projected to name/description only, so catalog internals cannot leak into a
menu response, and (c) a catalog outage degrades to commands rather than
failing the whole menu, because `/stop` must keep working when the catalog is
down.

Run:  cd server && python -m pytest tests/test_converse_registry.py -q
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import converse_registry  # noqa: E402


# --------------------------------------------------------------------------- #
# pure builder
# --------------------------------------------------------------------------- #
def test_every_entry_carries_a_dispatchable_client_action():
    """The anti-dead-affordance invariant: nothing is listed that the client
    cannot execute."""
    registry = converse_registry.build_registry(
        tools=[{"name": "count_panels", "description": "count them"}],
        skills=[{"name": "orwell-writing", "description": "tighten prose"}],
    )
    assert registry["entries"], "registry was empty"
    for entry in registry["entries"]:
        assert entry.get("client_action"), f"no client_action on {entry}"
        assert entry.get("kind") in converse_registry.KINDS, entry
        assert isinstance(entry.get("name"), str) and entry["name"].strip(), entry


def test_entries_arrive_in_group_order_with_counts():
    registry = converse_registry.build_registry(
        tools=[{"name": "t1"}, {"name": "t2"}],
        skills=[{"name": "s1"}],
    )
    kinds = [e["kind"] for e in registry["entries"]]
    # command(s) first, then skills, then tools — the same order the client's
    # rankEntries uses to break ties.
    assert kinds == sorted(kinds, key=lambda k: converse_registry.KINDS.index(k)), kinds
    assert registry["counts"]["skill"] == 1
    assert registry["counts"]["tool"] == 2
    assert registry["counts"]["command"] == len(converse_registry.PLATFORM_COMMANDS)


def test_tools_are_projected_not_copied():
    """A catalog row carries pins, digests and repo paths. The picker needs a
    label and a subtitle; anything else would leak through a menu response."""
    entries = converse_registry.tool_entries([{
        "name": "count_panels",
        "description": "count them",
        "catalog_digest": "sha256:secretish",
        "repo_path": "/srv/tenant-a/tools/count_panels.py",
        "internal": True,
    }])
    assert entries == [{
        "kind": "tool",
        "name": "count_panels",
        "description": "count them",
        "client_action": "run_tool",
    }]


def test_malformed_rows_are_dropped_not_rendered_blank():
    entries = converse_registry.tool_entries(
        [None, "nope", {}, {"name": ""}, {"name": "   "}, {"name": "ok"}])
    assert [e["name"] for e in entries] == ["ok"]
    assert converse_registry.tool_entries(None) == []
    assert converse_registry.skill_entries(None) == []


def test_description_is_always_a_string():
    """The picker renders it directly; None would print as "None"."""
    entries = converse_registry.tool_entries([{"name": "t", "description": None}])
    assert entries[0]["description"] == ""


def test_command_list_cannot_be_mutated_through_the_accessor():
    first = converse_registry.command_entries()
    first[0]["name"] = "clobbered"
    assert converse_registry.command_entries()[0]["name"] != "clobbered"


def test_stop_command_is_present_because_interrupt_shipped():
    names = {e["name"] for e in converse_registry.command_entries()}
    assert "stop" in names and "help" in names


# --------------------------------------------------------------------------- #
# route
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    import app as app_module
    return TestClient(app_module.app)


def test_route_returns_grouped_entries_for_the_tenant(client, monkeypatch):
    import deps
    monkeypatch.setattr(deps, "all_tools", lambda tenant: [
        {"name": "count_panels", "description": "count them"},
    ])
    res = client.get("/api/converse/registry")
    assert res.status_code == 200, res.text
    body = res.json()
    names = [e["name"] for e in body["entries"]]
    assert "count_panels" in names
    assert "stop" in names
    assert body["counts"]["tool"] == 1


def test_route_degrades_to_commands_when_the_catalog_fails(client, monkeypatch):
    """`/stop` must keep working when the catalog is down — the composer is how
    a user interrupts a runaway turn."""
    import deps

    def _boom(tenant):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(deps, "all_tools", _boom)
    res = client.get("/api/converse/registry")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["counts"]["tool"] == 0
    assert "stop" in [e["name"] for e in body["entries"]]
