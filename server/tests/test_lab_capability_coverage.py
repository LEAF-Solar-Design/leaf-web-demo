"""Completeness is measured against the real catalog composition seam."""
from collections import Counter
import ast
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

import catalog
import deps
import lab_tool_manifests
import mcp_tool_projection
from lab_registry import KASHBOT_CLASSES

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "contract/lab-capability-coverage.v1.json"
COVERAGE_SHA256 = "f41bb0ddccfdca5454909ddef3363e0aa0a94eb5166d4a5bf106ed846e7b29dd"
STATUSES = {"runnable", "readable", "explicit-handoff", "unavailable-with-reason"}


def check_coverage(coverage, families, sources, source_families):
    assert coverage["schema"] == "leaf.lab-capability-coverage.v1"
    family_counts = Counter(record["family"] for record in coverage["families"])
    for family in sorted(set(families) | set(family_counts)):
        assert family_counts[family] == (1 if family in families else 0), f"family:{family} missing, duplicate or unexpected"
    for index, record in enumerate(coverage["families"]):
        family = record["family"]
        assert family == families[index], f"family:{family} out of order"
    counts = Counter(entry["source"] for entry in coverage["entries"])
    for source in sorted(sources | set(counts)):
        assert counts[source] == (1 if source in sources else 0), f"{source} missing, duplicate or unexpected"
    for entry in coverage["entries"] + coverage["families"]:
        source = entry.get("source", "family:" + entry.get("family", ""))
        assert entry.get("status") in STATUSES, source
        assert isinstance(entry.get("adapter_owner"), str) and entry["adapter_owner"].strip(), source
        assert entry.get("host") in ("production", "pc"), source
        assert not (entry["host"] == "pc" and entry["status"] == "runnable"), source
        if entry["status"] == "unavailable-with-reason":
            assert isinstance(entry.get("reason"), str) and entry["reason"].strip(), source
    strength = {status: rank for rank, status in enumerate(
        ("unavailable-with-reason", "explicit-handoff", "readable", "runnable"))}
    for record in coverage["families"]:
        family = record["family"]
        entries = [entry for entry in coverage["entries"]
                   if source_families.get(entry["source"]) == family]
        expected = max((entry["status"] for entry in entries), key=strength.get,
                       default="unavailable-with-reason")
        assert record["status"] == expected, f"family:{family} strongest status"
        if not entries:
            if family == "mcp":
                assert record == {"family": "mcp", "status": "unavailable-with-reason",
                                  "adapter_owner": "server.mcp_tool_projection", "host": "production",
                                  "reason": "no connected MCP tools project into Lab"}, f"family:{family} reason"
            else:
                assert record.get("reason") == "no Lab adapter in wave 2", f"family:{family} reason"


@pytest.fixture
def inventory(monkeypatch, tmp_path):
    # Use the tracked tenant tier, rather than operator-local tenant settings.
    tenant_tools = json.loads((ROOT / "harness/test/fixtures/tenant-repo/registry.json").read_text(encoding="utf-8"))["tools"]
    import customization_service
    monkeypatch.setattr(customization_service, "load_authoritative_tenant_tools", lambda _tenant: tenant_tools)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda _tenant: tenant_tools)
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.setenv("LEAF_TENANT_MCP_DIR", str(tmp_path / "mcp"))
    tools = deps.all_tools("coverage-tenant") + catalog.seed_tools()
    sources = {"studio-tool:" + tool["name"] for tool in tools}
    sources.update("kashbot:" + name for name in KASHBOT_CLASSES)
    mcp_tools = mcp_tool_projection.projected_tools("coverage-tenant")
    sources.update("mcp:" + tool["name"] for tool in mcp_tools)
    config = json.loads(catalog.FAMILIES_FILE.read_text(encoding="utf-8"))
    source_families = {"studio-tool:" + tool["name"]: catalog.family_for_persist(tool)
                       for tool in tools}
    source_families.update({"mcp:" + tool["name"]: catalog.family_for_persist(tool)
                            for tool in mcp_tools})
    return [family["family_id"] for family in config["families"]] + ["mcp"], sources, source_families


def load():
    return json.loads(PATH.read_text(encoding="utf-8"))


