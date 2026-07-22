"""Dependency-free proof for the fixed-point provenance hasher (platform/hashing.py).

No Postgres, no third-party imports: the module under test is loaded straight
from its file path (same pattern as test_ledger_static.py). The frozen vectors
were computed from arlo-3dml ``arlo/core/hashing.py`` @ aa11646 and verified
byte-identical against the port at lift time; they must never change without an
explicit resolution-contract decision.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
from dataclasses import dataclass
from enum import Enum

import pytest


_PKG = pathlib.Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("leaf_platform_hashing", _PKG / "hashing.py")
assert _SPEC and _SPEC.loader
hashing = importlib.util.module_from_spec(_SPEC)
sys.modules["leaf_platform_hashing"] = hashing
_SPEC.loader.exec_module(hashing)


# Frozen digest vectors (arlo-3dml aa11646 agreement). Changing DEFAULT_RESOLUTION_M
# or canonicalization breaks these on purpose.
VECTORS = [
    ({}, "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
    ({"tool": "autofill-string-targets", "n": 3, "ok": True, "note": None},
     "43164284294b5f3a0407aad21fcebe3b00e58fa658c343db569bc2ff2694ea65"),
    ({"points": [[0.0001, 0.00014999], [1.5, -2.25]], "elev_m": 12.30004},
     "edccf621bc853a8059c85d6e5b79f47221808dcfd58fc05c697818522fa6ea47"),
    ({"z": [3, 2, 1], "a": {"b": (1.0, 2.0), "c": "x"}},
     "9c86d7c10d32ec0af46d9ae7a80c04b8766afde7f3c7a119c55b0e761eed41d3"),
    ({"x": -0.0, "y": 0.0},
     "da2102bffbf3d3dd24a5941b50f38096c320d3ee2cd9182c5c816d9c6aca449f"),
    ({"a": 0.00005, "b": 0.00015, "c": 0.00025},
     "06b91fbce247642a0403664e1694181bcd0ff615ff5468d7581a9be9db9d79e5"),
]


@pytest.mark.parametrize("value,expected", VECTORS, ids=[f"v{i}" for i in range(len(VECTORS))])
def test_frozen_vectors(value, expected):
    assert hashing.stable_hash(value) == expected


def test_resolution_constant_is_frozen():
    assert hashing.DEFAULT_RESOLUTION_M == 1e-4


def test_quantize_round_trip_and_half_even():
    # 0.5 / 1.5 / 2.5 are exactly representable; half-even rounds to 0 / 2 / 2.
    assert hashing.quantize_value(0.5, 1.0) == 0
    assert hashing.quantize_value(1.5, 1.0) == 2
    assert hashing.quantize_value(2.5, 1.0) == 2
    assert hashing.dequantize_value(12345) == pytest.approx(1.2345)
    assert hashing.quantize_sequence([1.0, -2.0], 1.0) == (1, -2)


def test_repeat_and_key_order_determinism():
    a = {"a": 1, "b": 2.5, "c": [1.0, {"d": True}]}
    b = {"c": [1.0, {"d": True}], "b": 2.5, "a": 1}
    assert hashing.stable_hash(a) == hashing.stable_hash(a)
    assert hashing.stable_hash(a) == hashing.stable_hash(b)


def test_negative_zero_and_within_resolution_equivalence():
    assert hashing.stable_hash({"x": -0.0}) == hashing.stable_hash({"x": 0.0})
    # values that quantize to the same grid cell hash identically
    assert hashing.stable_hash({"x": 3.0000001}, resolution=1.0) == \
        hashing.stable_hash({"x": 3.0}, resolution=1.0)


def test_tuple_list_equivalence_and_str_keys():
    assert hashing.stable_hash({"p": (1.0, 2.0)}) == hashing.stable_hash({"p": [1.0, 2.0]})
    assert hashing.stable_hash({1: "a"}) == hashing.stable_hash({"1": "a"})


class _Kind(Enum):
    STRING = "string"


@dataclass
class _Point:
    x: float
    y: float


class _WithToDict:
    def to_dict(self):
        return {"x": 1.0, "y": 2.0}


def test_enum_dataclass_and_to_dict_paths():
    assert hashing.stable_hash({"k": _Kind.STRING}) == hashing.stable_hash({"k": "string"})
    assert hashing.stable_hash(_Point(1.0, 2.0)) == hashing.stable_hash({"x": 1.0, "y": 2.0})
    assert hashing.stable_hash(_WithToDict()) == hashing.stable_hash({"x": 1.0, "y": 2.0})


def test_rejections_fail_closed():
    with pytest.raises(ValueError):
        hashing.quantize_value(float("nan"))
    with pytest.raises(ValueError):
        hashing.quantize_value(float("inf"))
    with pytest.raises(ValueError):
        hashing.quantize_value(1.0, resolution=0)
    with pytest.raises(TypeError):
        hashing.canonicalize(object())


def test_module_is_stdlib_only():
    tree = ast.parse((_PKG / "hashing.py").read_text(encoding="utf-8"))
    allowed = {"__future__", "dataclasses", "enum", "hashlib", "json", "math", "typing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {(node.module or "").split(".")[0]}
        else:
            continue
        assert names <= allowed, f"non-stdlib import: {names - allowed}"
