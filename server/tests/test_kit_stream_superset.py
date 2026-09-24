"""Freeze the Studio stream's total projection onto the pinned Kit contract."""
import json
from pathlib import Path

import pytest

from test_contract_freeze import SUBSCRIBABLE_STREAM_TYPES, _web_stream_event_types
from kit_stream_map import (
    EXTENSIONS, KIT_SCHEMA_RELPATH, NATIVE, KitProjection,
    load_kit_vocabulary, unresolved,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_studio_stream_projection_is_total():
    assert set(NATIVE).isdisjoint(EXTENSIONS)
    assert set(NATIVE) | set(EXTENSIONS) == SUBSCRIBABLE_STREAM_TYPES
    assert set(NATIVE) | set(EXTENSIONS) == _web_stream_event_types()


def test_native_projections_resolve_against_pinned_kit():
    assert unresolved(NATIVE, load_kit_vocabulary(REPO_ROOT / KIT_SCHEMA_RELPATH)) == []


def test_extensions_are_named_and_frozen():
    vocab = load_kit_vocabulary(REPO_ROOT / KIT_SCHEMA_RELPATH)
    assert set(EXTENSIONS).isdisjoint(vocab["events"])
    assert all(reason.strip() and len(reason) <= 200 for reason in EXTENSIONS.values())
    assert set(EXTENSIONS) == {
        "job_linked", "question_required", "turn_usage", "session_state",
        "overlay_proposed", "overlay_decided", "overlay_revoked",
    }
    assert len(NATIVE) == 11


def test_projection_gate_can_fail():
    vocab = load_kit_vocabulary(REPO_ROOT / KIT_SCHEMA_RELPATH)
    assert unresolved({"question": KitProjection("item", "server", "question")}, vocab)
    assert unresolved({"paused": KitProjection("turn", states=("paused",))}, vocab)


def test_missing_thread_event_schema_fails_closed(tmp_path):
    schema = tmp_path / "missing.json"
    schema.write_text('{"$defs": {}}')
    with pytest.raises(ValueError, match="missing.json"):
        load_kit_vocabulary(schema)


def test_schema_path_matches_vendor_pin():
    pin = json.loads((REPO_ROOT / "server/_vendor/VENDOR-PIN.json").read_text())
    assert KIT_SCHEMA_RELPATH == pin["contract_files"]["assistant.schema.json"]["path"]
