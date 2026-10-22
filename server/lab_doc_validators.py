"""Bounded graph-only admission. Validation always precedes persistence."""
from __future__ import annotations

import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from lab_canonical import canonical_bytes

MAX_RAW_BYTES = 262144
MAX_GRAPH_BYTES = 262144
MAX_DEPTH = 32
ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_CONTRACT = Path(__file__).resolve().parents[1] / "contract"
_GRAPH = json.loads((_CONTRACT / "lab-graph.v1.schema.json").read_text(encoding="utf-8"))
_MANIFEST = json.loads((_CONTRACT / "fixtures/lab/leaf-block-manifest.v1.schema.json").read_text(encoding="utf-8"))
_REGISTRY = Registry().with_resources([
    (_MANIFEST["$id"], Resource.from_contents(_MANIFEST)),
    ("https://leaf.internal/contract/fixtures/lab/leaf-block-manifest.v1.schema.json",
     Resource.from_contents(_MANIFEST)),
])
_VALIDATOR = Draft202012Validator(_GRAPH, registry=_REGISTRY)


class InvalidGraph(ValueError):
    pass


class TooLarge(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise InvalidGraph("invalid_identifier")
    return value


def expected_head(value):
    if value is not None and (type(value) is not int or not 1 <= value <= 9223372036854775807):
        raise InvalidGraph("invalid_expected_head")
    return value


def _depth(value, depth=0):
    if depth > MAX_DEPTH:
        raise InvalidGraph("graph_too_deep")
    if isinstance(value, dict):
        for child in value.values():
            _depth(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _depth(child, depth + 1)


def validate_graph(graph):
    try:
        _depth(graph)
        data = canonical_bytes(graph)
        if len(data) > MAX_GRAPH_BYTES:
            raise TooLarge("graph_too_large")
        _VALIDATOR.validate(graph)
        ids = [block["id"] for block in graph["blocks"]]
        if len(set(ids)) != len(ids):
            raise InvalidGraph("duplicate_block")
        endpoints = [pipe[side] for pipe in graph["pipes"] for side in ("from", "to")]
        endpoints.extend(graph["outputs"].values())
        if any(endpoint["node"] not in ids for endpoint in endpoints):
            raise InvalidGraph("missing_block")
        return data
    except (ValidationError, UnicodeError, RecursionError, OverflowError) as error:
        raise InvalidGraph("invalid_graph") from error
    except ValueError as error:
        if isinstance(error, (InvalidGraph, TooLarge)):
            raise
        raise InvalidGraph("invalid_graph") from error


def parse_body(raw):
    if len(raw) > MAX_RAW_BYTES:
        raise TooLarge("body_too_large")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidGraph("duplicate_key")
            result[key] = value
        return result

    def constant(_):
        raise InvalidGraph("nonfinite_number")

    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        _depth(body)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise InvalidGraph("invalid_body") from error
    if not isinstance(body, dict):
        raise InvalidGraph("invalid_body")
    return body
