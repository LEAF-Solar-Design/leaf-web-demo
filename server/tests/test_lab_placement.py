"""The browser reads these same placement cases, including trust overrides."""
import json
from pathlib import Path

import pytest

from lab_evaluate import trusted_registry
from lab_placement import HOST_BLOCK_IDS, PLACEMENTS, SETTINGS_DIGEST_ID, placement

FIXTURE = json.loads((Path(__file__).parent / "fixtures/lab-placement-cases.v1.json").read_text(encoding="utf-8"))


def test_fixture_contract():
    assert FIXTURE["schema"] == "leaf.lab-placement-cases.v1"
    assert len({item["name"] for item in FIXTURE["cases"]}) == len(FIXTURE["cases"])
    assert PLACEMENTS == ("here", "browser", "server", "bot", "host")
    assert SETTINGS_DIGEST_ID == "kashbot2-config"
    assert HOST_BLOCK_IDS == frozenset({"studio-jobs", "studio-capabilities", "host-source", "host-intent"})


@pytest.mark.parametrize("item", FIXTURE["cases"], ids=lambda item: item["name"])
def test_shared_placement(item):
    manifest = item.get("manifest") or trusted_registry()[item["manifest_ref"]]
    before = json.loads(json.dumps(manifest))
    trusted_ids = None if item["trusted_ids"] is None else frozenset(item["trusted_ids"])
    assert placement(manifest, trusted_ids=trusted_ids) == item["expect"]
    assert manifest == before


def test_every_snapshot_block_runs_on_bot():
    # The arithmetic builtins evaluate here; every strategy block in the snapshot runs on the bot.
    manifests = list(trusted_registry().values())
    assert len(manifests) == 19
    here = {manifest["id"] for manifest in manifests if placement(manifest) == "here"}
    assert here == {"constant", "add", "subtract", "multiply", "divide"}
    assert all(placement(manifest) == "bot" for manifest in manifests if manifest["id"] not in here)


def test_tool_case_matches_real_projection():
    from lab_tool_manifests import project_tool
    item = next(item for item in FIXTURE["cases"] if item["name"] == "projected-tool")
    tool = project_tool({"name": "qa-read-tool", "version": "1.2.3", "kind": "script",
                         "capability": "drawing.read", "params": {"type": "object", "properties": {}},
                         "returns_schema": {"type": "number"}}, "tenant_repo")
    assert tool.reason is None
    assert tool.manifest == item["manifest"]
    assert placement(tool.manifest) == item["expect"]
