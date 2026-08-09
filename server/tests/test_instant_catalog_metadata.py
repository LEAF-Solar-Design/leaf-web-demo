import copy
import hashlib
from pathlib import Path

import pytest

import catalog
import deps


def _instant_tool():
    return {
        "name": "instant-read",
        "version": "1.0.0",
        "description": "Read a preloaded drawing context.",
        "kind": "script",
        "engine_op": "instant_read",
        "params": {"type": "object", "properties": {}},
        "capabilities": ["drawing.read"],
        "execution_class": "instant",
        "runtime": "cpython-3.13-restricted-v1",
        "limits": {
            "cpu_ms": 25,
            "wall_ms": 100,
            "memory_bytes": 67_108_864,
            "output_bytes": 65_536,
        },
        "artifact_digest": "sha256:" + ("a" * 64),
        "batch_fallback": True,
    }


def test_capability_projection_exposes_authoritative_execution_metadata():
    entry = catalog.build_catalog([_instant_tool()])[0]["capabilities"][0]

    assert entry["execution_class"] == "instant"
    assert entry["runtime"] == "cpython-3.13-restricted-v1"
    assert entry["limits"]["wall_ms"] == 100
    assert entry["artifact_digest"] == "sha256:" + ("a" * 64)
    assert entry["batch_fallback"] is True


def test_capability_projection_requires_static_and_runtime_aps_live_authority():
    tool = _instant_tool()
    tool["aps_live"] = True
    view = deps.catalog_tool_view(tool)

    authorized = catalog.apply_live_aps_runtime_authority(
        [view],
        aps_live_enabled=True,
        trusted_live_catalog_digests={deps.catalog_tool_digest(tool)},
        tool_sources=["operator_owned_engine"],
        operator_owned_engine_source="operator_owned_engine",
    )
    entry = catalog.build_catalog(authorized)[0]["capabilities"][0]

    assert entry["aps_live"] is True


def test_raw_registry_live_aps_metadata_cannot_authorize_projection():
    tool = _instant_tool()
    tool["aps_live"] = True

    entry = catalog.build_catalog([tool])[0]["capabilities"][0]

    assert entry["aps_live"] is False


@pytest.mark.parametrize(
    ("source_value", "runtime_value"),
    [
        (True, False),
        (True, None),
        (True, "true"),
        (True, 1),
        (False, True),
        (None, True),
        ("true", True),
        (1, True),
        ([], True),
        ({}, True),
    ],
)
def test_capability_projection_fails_closed_without_both_exact_authorities(
    source_value, runtime_value
):
    trusted_tool = _instant_tool()
    trusted_tool["aps_live"] = True
    tool = _instant_tool()
    if source_value is not None:
        tool["aps_live"] = source_value
    view = deps.catalog_tool_view(tool)

    authorized = catalog.apply_live_aps_runtime_authority(
        [view],
        aps_live_enabled=runtime_value,
        trusted_live_catalog_digests={deps.catalog_tool_digest(trusted_tool)},
        tool_sources=["operator_owned_engine"],
        operator_owned_engine_source="operator_owned_engine",
    )
    entry = catalog.build_catalog(authorized)[0]["capabilities"][0]

    assert entry["aps_live"] is False


def test_legacy_tool_defaults_to_batch_without_an_implicit_fallback():
    tool = _instant_tool()
    for field in (
        "execution_class", "runtime", "limits", "artifact_digest", "batch_fallback"
    ):
        tool.pop(field, None)

    entry = catalog.build_catalog([tool])[0]["capabilities"][0]

    assert entry["execution_class"] == "batch"
    assert entry["batch_fallback"] is False


def test_all_execution_fields_participate_in_tool_and_effective_catalog_digests():
    original = _instant_tool()
    original_tool_digest = deps.catalog_tool_digest(original)
    original_effective_digest = deps.base_catalog_pin([original])["effective_catalog_digest"]

    mutations = {
        "execution_class": "batch",
        "runtime": "cpython-3.14-restricted-v1",
        "limits": {**original["limits"], "wall_ms": 101},
        "artifact_digest": "sha256:" + ("b" * 64),
    }
    for field, value in mutations.items():
        changed = copy.deepcopy(original)
        changed[field] = value
        assert deps.catalog_tool_digest(changed) != original_tool_digest, field
        assert (
            deps.base_catalog_pin([changed])["effective_catalog_digest"]
            != original_effective_digest
        ), field


def test_tracked_instant_tool_source_matches_its_catalog_digest():
    tool = next(
        item for item in deps.load_seed_catalog_tools()
        if item["name"] == "instant-list-layers"
    )
    source = (Path(__file__).parents[1] / "instant_tools" / tool["entry"]).read_text(
        encoding="utf-8"
    ).replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    digest = "sha256:" + hashlib.sha256(source).hexdigest()

    assert tool["execution_class"] == "instant"
    assert tool["capabilities"] == ["drawing.read"]
    assert tool["batch_fallback"] is False
    assert tool["code_digest"] == digest
    assert tool["artifact_digest"] == digest
