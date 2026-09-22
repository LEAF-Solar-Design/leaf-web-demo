#!/usr/bin/env python3
"""Produce Studio's committed string delete: solve the drawing, then delete named strings.

DeleteString asks for string polylines, so a run that never solved anything has
no cable to select. This producer therefore starts from the same committed state
the plugin started from: the solve half IS solar_w1_studio_solve, imported as a
sibling module and run with the same intake binding, the same recorded responses
and the same revision rules, so the panels, the groups, the sizing and the strings
are the ones that producer commits. server/builtins/solar_string_delete.py then
deletes exactly the circuits `--delete` names.

A circuit is named by its NEUTRAL id, `string:<handle>`, where the handle is the
source handle of the cable's first panel: that is the id the solve producer puts
in its entity mapping and the id a receipt compares, so the flag names what the
evidence names rather than a graph uuid that changes every run.

The provenance records which circuits were deleted, how many remain and how many
panels the delete left unwired, so the receipt shows the run solved first and
shows exactly which membership went away.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

from solar_design_graph import deserialize_graph, serialize_graph, validate_graph

PRODUCER = "solar_w1_studio_string_delete.v1"
MAX_DELETED = 4096


class ProducerError(ValueError):
    """A bounded, payload-free producer refusal."""


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The solve half of this run IS the solve producer: same intake binding, same
# recorded responses, same entity identities, same builtin commits. Only the
# delete and the metadata are new here.
solve = _sibling("solar_w1_studio_solve")
builtin = solve.builtin
normalized_handle = solve.normalized_handle
fixture_revision = solve.fixture_revision
# The solve producer's own CLI fields, in the order its parser declares them.
SOLVE_FIELDS = ("fixture", "intake", "placement", "sizing_response", "max_string_length",
                "dwgname", "replay", "grant_ref", "tenant", "record_responses")


def neutral_string_id(value):
    """`string:<handle>` with the handle normalized, or a bounded refusal."""
    if not isinstance(value, str) or not value.startswith("string:"):
        raise ProducerError("a deleted string is named string:<handle>")
    return "string:" + normalized_handle(value[len("string:"):])


def evidence_mapping(graph):
    """Rule G8: every panel the strings evidence names, and the circuits that survive.

    The strings evidence references a surviving circuit and its ordered membership,
    and it references every remaining panel through after/unassigned_panels, so the
    mapping carries every panel and exactly the strings that are still there. A
    deleted circuit drops OUT of the mapping while its panels stay. One bounded pass.
    """
    mapping = {panel["id"]: normalized_handle(panel["provenance"]["source_handle"])
               for panel in graph["panels"]}
    mapping.update({string["id"]: "string:" + mapping[string["ordered_panel_refs"][0]]
                    for string in graph["strings"]})
    return mapping


def deletion_plan(solved, named):
    """The graph string ids for already-normalized neutral ids, in request order.

    One pass over the panels, one over the strings and one over the request: never a
    graph scan per id.
    """
    handles = {panel["id"]: normalized_handle(panel["provenance"]["source_handle"])
               for panel in solved["panels"]}
    by_neutral = {"string:" + handles[string["ordered_panel_refs"][0]]: string["id"]
                  for string in solved["strings"]}
    if len(by_neutral) != len(solved["strings"]):
        raise ProducerError("two circuits share a neutral id")
    refs, seen = [], set()
    for neutral in named:
        if neutral in seen:
            raise ProducerError("a string may be named for deletion only once")
        seen.add(neutral)
        identifier = by_neutral.get(neutral)
        if identifier is None:
            raise ProducerError("delete names a string absent from the solved drawing")
        refs.append(identifier)
    return refs


def commit_solve(args, folder):
    """The committed solved state the plugin's delete starts from, the solve producer's own."""
    values = {name: getattr(args, name) for name in SOLVE_FIELDS}
    # The solve producer writes its own outputs; they are scratch here, so a failed
    # delete never leaves a half-written graph beside a metadata file.
    solved, metadata = solve.produce(SimpleNamespace(
        out_graph=folder / "solved-graph.json", out_metadata=folder / "solved-metadata.json",
        **values))
    if not solved["strings"]:
        raise ProducerError("the solve committed no strings to delete")
    return solved, metadata


def produce(args):
    started = time.monotonic()
    requested = args.delete
    if not isinstance(requested, list) or not 1 <= len(requested) <= MAX_DELETED:
        raise ProducerError("delete requires at least one string id")
    # Refuse a malformed or repeated id before the solve runs: a bad flag costs no
    # solver work. deletion_plan checks both again against the committed strings.
    named = [neutral_string_id(value) for value in requested]
    if len(set(named)) != len(named):
        raise ProducerError("a string may be named for deletion only once")
    with tempfile.TemporaryDirectory(prefix="solar-w1-string-delete-") as scratch:
        solved, metadata = commit_solve(args, Path(scratch))
    refs = deletion_plan(solved, named)
    deleter = builtin("solar_string_delete")
    deletion = deleter.delete_strings(solved, {"expected_rev": solved["rev"], "string_refs": refs})
    reopened = deserialize_graph(serialize_graph(deletion["graph"]))
    validate_graph(reopened)
    erased = set(refs)  # hoisted: one set, never rebuilt per circuit
    survivors = {string["id"]: string for string in solved["strings"] if string["id"] not in erased}
    if {string["id"] for string in reopened["strings"]} != set(survivors):
        raise ProducerError("the delete removed a circuit it was not asked to remove")
    for string in reopened["strings"]:
        if string != survivors[string["id"]]:
            # The plugin erases the selected cables and touches no other entity.
            raise ProducerError("a surviving circuit was rewritten by the delete")
    if {panel["id"] for panel in reopened["panels"]} != {panel["id"] for panel in solved["panels"]}:
        raise ProducerError("the delete must keep every panel it started with")
    if [frame["id"] for frame in reopened["frames"]] != [frame["id"] for frame in solved["frames"]]:
        raise ProducerError("the delete must leave every group in place")
    freed = set(deletion["panels_freed"])
    coverage = reopened["extra"]["solve_coverage"]
    if coverage["duplicate_panel_refs"] or set(coverage["unassigned_panel_refs"]) != freed:
        raise ProducerError("solve coverage does not match the circuits that remain")
    mapping = evidence_mapping(reopened)
    if len(set(mapping.values())) != len(mapping):
        raise ProducerError("entity mapping must be one-to-one")
    if any(identifier in mapping for identifier in refs):
        # Rule G8 as an output check: the evidence names no deleted circuit.
        raise ProducerError("a deleted string still has an entity in the mapping")
    metadata = deepcopy(metadata)
    metadata["versions"] = {**metadata["versions"], "producer": PRODUCER,
                            # The LEDGER's capability_version for string-delete.
                            "capability": "0"}
    metadata["entity_mapping"] = mapping
    metadata["elapsed_ms"] = (time.monotonic() - started) * 1000
    metadata["provenance"].update(
        # The run solved first: these say how many circuits it committed, which ones
        # the delete erased, and how many panels it left unwired.
        strings_solved=len(solved["strings"]), strings_deleted=len(refs),
        deleted_strings=sorted(named), strings_remaining=len(reopened["strings"]),
        panels_unassigned=len(freed))
    args.out_graph.write_text(serialize_graph(reopened) + "\n", encoding="utf-8")
    args.out_metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True,
                                            allow_nan=False) + "\n", encoding="utf-8")
    return reopened, metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("fixture", "intake", "placement", "sizing-response", "out-graph", "out-metadata"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--max-string-length", type=int, required=True)
    parser.add_argument("--dwgname", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", type=Path)
    mode.add_argument("--grant-ref")
    parser.add_argument("--tenant")
    parser.add_argument("--record-responses", type=Path)
    parser.add_argument("--delete", action="append", default=[], required=True,
                        help="neutral id (string:<handle>) of a circuit to delete; repeatable")
    args = parser.parse_args(argv)
    if args.grant_ref and not args.tenant:
        parser.error("live mode requires --tenant")
    if args.replay and (args.tenant or args.record_responses):
        parser.error("--tenant and --record-responses require live mode")
    try:
        produce(args)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError) as exc:
        # Never print an input object, request, grant reference or provider body.
        # The solve producer raises its OWN ProducerError class; those messages are
        # bounded too, so they pass through unchanged.
        bounded = isinstance(exc, (ProducerError, solve.ProducerError))
        message = str(exc) if bounded else getattr(exc, "code", None)
        message = message or getattr(exc, "classification", "invalid producer input or output")
        print(f"solar-w1-studio-string-delete: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
