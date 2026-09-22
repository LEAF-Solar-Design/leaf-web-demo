#!/usr/bin/env python3
"""Produce Studio's W1 unit sync (the settings family) through its own builtin.

The graph is built from the bound intake exactly as the solve producer builds it
(seeded in inches, the fixture's INSUNITS 1), the unit-sync builtin changes the
declaration only, and the reopened graph is written with identity metadata whose
parameters are {"family": "settings", "distance_unit": "Meters"|"Feet"}. Every
panel coordinate is checked unchanged across the sync before anything is written.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The solve producer owns the intake import, fixture binding and revision rules.
solve = _sibling("solar_w1_studio_solve")
ProducerError = solve.ProducerError
DISTANCE_UNITS = ("Meters", "Feet")
SEED_UNITS = "in"


def produce(args):
    started = time.monotonic()
    if args.distance_unit not in DISTANCE_UNITS:
        raise ProducerError("distance unit must be Meters or Feet")
    fixture = args.fixture.resolve()
    fixture_hash = solve.sha256(fixture.read_bytes())
    intake, intake_hash = solve.read_json(args.intake)
    if intake.get("source", {}).get("dwg_sha256") != fixture_hash:
        raise ProducerError("fixture hash mismatch with intake")
    revision = solve.fixture_revision(fixture)
    tenant = args.tenant or "studio-replay"
    created_at = datetime.now(timezone.utc).isoformat()
    graph = solve.new_empty_graph(
        tenant_id=tenant, drawing_id="w1-" + fixture_hash[:16], source_hash=intake_hash,
        units={"drawing_units": SEED_UNITS, "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
               "elevation_datum": "unrecorded", "crs": ""}, created_at=created_at)
    solve.import_panels(graph, intake, created_at)
    graph = solve.validate_graph(graph)
    geometry = [(p["id"], p["centre"], p["angle"]) for p in graph["panels"]]
    synced = solve.builtin("solar_unit_sync").sync_units(graph, {
        "expected_rev": graph["rev"], "distance_unit": args.distance_unit})
    reopened = solve.deserialize_graph(solve.serialize_graph(synced["graph"]))
    solve.validate_graph(reopened)
    if [(p["id"], p["centre"], p["angle"]) for p in reopened["panels"]] != geometry:
        raise ProducerError("unit sync must not move geometry")
    if reopened["project"]["units"]["drawing_units"] != synced["drawing_units"]:
        raise ProducerError("reopened graph lost the synced unit declaration")
    mapping = {p["id"]: solve.normalized_handle(p["provenance"]["source_handle"]) for p in reopened["panels"]}
    metadata = {
        "fixture_sha256": fixture_hash, "revision": revision,
        "parameters": {"family": "settings", "distance_unit": args.distance_unit},
        "versions": {"schema": "leaf.solar-w1-comparison.v1", "producer": "solar_w1_studio_unitsync.v1",
                     "capability": "unit-sync", "engine": "server-builtin", "catalog": "none",
                     "solver": "none"},
        "coordinate_system": "world", "geometry_units": reopened["project"]["units"]["drawing_units"],
        "angle_units": "deg", "entity_mapping": mapping, "before": {"recorded": False},
        "changes": {"created": [], "modified": [], "deleted": []}, "warnings": [], "rejected_inputs": [],
        "elapsed_ms": (time.monotonic() - started) * 1000,
        "execution_mode": "replay", "state": "committed",
        "survived_reopen": True, "synthetic_fields": ["before", "changes"],
        "fallback_fields": ["panels/producer-built-from-intake",
                            "units/before-sync/producer-seeded-" + SEED_UNITS],
        "synthetic_flagged": True,
        "provenance": {"intake_sha256": intake_hash,
                       "unit_sync": {"changed": synced["changed"], "drawing_units": synced["drawing_units"],
                                     "seed_units": SEED_UNITS}},
    }
    args.out_graph.write_text(solve.serialize_graph(reopened) + "\n", encoding="utf-8")
    args.out_metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True,
                                            allow_nan=False) + "\n", encoding="utf-8")
    return reopened, metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("fixture", "intake", "out-graph", "out-metadata"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--distance-unit", choices=DISTANCE_UNITS, required=True)
    parser.add_argument("--tenant")
    args = parser.parse_args(argv)
    try:
        produce(args)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError) as exc:
        # Never print an input object or a provider body.
        message = str(exc) if isinstance(exc, ProducerError) else getattr(exc, "code", None)
        message = message or getattr(exc, "classification", "invalid producer input or output")
        print(f"solar-w1-studio-unitsync: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
