"""Pin Studio surface declarations to the server catalog and capability resolver."""
from __future__ import annotations

import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import catalog  # noqa: E402
import deps  # noqa: E402
import entitlements  # noqa: E402

FIXTURE = SERVER_DIR.parent / "web" / "src" / "site" / "familyCapabilities.json"


def test_family_capabilities_fixture_equals_the_server_computation():
    loaded = (deps.load_engine_registry_tools() + deps.load_seed_catalog_tools()
              + deps.load_seed_write_tools())
    by_name = {}
    for tool in loaded:
        by_name.setdefault(tool["name"], tool)
    families = catalog.build_catalog(list(by_name.values()))
    for tool in catalog.seed_tools():
        by_name.setdefault(tool["name"], tool)
    computed = {
        family["family_id"]: sorted({
            entitlements.tool_required_capability(by_name[entry["name"]])
            for entry in family["capabilities"]
        })
        for family in families
    }
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert list(computed.items()) == list(fixture.items())


def test_every_family_capability_is_an_explicit_boolean_in_every_tier():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    required = {"build"}.union(*fixture.values())
    tiers = json.loads((SERVER_DIR / "entitlements.json").read_text(encoding="utf-8"))
    for tier, policy in tiers.items():
        if tier.startswith("_"):
            continue
        for capability in required:
            assert type(policy[capability]) is bool, (tier, capability)


@pytest.mark.parametrize("tool, expected", [
    ({"name": "solar-solve-proposal"}, "solve"),
    ({"name": "solar-settings"}, "run_write"),
    ({"name": "x", "capabilities": None}, "run_write"),
    ({"name": "x", "capabilities": []}, "run_read"),
    ({"name": "x"}, "run_read"),
    ({"name": "x", "capabilities": ["drawing.write"]}, "run_write"),
])
def test_classifier_branches_the_declaration_rests_on(tool, expected):
    assert entitlements.tool_required_capability(tool) == expected
