"""Release catalogs must agree with the managed Home Assistant version."""
import json
from pathlib import Path

import pytest

from leaf_agent.config import PINNED_CATALOG
from leaf_agent.packages.homeassistant import PINNED_VERSION


def test_agent_catalog_matches_homeassistant_pin():
    versions = [entry["version"] for entry in PINNED_CATALOG
                if entry["package"] == "homeassistant"]
    assert versions == [PINNED_VERSION]


def test_server_catalog_matches_homeassistant_pin():
    path = Path(__file__).resolve().parents[2] / "server/device_packages.json"
    if not path.exists():
        pytest.skip("Server catalog is not present in this tree")
    catalog = json.loads(path.read_text(encoding="utf-8"))
    versions = [entry["version"] for entry in catalog
                if entry["package"] == "homeassistant"]
    assert versions == [PINNED_VERSION]
