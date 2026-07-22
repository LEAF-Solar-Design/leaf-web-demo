"""Deterministic fixed-point quantization and content hashing (run provenance).

Ported from arlo-3dml ``arlo/core/hashing.py`` @ aa11646 (census #45: deterministic
hashing core). Behavior is intentionally byte-identical to the arlo original so
digests minted by either lineage agree.

Delineation from ``models.canonical_hash`` (this package already hashes): the
ledger hasher canonicalizes via RFC8785-JCS with domain separation and treats
floats verbatim — right for ledger/config records where the bytes ARE the
content. THIS module is the geometry-bearing contract: floats are quantized to a
fixed resolution grid before canonicalization, so coordinates that land in the
same quantization cell and cross-platform float-formatting differences produce
the SAME digest. Use ``models.canonical_hash`` for ledger records; use
``stable_hash`` here for run outputs, replay comparison, and output caching.

``DEFAULT_RESOLUTION_M`` is FROZEN: changing it invalidates every stored hash.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
import hashlib
import json
import math
from typing import Any


# FROZEN CONTRACT: 0.1 mm grid. Changing this constant invalidates every stored
# hash minted through this module (and breaks agreement with arlo-3dml digests).
DEFAULT_RESOLUTION_M = 1e-4


def quantize_value(value: float, resolution: float = DEFAULT_RESOLUTION_M) -> int:
    """Quantize a finite floating-point value with Python's half-even rounding."""
    if not math.isfinite(value):
        raise ValueError(f"Cannot quantize non-finite value: {value!r}")
    if resolution <= 0:
        raise ValueError("resolution must be positive")
    return int(round(value / resolution))


def dequantize_value(value: int, resolution: float = DEFAULT_RESOLUTION_M) -> float:
    """Convert a fixed-point integer back to meters."""
    return float(value) * resolution


def quantize_sequence(values: list[float] | tuple[float, ...], resolution: float = DEFAULT_RESOLUTION_M) -> tuple[int, ...]:
    """Quantize a sequence of floats into a deterministic integer tuple."""
    return tuple(quantize_value(float(value), resolution) for value in values)


def canonicalize(value: Any, resolution: float = DEFAULT_RESOLUTION_M) -> Any:
    """Convert nested values into a stable JSON-compatible structure.

    Floats are represented as fixed-point integers so hashes are stable across
    platforms and JSON encoders.
    """
    if hasattr(value, "to_dict"):
        return canonicalize(value.to_dict(), resolution)
    if is_dataclass(value):
        return canonicalize(asdict(value), resolution)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): canonicalize(value[key], resolution) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [canonicalize(item, resolution) for item in value]
    if isinstance(value, float):
        return {"__fixed_m__": quantize_value(value, resolution)}
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise TypeError(f"Unsupported value for canonicalization: {type(value)!r}")


def canonical_json(value: Any, resolution: float = DEFAULT_RESOLUTION_M) -> str:
    """Serialize nested data to canonical JSON."""
    return json.dumps(canonicalize(value, resolution), sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any, resolution: float = DEFAULT_RESOLUTION_M) -> str:
    """Return a SHA-256 hex digest for a canonicalized value."""
    return hashlib.sha256(canonical_json(value, resolution).encode("utf-8")).hexdigest()
