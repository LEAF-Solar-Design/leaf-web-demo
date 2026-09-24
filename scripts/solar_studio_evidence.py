#!/usr/bin/env python3
"""Project Studio graph snapshots into the Solar W1 neutral evidence contract.

The graph supplies frames (groups), panels and drawing frame. A
separate --metadata JSON supplies measured run facts: the evidence keys other
than after, output_sha256, input_sha256, units and frame, plus coordinate_system,
geometry_units and angle_units. In particular before, changes, identity mapping
and survived_reopen must come from the producer, not from this snapshot reader.
geometry_units and angle_units declare the graph geometry's actual units.
The groups family follows the joint contract's v2 amendment: the compared name
is the group's neutral id, membership sorts by handle value, the four group
semantics the plugin capture cannot record are null and named in
fallback_fields, and Studio's own frame labels go in provenance.group_names.
The zones family differs from groups in exactly one way that matters: a zone's
NAME is committed state the plugin chose (LEAFADDZONE prompts for it and writes
it to the drawing), so it is compared as given rather than replaced by the
neutral id, and the zones are compared in the drawing's own creation order.
Membership still sorts by handle value. Creating a zone and assigning panels to
it commits identity and membership only, so equipment_config and sizing_provenance
are null and named as fallbacks (rule Z7) rather than compared: the equipment and
sizing a zone later carries are set by other capabilities and proven by their own
receipts. Nothing is lost, the zone's own raw fields go to provenance.zone_fields
keyed by zone neutral id, so a later capability can compare them.
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
FAMILIES = ("groups", "panels", "settings", "strings", "zones")
# The graph collection each family reads; every other family is its own key.
COLLECTIONS = {"groups": "frames", "zones": "electrical_zones"}
# Contract v3 rule Z7 (revised): a zone's equipment and sizing fields are NOT
# compared by this family, so nothing here is coerced into a neutral shape. They
# are still preserved verbatim under provenance.zone_fields, because the capture
# is the only record of them until the capability that sets them is receipted. A
# field the zone does not hold is simply absent from that record; an invented null
# would read as "not configured" and lose the difference.
ZONE_RAW_FIELDS = ("module_model", "inverter_model_a", "inverter_model_b", "inverter_count_a",
                   "inverter_count_b", "optimizer_model", "dc_ac_ratio", "panels_in_sequence",
                   "string_sizer_response", "voc_cold")
# The graph schema requires these on every zone, so a missing or wrong-typed one is
# malformed input. Membership and identity depend on a well-formed zone record, so
# this check stays even though the values themselves are no longer compared.
ZONE_REQUIRED = (("module_model", str), ("inverter_model_a", str), ("inverter_count_a", int),
                 ("panels_in_sequence", int), ("dc_ac_ratio", (int, float)), ("voc_cold", dict))


def build_evidence(graph, family, metadata):
    """Build one 22-key evidence object, refusing absent measurement metadata."""
    if family not in FAMILIES:
        raise compare.InputError("Studio adapter supports groups, panels, settings, strings and zones")
    # Input bounds, sized for a real drawing; the evidence built below is still
    # validated under the unchanged comparison bounds.
    graph_sha256 = compare.scan_input(graph)
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
    if (metadata.get("provenance") or {}).get("unassigned_scope") not in (None, "grouped"):
        raise compare.InputError("unassigned_scope must be grouped when given")
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

    def handle_value(neutral):
        # Rule 8: a panel neutral id is its upper-case DWG handle; rule G4 sorts by its value.
        if not isinstance(neutral, str) or not re.fullmatch(r"[0-9A-F]{1,32}", neutral):
            raise compare.InputError("group member neutral id must be an upper-case hex handle")
        return int(neutral, 16)

    group_names = {}
    zone_fields = {}
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
        collection = graph[COLLECTIONS.get(family, family)]
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
        elif family == "zones":
            # A zone's name IS committed state (LEAFADDZONE wrote it), so it is
            # compared as given; membership still sorts by handle value (rule G4).
            members = item.get("panel_refs")
            # An EMPTY list is a real zone: LEAFADDZONE commits a named zone with no panels
            # until LEAFZONEASSIGNPANELS runs. Only a non-list is malformed.
            if not isinstance(members, list):
                raise compare.InputError("zone panel_refs must be an array")
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                raise compare.InputError("zone name must be a nonempty string")
            membership = [reference(identifier) for identifier in members]
            membership.sort(key=lambda ref: handle_value(result["entity_mapping"][ref["entity_id"]]))
            record.update({"name": name, "membership": membership})
            for field, kind in ZONE_REQUIRED:
                if type(item.get(field)) is bool or not isinstance(item.get(field), kind):
                    raise compare.InputError("zone equipment and sizing fields are malformed")
            # Rule Z7: this capability commits identity and membership only, so the
            # equipment and sizing records are not compared here; the raw values are
            # kept verbatim under provenance.zone_fields so nothing is lost.
            zone_fields[result["entity_mapping"][item["id"]]] = {
                field: deepcopy(item[field]) for field in ZONE_RAW_FIELDS if field in item}
            for field in ("equipment_config", "sizing_provenance"):
                record[field] = absent("zones/" + field + "/not-part-of-this-capability")
            for field in ("boundaries", "elevation"):
                record[field] = absent("zones/" + field + "/unrecorded")
        else:
            # Contract v2 (rules G3 to G7): the frame's own label is provenance,
            # the compared name is the neutral id, members sort by handle value.
            members = item.get("panel_refs")
            if not isinstance(members, list) or not members:
                raise compare.InputError("group panel_refs must be a nonempty array")
            if not isinstance(item.get("name"), str):
                raise compare.InputError("group name must be a string")
            neutral = result["entity_mapping"][item["id"]]
            membership = [reference(identifier) for identifier in members]
            membership.sort(key=lambda ref: handle_value(result["entity_mapping"][ref["entity_id"]]))
            group_names[neutral] = item["name"]
            record.update({"name": neutral, "membership": membership})
            for field in ("boundaries", "elevation", "equipment_config", "sizing_provenance"):
                record[field] = absent("groups/" + field + "/unrecorded")
        records.append(record)
    if family in ("groups", "strings"):
        records.sort(key=lambda record: result["entity_mapping"][record["id"]["entity_id"]])
    if family == "settings":
        result["after"] = {"settings": {"insunits": INSUNITS[units["drawing_units"]]}}
    else:
        result["after"] = {family: records}
    if family == "groups":
        result["provenance"]["group_names"] = group_names
    if family == "zones":
        result["provenance"]["zone_fields"] = zone_fields
    if family == "strings":
        extra = graph.get("extra", {})
        coverage = extra.get("solve_coverage") if isinstance(extra, dict) else None
        if not isinstance(coverage, dict):
            raise compare.InputError("strings evidence requires committed solve_coverage")
        # A producer that ran a REMOVEPANEL cut declares provenance.unassigned_scope "grouped": a panel no group
        # holds is then not a stringing target, as the plugin's evidence counts unassigned panels among grouped
        # panels only (plugin_evidence.py, grouped_panels - assigned). Every other producer keeps every panel.
        scope = (metadata.get("provenance") or {}).get("unassigned_scope")
        grouped = ({panel["id"] for panel in graph.get("panels", []) if panel.get("frame_ref") is not None}
                   if scope == "grouped" else None)
        for field in ("unassigned", "duplicate"):
            refs = coverage.get(field + "_panel_refs")
            if not isinstance(refs, list):
                raise compare.InputError("solve_coverage requires panel reference arrays")
            if field == "unassigned" and grouped is not None:
                refs = [ref for ref in refs if ref in grouped]
            panels = [reference(identifier) for identifier in refs]
            # Rule G9: a SET-valued list is emitted in ascending neutral-id order on both
            # sides, by the same key the records above sort by, so two equal sets recorded
            # in different orders compare equal instead of reading as one diff per member.
            # A string's ordered_membership is not a set and keeps the order it was given.
            panels.sort(key=lambda ref: result["entity_mapping"][ref["entity_id"]])
            result["after"][field + "_panels"] = panels
        result["after"]["length_distribution"] = sorted(item["module_count"] for item in collection)
    result["output_sha256"] = compare.semantic_hash(result["after"])
    result["provenance"]["studio_graph_sha256"] = graph_sha256
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
        evidence = build_evidence(compare.load_input_graph(args.graph), args.family, compare.load_evidence(args.metadata))
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
