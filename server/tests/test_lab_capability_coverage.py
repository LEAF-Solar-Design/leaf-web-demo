"""Completeness is measured against the real catalog composition seam."""
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path

import pytest

import catalog
import deps
import mcp_tool_projection
from lab_registry import KASHBOT_CLASSES

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "contract/lab-capability-coverage.v1.json"
COVERAGE_SHA256 = "cdb77a4b5fb4647a16392ad347219c880bbbd22a8c15df13a99ccc108625999f"
STATUSES = {"runnable", "readable", "explicit-handoff", "unavailable-with-reason"}


def check_coverage(coverage, families, sources):
    assert coverage["schema"] == "leaf.lab-capability-coverage.v1"
    family_counts = Counter(coverage["families"])
    for family in sorted(set(families) | set(family_counts)):
        assert family_counts[family] == (1 if family in families else 0), f"family:{family} missing, duplicate or unexpected"
    counts = Counter(entry["source"] for entry in coverage["entries"])
    for source in sorted(sources | set(counts)):
        assert counts[source] == (1 if source in sources else 0), f"{source} missing, duplicate or unexpected"
    for entry in coverage["entries"]:
        source = entry["source"]
        assert entry["status"] in STATUSES, source
        assert isinstance(entry.get("adapter_owner"), str) and entry["adapter_owner"].strip(), source
        assert entry["host"] in ("production", "pc"), source
        assert not (entry["host"] == "pc" and entry["status"] == "runnable"), source
        if entry["status"] == "unavailable-with-reason":
            assert isinstance(entry.get("reason"), str) and entry["reason"].strip(), source


@pytest.fixture
def inventory(monkeypatch, tmp_path):
    # Use the tracked tenant tier, rather than operator-local tenant settings.
    tenant_tools = json.loads((ROOT / "harness/test/fixtures/tenant-repo/registry.json").read_text(encoding="utf-8"))["tools"]
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda _tenant: tenant_tools)
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.setenv("LEAF_TENANT_MCP_DIR", str(tmp_path / "mcp"))
    tools = deps.all_tools("coverage-tenant") + catalog.seed_tools()
    sources = {"studio-tool:" + tool["name"] for tool in tools}
    sources.update("kashbot:" + name for name in KASHBOT_CLASSES)
    sources.update("mcp:" + tool["name"] for tool in mcp_tool_projection.projected_tools("coverage-tenant"))
    config = json.loads(catalog.FAMILIES_FILE.read_text(encoding="utf-8"))
    return {family["family_id"] for family in config["families"]}, sources


def load():
    return json.loads(PATH.read_text(encoding="utf-8"))


def test_complete(inventory):
    check_coverage(load(), *inventory)


def test_missing_entry_named(inventory):
    coverage = load()
    source = coverage["entries"].pop()["source"]
    with pytest.raises(AssertionError, match=source):
        check_coverage(coverage, *inventory)


def test_duplicate_entry_named(inventory):
    coverage = load()
    duplicate = copy.deepcopy(coverage["entries"][0])
    coverage["entries"].append(duplicate)
    with pytest.raises(AssertionError, match=duplicate["source"]):
        check_coverage(coverage, *inventory)


def test_missing_family_named(inventory):
    coverage = load()
    family = coverage["families"].pop()
    with pytest.raises(AssertionError, match=family):
        check_coverage(coverage, *inventory)


def test_duplicate_family_named(inventory):
    coverage = load()
    family = coverage["families"][0]
    coverage["families"].append(family)
    with pytest.raises(AssertionError, match=family):
        check_coverage(coverage, *inventory)


def test_pc_cannot_run(inventory):
    coverage = load()
    entry = coverage["entries"][0]
    entry.update(host="pc", status="runnable")
    with pytest.raises(AssertionError, match=entry["source"]):
        check_coverage(coverage, *inventory)


def test_unavailable_requires_reason(inventory):
    coverage = load()
    entry = coverage["entries"][0]
    entry["status"] = "unavailable-with-reason"
    with pytest.raises(AssertionError, match=entry["source"]):
        check_coverage(coverage, *inventory)
    entry["reason"] = "Adapter pending SEL-306"
    check_coverage(coverage, *inventory)


def test_sha_pin():
    actual = hashlib.sha256(PATH.read_bytes()).hexdigest()
    assert actual == COVERAGE_SHA256, f"coverage sha256 changed: {actual}"
