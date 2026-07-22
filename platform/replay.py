"""Gold-set replay: byte-identity verification of stored provenance digests.

Ported (in shape) from arlo-3dml ``arlo/semantics/replay/runner.py`` @ aa11646
(census #45, slice 2). The arlo runner replays IFC scene fixtures through its
semantics service and compares label content-hashes; that simulator stack does
not exist here, so THIS module ports the generic determinism contract only:

  * a GOLD SET is a JSON-native list of ``{id, payload, expected_digest}``;
  * REPLAY recomputes ``stable_hash(payload)`` (platform/hashing.py, frozen
    resolution) and verifies BYTE-IDENTITY against ``expected_digest``;
  * malformed records are SKIPPED VISIBLY in the report (arlo's ``skipped``
    discipline), never silently dropped and never a crash;
  * an empty gold set reports ``total=0`` — a vacuous pass stays visible.

Left behind from arlo, deliberately: scene loading, double-classification
comparison, and the leading-contract-key sniffing (all simulator-specific).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Load the sibling hashing module by file path: this package's directory name
# (``platform/``) shadows the stdlib ``platform`` module whenever the repo root
# is on sys.path, so ``from platform import hashing`` is unsafe. Same pattern
# as platform/tests/test_ledger_static.py.
_HASHING_PATH = Path(__file__).resolve().parent / "hashing.py"
_spec = importlib.util.spec_from_file_location("leaf_platform_hashing", _HASHING_PATH)
assert _spec and _spec.loader
if "leaf_platform_hashing" in sys.modules:
    _hashing = sys.modules["leaf_platform_hashing"]
else:
    _hashing = importlib.util.module_from_spec(_spec)
    sys.modules["leaf_platform_hashing"] = _hashing
    _spec.loader.exec_module(_hashing)

stable_hash = _hashing.stable_hash
DEFAULT_RESOLUTION_M = _hashing.DEFAULT_RESOLUTION_M


@dataclass(frozen=True)
class Divergence:
    record_id: str
    expected_digest: str
    actual_digest: str

    def to_dict(self) -> dict[str, str]:
        return {"record_id": self.record_id,
                "expected_digest": self.expected_digest,
                "actual_digest": self.actual_digest}


@dataclass(frozen=True)
class ReplayReport:
    total: int
    matched: int
    diverged: tuple[Divergence, ...] = ()
    skipped: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.diverged and not self.skipped

    def to_dict(self) -> dict[str, Any]:
        return {"total": self.total, "matched": self.matched,
                "ok": self.ok,
                "diverged": [d.to_dict() for d in self.diverged],
                "skipped": list(self.skipped)}


def mint_gold_set(payloads: dict[str, Any],
                  resolution: float = DEFAULT_RESOLUTION_M) -> list[dict[str, Any]]:
    """Build gold-set records ({id, payload, expected_digest}) from payloads."""
    return [{"id": str(record_id), "payload": payload,
             "expected_digest": stable_hash(payload, resolution)}
            for record_id, payload in payloads.items()]


def replay_gold_set(records: Iterable[dict[str, Any]],
                    resolution: float = DEFAULT_RESOLUTION_M) -> ReplayReport:
    """Recompute every record's digest and verify byte-identity."""
    total = 0
    matched = 0
    diverged: list[Divergence] = []
    skipped: list[str] = []
    for index, record in enumerate(records):
        total += 1
        if not isinstance(record, dict) or "payload" not in record \
                or not isinstance(record.get("expected_digest"), str):
            skipped.append(str(record.get("id", f"index:{index}"))
                           if isinstance(record, dict) else f"index:{index}")
            continue
        try:
            actual = stable_hash(record["payload"], resolution)
        except (TypeError, ValueError):
            skipped.append(str(record.get("id", f"index:{index}")))
            continue
        if actual == record["expected_digest"]:
            matched += 1
        else:
            diverged.append(Divergence(record_id=str(record.get("id", f"index:{index}")),
                                       expected_digest=record["expected_digest"],
                                       actual_digest=actual))
    return ReplayReport(total=total, matched=matched,
                        diverged=tuple(diverged), skipped=tuple(skipped))


def save_gold_set(records: list[dict[str, Any]], path: Path | str) -> None:
    Path(path).write_text(json.dumps(records, indent=1, sort_keys=True),
                          encoding="utf-8")


def load_gold_set(path: Path | str) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"gold set must be a JSON list, got {type(data).__name__}")
    return data


__all__ = ["Divergence", "ReplayReport", "mint_gold_set", "replay_gold_set",
           "save_gold_set", "load_gold_set", "stable_hash", "DEFAULT_RESOLUTION_M"]