def test_complete(inventory):
    coverage = load()
    check_coverage(coverage, *inventory)
    rows = {row["name"]: (row, source)
            for row, source in deps.effective_tools_with_provenance("coverage-tenant")}
    for row in catalog.seed_tools():
        rows.setdefault(row["name"], (row, "catalog_seed"))
    for entry in coverage["entries"]:
        if entry["source"].startswith("studio-tool:"):
            row, source = rows[entry["source"].removeprefix("studio-tool:")]
            projected = lab_tool_manifests.project_tool(row, source).manifest is not None
            assert (entry["status"] == "runnable") == projected, entry["source"]
            if projected:
                assert entry["adapter_owner"] == "server.lab_tool_manifests", entry["source"]
    assert mcp_tool_projection.projected_tools("coverage-tenant") == []


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
    family = coverage["families"].pop()["family"]
    with pytest.raises(AssertionError, match=family):
        check_coverage(coverage, *inventory)


def test_duplicate_family_named(inventory):
    coverage = load()
    family = coverage["families"][0]
    coverage["families"].append(family)
    with pytest.raises(AssertionError, match=family["family"]):
        check_coverage(coverage, *inventory)


@pytest.mark.parametrize("mutation", [
    "missing-status", "unknown-status", "empty-owner", "pc-runnable", "unknown-host",
    "missing-reason", "weaker-status", "wrong-order",
])
def test_J1_family_record_mutations_name_family(inventory, mutation):
    coverage = load()
    record = coverage["families"][0]
    if mutation == "missing-status":
        del record["status"]
    elif mutation == "unknown-status":
        record["status"] = "unknown"
    elif mutation == "empty-owner":
        record["adapter_owner"] = ""
    elif mutation == "pc-runnable":
        record.update(host="pc", status="runnable")
    elif mutation == "unknown-host":
        record["host"] = "unknown"
    elif mutation == "missing-reason":
        record = next(row for row in coverage["families"] if row["family"] == "solve")
        del record["reason"]
    elif mutation == "weaker-status":
        record["status"] = "explicit-handoff"
    else:
        coverage["families"][0], coverage["families"][1] = coverage["families"][1], record
        record = coverage["families"][0]
    with pytest.raises(AssertionError, match="family:" + record["family"]):
        check_coverage(coverage, *inventory)


@pytest.mark.parametrize("statuses,expected", [
    (("runnable", "readable"), "runnable"),
    (("readable", "explicit-handoff"), "readable"),
    (("explicit-handoff", "unavailable-with-reason"), "explicit-handoff"),
    (("unavailable-with-reason", "unavailable-with-reason"), "unavailable-with-reason"),
])
def test_J1_family_status_is_strongest_member(inventory, statuses, expected):
    coverage = load()
    source_families = inventory[2]
    entries = [entry for entry in coverage["entries"]
               if source_families.get(entry["source"]) == "measurement"]
    assert len(entries) == len(statuses)
    for entry, status in zip(entries, statuses):
        entry.update(status=status, reason="adapter unavailable")
    record = next(row for row in coverage["families"] if row["family"] == "measurement")
    record.update(status=expected, reason="adapter unavailable")
    check_coverage(coverage, *inventory)


def test_J2_gate_counts_cover_parametrized_baseline():
    tree = ast.parse((ROOT / "scripts/run-all-gates.py").read_text(encoding="utf-8"))
    suites = {
        "server-lab-registry": "tests/test_lab_registry.py",
        "server-lab-validate": "tests/test_lab_validate.py",
        "server-catalog-returns-schema": "tests/test_catalog_returns_schema.py",
        "server-lab-capability-coverage": "tests/test_lab_capability_coverage.py",
    }
    counts = {node.args[0].value: ast.literal_eval(node.args[5])
              for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "Suite" and len(node.args) > 5
              and isinstance(node.args[0], ast.Constant) and node.args[0].value in suites}
    assert counts.keys() == suites.keys()
    for suite, path in suites.items():
        result = subprocess.run(
            [sys.executable, "-m", "pytest", path, "--collect-only", "-q"],
            cwd=ROOT / "server", capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        node_ids = [line for line in result.stdout.splitlines() if line.startswith(path + "::")]
        assert len(node_ids) == counts[suite], (suite, len(node_ids), counts[suite])


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
