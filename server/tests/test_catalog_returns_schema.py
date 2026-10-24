"""Opt-in return contracts leave legacy projections unchanged."""
import copy
import json

import pytest

import catalog
from lab_validate import ManifestInvalid, validate_returns_schema


def projected(tool, monkeypatch):
    monkeypatch.setattr(catalog, "seed_tools", lambda: [tool])
    return catalog.build_catalog([], include_internal=True)[0]["capabilities"][0]


def test_valid_returns_schema(monkeypatch):
    tool = {"name": "example", "family_id": "custom", "returns_schema": {"type": "object"}}
    original = copy.deepcopy(tool)
    result = projected(tool, monkeypatch)
    assert result["returns_schema"] == tool["returns_schema"]
    assert result["returns_schema_digest"] == validate_returns_schema(tool["returns_schema"])
    assert tool == original


def test_bad_returns_schema(monkeypatch):
    tool = {"name": "example", "family_id": "custom", "returns_schema": {"$ref": "https://example.invalid"}}
    with pytest.raises(ManifestInvalid) as caught:
        projected(tool, monkeypatch)
    assert caught.value.to_http() == (422, {
        "detail": "lab_returns_schema_bounds", "pointer": "/$ref", "block": "example",
    })


def test_legacy_projection_bytes(monkeypatch):
    tool = {"name": "example", "family_id": "custom"}
    before = json.dumps(projected(tool, monkeypatch))
    with_schema = dict(tool, returns_schema={})
    result = projected(with_schema, monkeypatch)
    del result["returns_schema"]
    del result["returns_schema_digest"]
    assert json.dumps(result) == before
    assert not any(key.startswith("returns_schema") for key in projected(tool, monkeypatch))
