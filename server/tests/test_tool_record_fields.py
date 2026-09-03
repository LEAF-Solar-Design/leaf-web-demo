"""Optional tool-record fields: `icon`, `placement`, and a PERSISTED `family_id`.

Standardization slice 3. The record now carries how it wants to be shown, so
the ribbon and the slash picker stop hardcoding one glyph for every tool, and a
renamed tool keeps its family instead of silently falling into "custom".

Acceptance covered here:
  * PASS-THROUGH: a valid icon/placement survives catalog._capability_entry
    (the single projection point) into build_catalog's family output, and a
    record that declares neither produces exactly today's entry — no new keys.
  * DROP-WITH-WARNING: an INVALID optional field read from a fold tier is
    dropped and logged once, naming the tool. One bad tool never breaks the
    catalog for the rest.
  * 422: the authoring API has a caller to tell, so the SAME validator rejects
    there, with a message that names the field.
  * PERSISTED family_id: a stamped family survives a rename that
    capability_families.json's name map knows nothing about, and the unstamped
    fallback warns once per tool per process instead of dropping silently.

Run:  cd server && python -m pytest tests/test_tool_record_fields.py -q
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import catalog  # noqa: E402
import tool_record_fields  # noqa: E402
from routers import author as author_router  # noqa: E402


def _tool(**over):
    base = {
        "name": "count-by-layer",
        "version": "1.0.0",
        "description": "Counts entities per layer.",
        "kind": "script",
        "engine_op": "count_by_layer",
        "params": {"type": "object", "properties": {}},
        "capabilities": ["drawing.read"],
        "provenance": {"author": "agent"},
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _fresh_warn_ledger():
    tool_record_fields.reset_warn_ledger()
    yield
    tool_record_fields.reset_warn_ledger()


# --------------------------------------------------------------------------- #
# A1 pass-through
# --------------------------------------------------------------------------- #
def test_valid_icon_and_placement_reach_the_capability_entry():
    entry = catalog._capability_entry(
        _tool(icon="layers", placement={"tab": "draw", "size": "small"}))
    assert entry["icon"] == "layers"
    assert entry["placement"] == {"tab": "draw", "size": "small"}


def test_build_catalog_carries_the_fields_into_the_family_output():
    families = catalog.build_catalog([_tool(icon="layers", placement={"tab": "view"})])
    measurement = next(f for f in families if f["family_id"] == "measurement")
    entry = next(c for c in measurement["capabilities"] if c["name"] == "count-by-layer")
    assert entry["icon"] == "layers"
    assert entry["placement"] == {"tab": "view"}


def test_a_record_that_declares_nothing_gets_no_new_keys():
    """Backwards compatibility as an EQUALITY, not a promise."""
    entry = catalog._capability_entry(_tool())
    assert "icon" not in entry
    assert "placement" not in entry


# --------------------------------------------------------------------------- #
# A1 drop-with-warning on a fold tier
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_icon", [
    "Layers",            # uppercase
    "-leading-dash",     # must start alphanumeric
    "with space",
    "x" * 41,            # over the bound
    42,
    ["layers"],
])
def test_an_invalid_icon_is_dropped_not_raised(bad_icon, caplog):
    with caplog.at_level(logging.WARNING, logger="tool_record_fields"):
        entry = catalog._capability_entry(_tool(icon=bad_icon))
    assert "icon" not in entry
    assert entry["name"] == "count-by-layer"
    assert any("count-by-layer" in rec.getMessage() for rec in caplog.records)


@pytest.mark.parametrize("bad_placement", [
    {"tab": "model"},            # declared in RIBBON_TABS but never selectable
    {"tab": "nope"},
    {"size": "huge"},
    {"tab": "draw", "colour": "red"},   # unknown key, never silently ignored
    {},
    "draw",
    7,
])
def test_an_invalid_placement_is_dropped_not_raised(bad_placement, caplog):
    with caplog.at_level(logging.WARNING, logger="tool_record_fields"):
        entry = catalog._capability_entry(_tool(placement=bad_placement))
    assert "placement" not in entry


def test_one_bad_tool_never_breaks_the_rest_of_the_catalog():
    families = catalog.build_catalog([
        _tool(icon="!!bad!!"),
        _tool(name="measure-panel-area", engine_op="measure_area", icon="dimension"),
    ])
    measurement = next(f for f in families if f["family_id"] == "measurement")
    by_name = {c["name"]: c for c in measurement["capabilities"]}
    assert "icon" not in by_name["count-by-layer"]
    assert by_name["measure-panel-area"]["icon"] == "dimension"


def test_the_drop_warning_fires_once_per_tool_per_process(caplog):
    with caplog.at_level(logging.WARNING, logger="tool_record_fields"):
        for _ in range(5):
            catalog._capability_entry(_tool(icon="!!bad!!"))
    drops = [r for r in caplog.records if "tool_record_field_dropped" in r.getMessage()]
    assert len(drops) == 1


def test_the_warn_ledger_is_bounded():
    limit = tool_record_fields.WARN_LEDGER_MAX
    for index in range(limit + 50):
        tool_record_fields.sanitize_optional_fields({"name": f"t{index}", "icon": "!!"})
    assert len(tool_record_fields._WARNED) == limit


# --------------------------------------------------------------------------- #
# A1 the authoring API rejects instead of dropping
# --------------------------------------------------------------------------- #
def _author_call(**over):
    payload = {"description": "count the panels"}
    payload.update(over)
    return author_router.author(author_router.AuthorRequest(**payload), tenant="demo-tenant")


def test_the_authoring_api_rejects_a_bad_icon_with_422_naming_the_field():
    response = _author_call(icon="Not A Key")
    assert response.status_code == 422
    body = json.loads(response.body)
    assert body["invalid_field"] == "icon"
    assert "sprite key" in body["error"]["message"]
    assert body["tool"] is None


def test_the_authoring_api_rejects_a_bad_placement_tab_with_the_allowed_set():
    response = _author_call(placement={"tab": "model"})
    assert response.status_code == 422
    body = json.loads(response.body)
    assert body["invalid_field"] == "placement"
    assert "placement.tab must be one of" in body["error"]["message"]
    for tab in ("draw", "insert", "annotate", "view", "manage"):
        assert tab in body["error"]["message"]


def test_the_authoring_api_rejects_an_unknown_placement_key():
    response = _author_call(placement={"tab": "draw", "size": "small", "row": 2})
    assert response.status_code == 422
    body = json.loads(response.body)
    assert "unknown key(s): row" in body["error"]["message"]


def test_the_authoring_api_rejects_an_over_long_icon_at_the_model_boundary():
    with pytest.raises(Exception):
        author_router.AuthorRequest(description="x", icon="a" * 200)


def test_validate_accepts_what_sanitize_accepts():
    fields = {"icon": "toolbox", "placement": {"tab": "manage", "size": "row"}}
    assert tool_record_fields.validate_optional_fields(fields) == fields
    assert tool_record_fields.sanitize_optional_fields({"name": "t", **fields}) == fields


# --------------------------------------------------------------------------- #
# A2 persisted family_id
# --------------------------------------------------------------------------- #
def test_family_for_persist_answers_the_name_map_and_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="tool_record_fields"):
        assert catalog.family_for_persist(_tool()) == "measurement"
        assert catalog.family_for_persist(_tool(name="brand-new", engine_op="brand_new")) \
            == catalog.DEFAULT_FAMILY
    assert [r for r in caplog.records if "tool_family_fallback" in r.getMessage()] == []


def test_a_stamped_family_survives_a_rename():
    """The rename trap: capability_families.json knows the OLD name only."""
    tool = _tool()
    stamped = tool_record_fields.family_id_for_persist(
        tool, catalog.family_for_persist(tool))
    assert stamped == "measurement"
    tool["family_id"] = stamped
    tool["name"] = "count-entities-per-layer"   # renamed, map not updated
    tool["engine_op"] = "count_entities_per_layer"
    families = catalog.build_catalog([tool])
    measurement = next(f for f in families if f["family_id"] == "measurement")
    assert [c["name"] for c in measurement["capabilities"]] == ["count-entities-per-layer"]
    assert not any(f["family_id"] == catalog.DEFAULT_FAMILY for f in families)


def test_without_the_stamp_the_same_rename_falls_to_custom_and_warns_once(caplog):
    renamed = _tool(name="count-entities-per-layer", engine_op="count_entities_per_layer")
    with caplog.at_level(logging.WARNING, logger="tool_record_fields"):
        for _ in range(3):
            catalog.build_catalog([dict(renamed)])
    families = catalog.build_catalog([renamed])
    assert any(f["family_id"] == catalog.DEFAULT_FAMILY for f in families)
    fallbacks = [r for r in caplog.records if "tool_family_fallback" in r.getMessage()]
    assert len(fallbacks) == 1
    assert "count-entities-per-layer" in fallbacks[0].getMessage()


def test_an_explicit_family_id_always_wins_over_the_resolver():
    tool = _tool(family_id="stringing")
    assert tool_record_fields.family_id_for_persist(tool, "measurement") == "stringing"


def test_an_authored_row_never_emits_the_fallback_warning(caplog):
    """Authored rows are stamped at publish time; a legacy one is not the drop
    this warning exists for."""
    authored = _tool(name="my-authored-tool", engine_op="", tenant_id="demo-tenant")
    with caplog.at_level(logging.WARNING, logger="tool_record_fields"):
        catalog.build_catalog([authored])
    assert [r for r in caplog.records if "tool_family_fallback" in r.getMessage()] == []


def test_the_ribbon_tab_set_matches_what_the_web_declares():
    """ONE definition server-side; the web's RIBBON_TABS is the named source."""
    band = (SERVER_DIR.parent / "web" / "src" / "site" / "CockpitTopBand.jsx").read_text(
        encoding="utf-8")
    head = "RIBBON_TABS = Object.freeze(["
    start = band.index(head) + len(head)
    block = band[start:band.index("])", start)]
    declared = set()
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{ id: '"):
            continue
        # A tab carrying a `reason` is declared but never selectable.
        if "reason:" in stripped:
            continue
        declared.add(stripped.split("{ id: '", 1)[1].split("'", 1)[0])
    assert declared, "RIBBON_TABS block not parsed"
    assert declared == set(tool_record_fields.RIBBON_TAB_IDS)
