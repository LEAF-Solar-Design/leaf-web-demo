#!/usr/bin/env python3
"""Project Studio graph snapshots into the Solar W1 neutral evidence contract.

The graph supplies frames (groups), panels and drawing frame. A
separate --metadata JSON supplies measured run facts: the evidence keys other
than after, output_sha256, input_sha256, units and frame, plus coordinate_system,
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
import re
import sys


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compare = _sibling("solar_w1_compare")

# AutoCAD INSUNITS codes for the graph's drawing_units; anything else is an error, never a default.
INSUNITS = {"in": 1, "ft": 2, "mm": 4, "cm": 5, "m": 6, "km": 7, "yd": 10}
FAMILIES = ("groups", "panels", "settings", "strings")


def build_evidence(graph, family, metadata):
    """Build one 22-key evidence object, refusing absent measurement metadata."""
    if family not in FAMILIES:
        raise compare.InputError("Studio adapter supports groups, panels, settings and strings")
    compare.semantic_hash(graph)  # Bound and reject non-JSON/nonfinite input.
    if type(graph.get("graph_schema_version")) is not int or graph["graph_schema_version"] != 1:
        raise compare.InputError("unsupported Studio graph version")
    if type(graph.get("rev")) is not int or graph["rev"] < 0:
        raise compare.InputError("invalid Studio graph revision")
    derived = {"after", "output_sha256", "input_sha256", "units", "frame"}
    required = compare.EVIDENCE_KEYS - derived
    if not isinstance(metadata, dict) or not required <= metadata.keys():
        raise compare.InputError("missing measured evidence metadata")
    if "input_sha256" in metadata:
        raise compare.InputError("input_sha256 must be derived, not supplied")
    if not isinstance(metadata["revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", metadata["revision"]):
        raise compare.InputError("revision must be a 40-character lowercase git commit")
    result = {key: deepcopy(metadata[key]) for key in required}
    result["input_sha256"] = compare.semantic_hash({
        "fixture_sha256": metadata["fixture_sha256"], "parameters": metadata["parameters"],
    })
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
    if family == "settings":
        # The plugin's LEAFUNITSYNC writes only INSUNITS; Studio's declaration maps to that code.
        if not isinstance(units["drawing_units"], str) or units["drawing_units"] not in INSUNITS:
            raise compare.InputError("drawing units have no INSUNITS code")
        absent("after/settings/insunits/derived-from-drawing-units")
        collection = []
    else:
        collection = graph["frames" if family == "groups" else family]
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
        elif family == "strings":
            members = item.get("ordered_panel_refs")
            if not isinstance(members, list) or not members:
                raise compare.InputError("string ordered_panel_refs must be a nonempty array")
            count = item.get("module_count")
            if type(count) is not int or count < 0:
                raise compare.InputError("string module_count must be a nonnegative integer")
            record["ordered_membership"] = [reference(identifier) for identifier in members]
            extra = item.get("extra", {})
            if not isinstance(extra, dict):
                raise compare.InputError("string extra must be an object")
            if "polarity" not in extra:
                absent(path + "/polarity")
                record["polarity"] = "none"
            else:
                polarity = extra["polarity"]
                if not isinstance(polarity, dict):
                    raise compare.InputError("invalid string polarity")
                ends = (polarity.get("negative_panel_ref"), polarity.get("positive_panel_ref"))
                # Positive means membership runs from the negative to the positive terminal.
                if ends == (members[0], members[-1]):
                    record["polarity"] = "positive"
                elif ends == (members[-1], members[0]):
                    record["polarity"] = "negative"
                else:
                    raise compare.InputError("string polarity does not match membership endpoints")
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
    if family == "strings":
        records.sort(key=lambda record: result["entity_mapping"][record["id"]["entity_id"]])
    if family == "settings":
        result["after"] = {"settings": {"insunits": INSUNITS[units["drawing_units"]]}}
    else:
        result["after"] = {family: records}
    if family == "strings":
        extra = graph.get("extra", {})
        coverage = extra.get("solve_coverage") if isinstance(extra, dict) else None
        if not isinstance(coverage, dict):
            raise compare.InputError("strings evidence requires committed solve_coverage")
        for field in ("unassigned", "duplicate"):
            refs = coverage.get(field + "_panel_refs")
            if not isinstance(refs, list):
                raise compare.InputError("solve_coverage requires panel reference arrays")
            result["after"][field + "_panels"] = [reference(identifier) for identifier in refs]
        result["after"]["length_distribution"] = sorted(item["module_count"] for item in collection)
    result["output_sha256"] = compare.semantic_hash(result["after"])
    result["provenance"]["studio_graph_sha256"] = compare.semantic_hash(graph)
    result["provenance"]["studio_graph_rev"] = graph["rev"]
    # The frozen comparator calls replayed observations "recorded".
    if result["execution_mode"] == "replay":
        result["execution_mode"] = "recorded"
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
    parser.add_argument("--family", choices=FAMILIES, required=True)
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
