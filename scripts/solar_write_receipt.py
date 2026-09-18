#!/usr/bin/env python3
"""Write executable Solar parity receipts from neutral evidence files only.

Plugin provenance must supply build, receipt_sha256 and fixture_id. Studio
provenance must supply engine (the parity ledger engine category). These are
producer facts, never inferred from the checkout or today's HEAD. A valid FAIL
receipt is written normally; CLI exit 1 reports divergence, exit 2 input error.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compare = _sibling("solar_w1_compare")
status = _sibling("solar_parity_status")


def build_receipt(plugin, studio, capability, capability_version, family, *,
                  produced_at=None, prerequisites=None, objective_bound=None):
    """Preserve both inputs and use the frozen comparator as the sole verdict."""
    comparison = {
        "schema": compare.SCHEMA, "capability": capability, "family": family,
        "plugin": deepcopy(plugin), "studio": deepcopy(studio),
    }
    if prerequisites is not None:
        comparison["prerequisites"] = deepcopy(prerequisites)
    if objective_bound is not None:
        comparison["objective_bound"] = objective_bound
    comparator = compare.compare_document(comparison)
    if plugin["fixture_sha256"] != studio["fixture_sha256"]:
        raise compare.InputError("receipt requires the same fixture on both sides")
    if plugin["survived_reopen"] != studio["survived_reopen"]:
        raise compare.InputError("receipt requires the same reopen state on both sides")
    if not isinstance(capability_version, str) or not capability_version or studio["versions"]["capability"] != capability_version:
        raise compare.InputError("Studio capability version disagrees with receipt")
    provenance = plugin["provenance"]
    for key in ("build", "receipt_sha256", "fixture_id"):
        if not isinstance(provenance.get(key), str) or not provenance[key].strip():
            raise compare.InputError("plugin provenance requires " + key)
    if not re.fullmatch(r"[0-9a-f]{64}", provenance["receipt_sha256"]):
        raise compare.InputError("invalid plugin provenance receipt_sha256")
    engine = studio["provenance"].get("engine")
    if engine not in status.ENGINES:
        raise compare.InputError("Studio provenance requires a ledger engine")
    timestamp = produced_at if produced_at is not None else datetime.now(timezone.utc).isoformat()
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise compare.InputError("produced_at must be nonempty")
    return {
        "schema": status.SCHEMA_RECEIPT,
        "capability": capability, "capability_version": capability_version,
        "fixture": {"id": provenance["fixture_id"], "sha256": plugin["fixture_sha256"]},
        "plugin": {"build": provenance["build"], "state": plugin["state"], "receipt_sha256": provenance["receipt_sha256"]},
        "studio": {"capability_version": studio["versions"]["capability"], "engine": engine},
        "comparator": comparator, "comparison": comparison,
        "synthetic_fields": [f"{side}/{field}" for side, evidence in (("plugin", plugin), ("studio", studio)) for field in evidence["synthetic_fields"]],
        "fallback_fields": [f"{side}/{field}" for side, evidence in (("plugin", plugin), ("studio", studio)) for field in evidence["fallback_fields"]],
        "synthetic_flagged": all(evidence["synthetic_flagged"] for evidence in (plugin, studio) if evidence["synthetic_fields"] or evidence["fallback_fields"]),
        "survived_reopen": plugin["survived_reopen"], "produced_at": timestamp,
    }


def write_receipt(receipt, receipts_dir):
    """Validate a temporary receipt with the real parser before publishing it."""
    capability = receipt["capability"]
    if not isinstance(capability, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", capability):
        raise compare.InputError("invalid capability")
    payload = json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(payload.encode("utf-8")) > status.MAX_RECEIPT_BYTES:
        raise compare.InputError("receipt exceeds byte limit")
    directory = Path(receipts_dir) / capability
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (compare.semantic_hash(receipt) + ".json")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        status.parse_receipt(temporary, capability)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-input", type=Path, required=True)
    parser.add_argument("--studio-input", type=Path, required=True)
    parser.add_argument("--capability", required=True)
    parser.add_argument("--capability-version", required=True)
    parser.add_argument("--family", choices=sorted(compare.FAMILIES), required=True)
    parser.add_argument("--prerequisites", type=Path)
    parser.add_argument("--objective-bound", type=float)
    parser.add_argument("--receipts-dir", type=Path, default=Path(__file__).resolve().parents[1] / "docs" / "parity" / "receipts")
    args = parser.parse_args(argv)
    try:
        receipt = build_receipt(
            compare.load_evidence(args.plugin_input), compare.load_evidence(args.studio_input),
            args.capability, args.capability_version, args.family,
            prerequisites=compare.load_evidence(args.prerequisites) if args.prerequisites else None,
            objective_bound=args.objective_bound,
        )
        path = write_receipt(receipt, args.receipts_dir)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, status.InputError) as exc:
        print(f"solar-write-receipt: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"path": str(path), "comparator": receipt["comparator"]}, sort_keys=True))
    return 0 if receipt["comparator"]["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
