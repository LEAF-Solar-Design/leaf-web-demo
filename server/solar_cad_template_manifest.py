"""Closed immutable manifest contract for the dormant Solar CAD template lane.

This module validates evidence identities only. It does not find templates,
read drawings, contact APS, or assert that any accepted producer input exists.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


SCHEMA_ID = "leaf.solar-cad-template-manifest.v1"
_FIELDS = (
    "schema",
    "template_version",
    "source_drawing_sha256",
    "catalog_sha256",
    "standards_snapshot_sha256",
    "compiled_tool_bundle_sha256",
    "license_record_sha256",
)
_DIGEST_FIELDS = _FIELDS[2:]
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[1-9][0-9]*\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_SECRET_KEY_RE = re.compile(
    r"(?:secret|token|password|credential|private[_-]?key|api[_-]?key)",
    re.IGNORECASE,
)


class SolarCadTemplateManifestError(ValueError):
    """The supplied bytes do not represent the closed immutable contract."""


def _reject_constant(value: str) -> None:
    raise SolarCadTemplateManifestError(f"non-finite JSON constant: {value}")


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SolarCadTemplateManifestError("duplicate JSON key")
        if _SECRET_KEY_RE.search(key):
            raise SolarCadTemplateManifestError("secret-shaped fields are forbidden")
        result[key] = value
    return result


@dataclass(frozen=True)
class SolarCadTemplateManifest:
    schema: str
    template_version: str
    source_drawing_sha256: str
    catalog_sha256: str
    standards_snapshot_sha256: str
    compiled_tool_bundle_sha256: str
    license_record_sha256: str

    @classmethod
    def parse(cls, raw: str | bytes) -> "SolarCadTemplateManifest":
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SolarCadTemplateManifestError("manifest must be UTF-8") from exc
        elif type(raw) is str:
            text = raw
        else:
            raise SolarCadTemplateManifestError("manifest must be text or bytes")

        if text.startswith("\ufeff"):
            raise SolarCadTemplateManifestError("byte-order marks are forbidden")
        try:
            value = json.loads(
                text,
                object_pairs_hook=_closed_object,
                parse_constant=_reject_constant,
            )
        except SolarCadTemplateManifestError:
            raise
        except (TypeError, ValueError) as exc:
            raise SolarCadTemplateManifestError("manifest is invalid JSON") from exc

        if not isinstance(value, dict):
            raise SolarCadTemplateManifestError("manifest must be an object")
        actual = set(value)
        expected = set(_FIELDS)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise SolarCadTemplateManifestError(
                f"manifest fields are not exact: missing={missing}, extra={extra}"
            )
        if any(type(value[field]) is not str for field in _FIELDS):
            raise SolarCadTemplateManifestError("manifest values must be strings")
        if value["schema"] != SCHEMA_ID:
            raise SolarCadTemplateManifestError("manifest schema is not supported")
        if not _VERSION_RE.fullmatch(value["template_version"]):
            raise SolarCadTemplateManifestError("template version is not canonical")
        for field in _DIGEST_FIELDS:
            if not _SHA256_RE.fullmatch(value[field]):
                raise SolarCadTemplateManifestError(f"{field} is not a SHA-256 digest")
        return cls(**value)

    def to_mapping(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in _FIELDS}

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
