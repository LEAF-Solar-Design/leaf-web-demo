#!/usr/bin/env python3
"""Project Studio graph snapshots into the Solar W1 neutral evidence contract.

The graph supplies frames (groups), panels, revision and drawing frame. A
separate --metadata JSON supplies measured run facts: the evidence keys other
than after, output_sha256, revision, units and frame, plus coordinate_system,
geometry_units and angle_units. In particular before, changes, identity mapping
and survived_reopen must come from the producer, not from this snapshot reader.
geometry_units and angle_units declare the graph geometry's actual units.
Missing group semantics are represented by null and named in fallback_fields.
This adapter does not execute a capability or claim a reopen occurred.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compare = _sibling("solar_w1_compare")


def build_evidence(graph, family, metadata):
    """Build one 22-key evidence object, refusing absent measurement metadata."""
    if family not in ("groups", "panels"):
        raise compare.InputError("Studio adapter supports groups and panels")
    compare.semantic_hash(graph)  # Bound and reject non-JSON/nonfinite input.
    if type(graph.get("graph_schema_version")) is not int or graph["graph_schema_version"] != 1:
        raise compare.InputError("unsupported Studio graph version")
    if type(graph.get("rev")) is not int or graph["rev"] < 0:
        raise compare.InputError("invalid Studio graph revision")
    derived = {"after", "output_sha256", "revision", "units", "frame"}
    required = compare.EVIDENCE_KEYS - derived
    if not isinstance(metadata, dict) or not required <= metadata.keys():
        raise compare.InputError("missing measured evidence metadata")
    result = {key: deepcopy(metadata[key]) for key in required}
    if not isinstance(result["fallback_fields"], list):
        raise compare.InputError("fallback_fields must be an array")
    units = graph["project"]["units"]
    geometry_units = metadata["geometry_units"]
    angle_units = metadata["angle_units"]
    if geometry_units not in compare.LENGTH_UNITS or angle_units not in compare.ANGLE_UNITS:
        raise compare.InputError("unsupported geometry units")

    def absent(path):
        if path not in result["fallback_fields"]:
            result["fallback_fields"].append(path)
        result["synthetic_flagged"] = True
        return None

    def quantity(kind, value, unit):
        return {"kind": kind, "value": deepcopy(value), "unit": unit}

    def reference(identifier):
        if not isinstance(identifier, str) or identifier not in result["entity_mapping"]:
            raise compare.InputError("graph entity requires an explicit neutral mapping")
        return {"entity_id": identifier}

    result["revision"] = str(graph["rev"])
    result["units"] = units["drawing_units"]
    result["frame"] = {
        "coordinate_system": metadata["coordinate_system"],
        "transform": deepcopy(units["wcs_to_ucs"]),
        "elevation_datum": units["elevation_datum"],
        "crs": units["crs"],
    }
    if not result["frame"]["crs"]:
        absent("frame/crs")
        result["frame"]["crs"] = "none"
    records = []
    collection = graph["frames" if family == "groups" else "panels"]
    if not isinstance(collection, list):
        raise compare.InputError("graph collection must be an array")
    for index, item in enumerate(collection):
        path = f"after/{family}/{index}"
        record = {"id": reference(item["id"])}
        if family == "panels":
            record["geometry"] = {
                "centre": quantity("coordinate", item["centre"], geometry_units),
                "angle": quantity("angle", item["angle"], angle_units),
            }
        else:
            point = item["insertion_point"]
            if not isinstance(point, list) or len(point) not in (2, 3):
                raise compare.InputError("invalid group insertion point")
            record.update({
                "name": item["name"],
                "membership": [reference(identifier) for identifier in item["panel_refs"]],
                "boundaries": deepcopy(item["boundaries"]) if "boundaries" in item else absent(path + "/boundaries"),
                "elevation": quantity("length", point[2], geometry_units) if len(point) == 3 else absent(path + "/elevation"),
                "equipment_config": {key: deepcopy(item[key]) for key in (
                    "installation_design", "module_rows", "module_columns", "module_slots",
                    "module_power_watts", "module_width_along_row", "module_height_across_row",
                )},
                "sizing_provenance": deepcopy(item["sizing_provenance"]) if "sizing_provenance" in item else absent(path + "/sizing_provenance"),
            })
        records.append(record)
    result["after"] = {family: records}
    result["output_sha256"] = compare.semantic_hash(result["after"])
    result["provenance"]["studio_graph_sha256"] = compare.semantic_hash(graph)
    compare.validate_evidence(result, family)
    # The evidence validator checks shape; normalization also checks references
    # and quantity declarations, including the caller's before snapshot.
    compare._normalize(result["before"], result["entity_mapping"])
    compare._normalize(result["after"], result["entity_mapping"])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--family", choices=("groups", "panels"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        evidence = build_evidence(compare.load_evidence(args.graph), args.family, compare.load_evidence(args.metadata))
        payload = json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if len(payload.encode("utf-8")) > compare.MAX_BYTES:
            raise compare.InputError("evidence exceeds byte limit")
        args.output.write_text(payload, encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-studio-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
