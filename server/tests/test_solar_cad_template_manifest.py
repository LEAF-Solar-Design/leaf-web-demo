from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from server.solar_cad_template_manifest import (
    SCHEMA_ID,
    SolarCadTemplateManifest,
    SolarCadTemplateManifestError,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "contract" / "solar-cad-template-manifest.v1.schema.json"
DIGESTS = {
    "source_drawing_sha256": "1" * 64,
    "catalog_sha256": "2" * 64,
    "standards_snapshot_sha256": "3" * 64,
    "compiled_tool_bundle_sha256": "4" * 64,
    "license_record_sha256": "5" * 64,
}


def _manifest() -> dict[str, str]:
    return {
        "schema": SCHEMA_ID,
        "template_version": "1.0.0",
        **DIGESTS,
    }


def _parse(value: object) -> SolarCadTemplateManifest:
    return SolarCadTemplateManifest.parse(
        json.dumps(value, separators=(",", ":"), sort_keys=True)
    )


def test_schema_and_parser_bind_the_same_closed_contract() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    expected = set(_manifest())
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == expected
    assert set(schema["properties"]) == expected
    assert schema["properties"]["schema"]["const"] == SCHEMA_ID
    assert schema["$defs"]["sha256"]["pattern"] == "^[0-9a-f]{64}$"


def test_canonical_round_trip_is_stable_and_contains_only_immutable_witnesses() -> None:
    parsed = _parse(_manifest())
    encoded = parsed.canonical_json()
    assert encoded == SolarCadTemplateManifest.parse(encoded).canonical_json()
    assert parsed.to_mapping() == _manifest()
    assert "/" not in encoded
    assert "tag" not in encoded.lower()


@pytest.mark.parametrize("field", list(_manifest()))
def test_missing_fields_are_rejected(field: str) -> None:
    value = _manifest()
    del value[field]
    with pytest.raises(SolarCadTemplateManifestError, match="fields are not exact"):
        _parse(value)


@pytest.mark.parametrize(
    "field",
    [
        "source_drawing_path",
        "catalog_tag",
        "standards_branch",
        "mutable_ref",
        "api_token",
        "client_secret",
    ],
)
def test_paths_tags_mutable_refs_and_secret_shaped_fields_are_rejected(field: str) -> None:
    value = _manifest()
    value[field] = "forbidden"
    with pytest.raises(SolarCadTemplateManifestError):
        _parse(value)


@pytest.mark.parametrize(
    "digest",
    ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "sha256:" + "a" * 64],
)
def test_malformed_or_decorated_digests_are_rejected(digest: str) -> None:
    for field in DIGESTS:
        value = _manifest()
        value[field] = digest
        with pytest.raises(SolarCadTemplateManifestError, match="SHA-256"):
            _parse(value)


@pytest.mark.parametrize(
    "version",
    ["0.1.0", "01.0.0", "1.01.0", "1.0.00", "v1.0.0", "1.0", "1.0.0+build", "1.0.0-rc.1"],
)
def test_noncanonical_template_versions_are_rejected(version: str) -> None:
    value = _manifest()
    value["template_version"] = version
    with pytest.raises(SolarCadTemplateManifestError, match="not canonical"):
        _parse(value)


def test_ambiguous_or_non_json_inputs_are_rejected() -> None:
    duplicate = json.dumps(_manifest())[:-1] + ',"catalog_sha256":"' + "9" * 64 + '"}'
    with pytest.raises(SolarCadTemplateManifestError, match="duplicate JSON key"):
        SolarCadTemplateManifest.parse(duplicate)
    with pytest.raises(SolarCadTemplateManifestError, match="non-finite"):
        SolarCadTemplateManifest.parse('{"value":NaN}')
    with pytest.raises(SolarCadTemplateManifestError, match="byte-order"):
        SolarCadTemplateManifest.parse("\ufeff" + json.dumps(_manifest()))
    with pytest.raises(SolarCadTemplateManifestError, match="UTF-8"):
        SolarCadTemplateManifest.parse(b"\xff")
    with pytest.raises(SolarCadTemplateManifestError, match="must be an object"):
        SolarCadTemplateManifest.parse("[]")


def test_wrong_schema_and_non_string_values_are_rejected() -> None:
    wrong = _manifest()
    wrong["schema"] = "leaf.solar-cad-template-manifest.v2"
    with pytest.raises(SolarCadTemplateManifestError, match="not supported"):
        _parse(wrong)

    non_string = deepcopy(_manifest())
    non_string["template_version"] = 1  # type: ignore[assignment]
    with pytest.raises(SolarCadTemplateManifestError, match="must be strings"):
        _parse(non_string)
