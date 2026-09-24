#!/usr/bin/env python3
"""Produce Studio's committed auto-fill-then-solve (AutoFillSolve) through its own builtins.

The plugin's AutoFillSolve (Commands.cs:572-578) chains AutoFillAllPanelGroups and then
StartPanelGroupsSolve with the string-sizer popup off: it has no design logic of its own. So this
producer chains the two receipted Studio producers: scripts/solar_w1_studio_autofill.py builds the
groups with the kernel, removes the `--remove` panels (the fixture state the plugin started from:
REMOVEPANEL made one group infeasible) and applies the auto-fill rebalance; then the solve
producer's recorded string-sizing precondition and frame-by-frame solve (split frames piece by
piece) run on that auto-filled graph, live or from recorded responses.

Replay files contain {"responses": [<raw stringer response>, ...]}, each used at most once and
matched to its request; `--record-responses` writes the live responses in that shape.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _sibling(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


autofill = _sibling("solar_w1_studio_autofill")
solve = _sibling("solar_w1_studio_solve")
ProducerError = solve.ProducerError


def produce(args):
    """(reopened graph, metadata); writes the auto-fill stage beside --out-graph and the final graph and
    metadata to their paths. Fails closed on any mismatch the sibling producers refuse."""
    started = time.monotonic()
    stage_graph = args.out_graph.with_name(args.out_graph.stem + ".autofill.json")
    stage_meta = args.out_metadata.with_name(args.out_metadata.stem + ".autofill.json")
    af_args = argparse.Namespace(
        fixture=args.fixture, intake=args.intake, out_graph=stage_graph, out_metadata=stage_meta,
        branch_max_offset=args.branch_max_offset, alignment_tolerance=args.alignment_tolerance,
        layer_contains=args.layer_contains, installation_design=args.installation_design,
        max_string_length=args.max_string_length, remove=list(args.remove), revert=False)
    graph, af_meta = autofill.produce(af_args)
    sizing_record, sizing_hash = solve.read_json(args.sizing_response)
    standard = sizing_record["response"]["simulation_results"]["standard"]
    if type(standard["string_length"]) not in (int, float) or int(standard["string_length"]) != args.max_string_length:
        raise ProducerError("sizing response must recommend the requested max string length")
    tenant = args.tenant or "studio-replay"
    graph = solve.apply_recorded_sizing(graph, sizing_record, tenant)
    by_handle = {solve.normalized_handle(panel["provenance"]["source_handle"]): panel for panel in graph["panels"]}
    replay_hash, responses = None, None
    if args.replay:
        document, replay_hash = solve.read_json(args.replay)
        responses = solve.replay_responses(document, by_handle)
    frames = [{"id": frame["id"], "name": frame["name"]} for frame in graph["frames"]]
    graph, recorded, response_hashes, split_frames = solve.solve_frames(
        graph, frames, maximum=args.max_string_length, dwgname=args.dwgname, tenant=tenant,
        grant_ref=args.grant_ref, replay=responses)
    reopened = solve.deserialize_graph(solve.serialize_graph(graph))
    solve.validate_graph(reopened)
    coverage = reopened["extra"]["solve_coverage"]
    grouped = {p["id"] for p in reopened["panels"] if p.get("frame_ref") is not None}
    if coverage["duplicate_panel_refs"] or set(coverage["unassigned_panel_refs"]) & grouped:
        raise ProducerError("committed solve has unassigned or duplicate grouped panels")
    # Only grouped panels are in the plugin's evidence: a REMOVEPANEL cut leaves no group holding a panel.
    mapping = {p["id"]: solve.normalized_handle(p["provenance"]["source_handle"]) for p in reopened["panels"]
               if p.get("frame_ref") is not None}
    mapping.update({s["id"]: "string:" + mapping[s["ordered_panel_refs"][0]] for s in reopened["strings"]})
    synthetic = ["before", "changes", "versions.solver"]
    if sizing_record.get("synthetic") is True:
        synthetic.append("settings/string_sizing")
    metadata = {
        "fixture_sha256": af_meta["fixture_sha256"], "revision": af_meta["revision"],
        "parameters": {"family": "strings", "max_string_length": args.max_string_length},
        "versions": {"schema": "leaf.solar-w1-comparison.v1", "producer": "solar_w1_studio_autofill_solve.v1",
                     "capability": "0", "engine": "server-builtin", "catalog": "none",
                     "solver": "leaf-stringer-service"},
        "coordinate_system": "world", "geometry_units": "in", "angle_units": "deg",
        "entity_mapping": mapping, "before": {"recorded": False},
        "changes": {"created": [], "modified": [], "deleted": []}, "warnings": [], "rejected_inputs": [],
        "elapsed_ms": (time.monotonic() - started) * 1000,
        "execution_mode": "replay" if args.replay else "live", "state": "committed",
        "survived_reopen": True, "synthetic_fields": synthetic,
        "fallback_fields": ["panels/producer-built-from-intake", "frames/kernel-grouped",
                            "frames/producer-derived-dimensions", "frames/module_power_watts/unrecorded"],
        "synthetic_flagged": True,
        "provenance": {"autofill": {key: af_meta["provenance"].get(key) for key in
                                    ("corrections", "panels_removed", "panels_moved", "groups_modified",
                                     "removed", "solver", "intake_sha256")},
                       "sizing_response_sha256": sizing_hash, "response_sha256s": response_hashes,
                       "split_frames": split_frames, "unassigned_scope": "grouped"},
    }
    if replay_hash:
        metadata["provenance"]["replay_sha256"] = replay_hash
    if args.record_responses:
        args.record_responses.write_text(json.dumps({"responses": recorded}, indent=2, allow_nan=False) + "\n",
                                         encoding="utf-8")
    args.out_graph.write_text(solve.serialize_graph(reopened) + "\n", encoding="utf-8")
    args.out_metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True, allow_nan=False) + "\n",
                                 encoding="utf-8")
    return reopened, metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("fixture", "intake", "sizing-response", "out-graph", "out-metadata"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--branch-max-offset", type=float, required=True)
    parser.add_argument("--alignment-tolerance", type=float, required=True)
    parser.add_argument("--layer-contains", required=True)
    parser.add_argument("--installation-design", required=True)
    parser.add_argument("--max-string-length", type=int, required=True)
    parser.add_argument("--remove", action="append", default=[],
                        help="source handle of a panel removed before the auto-fill; repeatable")
    parser.add_argument("--dwgname", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", type=Path)
    mode.add_argument("--grant-ref")
    parser.add_argument("--tenant")
    parser.add_argument("--record-responses", type=Path)
    args = parser.parse_args(argv)
    if args.grant_ref and not args.tenant:
        parser.error("live mode requires --tenant")
    if args.replay and (args.tenant or args.record_responses):
        parser.error("--tenant and --record-responses require live mode")
    try:
        produce(args)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError) as exc:
        # Never print an input object, request, grant reference or provider body.
        bounded = isinstance(exc, (ProducerError, autofill.ProducerError))
        message = str(exc) if bounded else getattr(exc, "code", None)
        message = message or getattr(exc, "classification", "invalid producer input or output")
        print(f"solar-w1-studio-autofill-solve: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
