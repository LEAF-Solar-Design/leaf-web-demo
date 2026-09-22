#!/usr/bin/env python3
"""Produce Studio's committed single-string add: solve, delete named strings, add one.

SINGLESTRING asks for panels that are NOT already wired, so a run that never freed
any has nothing to select. This producer therefore starts from the same committed
state the plugin started from, in the same two steps: the solve half IS
solar_w1_studio_solve and the delete half IS solar_w1_studio_string_delete, both
imported as siblings and run with the same intake binding, the same recorded
responses and the same revision rules. server/builtins/solar_string_add.py then
creates ONE circuit over exactly the panels `--add` names, in exactly that order.

A circuit is named for deletion by its NEUTRAL id, `string:<handle>`, and a panel is
named for the add by its own DWG HANDLE, because that is what the plugin's selection
named and what a receipt compares; neither is a graph uuid that changes every run.

The order of `--add` is the capability's output, not a set, so it is passed through
untouched and the produced graph is checked against it.

The provenance records which circuits were deleted, the added circuit and its
members in order, and the resulting string count, so the receipt shows the run
solved first, freed the panels it used, and added exactly one circuit.
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

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

from solar_design_graph import deserialize_graph, serialize_graph, validate_graph

PRODUCER = "solar_w1_studio_string_add.v1"
# One AutoCAD selection set of panels; the drawing's sized length is the real cap and
# the builtin enforces it against the graph.
MAX_ADDED = 4096


class ProducerError(ValueError):
    """A bounded, payload-free producer refusal."""


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The solve and the delete halves of this run ARE the producers that own them: same
# intake binding, same recorded responses, same entity identities, same builtin
# commits, same neutral ids. Only the add and the metadata are new here, and `solve`
# is the delete producer's own module object, so a patch on it reaches both.
deleter = _sibling("solar_w1_studio_string_delete")
solve = deleter.solve
builtin = solve.builtin
normalized_handle = solve.normalized_handle
fixture_revision = solve.fixture_revision
neutral_string_id = deleter.neutral_string_id
deletion_plan = deleter.deletion_plan
commit_solve = deleter.commit_solve


def evidence_mapping(graph):
    """Rule G8: every panel the strings evidence names, and every circuit it holds.

    The added circuit is committed state, so it IS in the mapping; the deleted ones
    are not, and their panels still are. One bounded pass.
    """
    mapping = {panel["id"]: normalized_handle(panel["provenance"]["source_handle"])
               for panel in graph["panels"]}
    mapping.update({string["id"]: "string:" + mapping[string["ordered_panel_refs"][0]]
                    for string in graph["strings"]})
    return mapping


def addition_plan(solved, handles):
    """The graph panel ids for already-normalized handles, in request order.

    One pass over the panels and one over the request: never a graph scan per handle.
    """
    by_handle = {normalized_handle(panel["provenance"]["source_handle"]): panel["id"]
                 for panel in solved["panels"]}
    if len(by_handle) != len(solved["panels"]):
        raise ProducerError("two panels share a source handle")
    refs = []
    for handle in handles:
        identifier = by_handle.get(handle)
        if identifier is None:
            raise ProducerError("add names a panel absent from the drawing")
        refs.append(identifier)
    return refs


def produce(args):
    started = time.monotonic()
    requested_delete, requested_add = args.delete, args.add
    if not isinstance(requested_delete, list) or not requested_delete:
        raise ProducerError("delete requires at least one string id")
    if not isinstance(requested_add, list) or not 1 <= len(requested_add) <= MAX_ADDED:
        raise ProducerError("add requires at least one panel handle")
    # Refuse a malformed or repeated flag before the solve runs: a bad flag costs no
    # solver work. The plans below check both again against the committed drawing.
    named = [neutral_string_id(value) for value in requested_delete]
    members = [normalized_handle(value) for value in requested_add]
    if len(set(named)) != len(named):
        raise ProducerError("a string may be named for deletion only once")
    if len(set(members)) != len(members):
        raise ProducerError("a panel may be named for the added string only once")
    with tempfile.TemporaryDirectory(prefix="solar-w1-string-add-") as scratch:
        solved, metadata = commit_solve(args, Path(scratch))
    refs = deletion_plan(solved, named)
    deletion = builtin("solar_string_delete").delete_strings(
        solved, {"expected_rev": solved["rev"], "string_refs": refs})
    freed = deletion["graph"]
    panel_refs = addition_plan(freed, members)
    addition = builtin("solar_string_add").add_string(
        freed, {"expected_rev": freed["rev"], "ordered_panel_refs": panel_refs})
    reopened = deserialize_graph(serialize_graph(addition["graph"]))
    validate_graph(reopened)
    survivors = {string["id"]: string for string in freed["strings"]}
    added = next((string for string in reopened["strings"]
                  if string["id"] == addition["string_ref"]), None)
    if added is None or len(reopened["strings"]) != len(survivors) + 1:
        raise ProducerError("the add must commit exactly one new circuit")
    if added["ordered_panel_refs"] != panel_refs:
        # The selection order IS the output; a sorted or reordered membership is a
        # different capability.
        raise ProducerError("the added circuit does not carry the named panels in order")
    for string in reopened["strings"]:
        if string["id"] != added["id"] and string != survivors.get(string["id"]):
            # SINGLESTRING commits one polyline and touches no other circuit.
            raise ProducerError("an existing circuit was rewritten by the add")
    if {panel["id"] for panel in reopened["panels"]} != {panel["id"] for panel in solved["panels"]}:
        raise ProducerError("the add must keep every panel it started with")
    if [frame["id"] for frame in reopened["frames"]] != [frame["id"] for frame in solved["frames"]]:
        raise ProducerError("the add must leave every group in place")
    unassigned = set(deletion["panels_freed"]) - set(panel_refs)
    coverage = reopened["extra"]["solve_coverage"]
    if coverage["duplicate_panel_refs"] or set(coverage["unassigned_panel_refs"]) != unassigned:
        raise ProducerError("solve coverage does not match the circuits that remain")
    mapping = evidence_mapping(reopened)
    if len(set(mapping.values())) != len(mapping):
        raise ProducerError("entity mapping must be one-to-one")
    if mapping.get(added["id"]) != "string:" + members[0]:
        # Rule G8 as an output check: the added circuit is named by the evidence, by
        # the handle of the first panel the selection named.
        raise ProducerError("the added string has no entity in the mapping")
    metadata = deepcopy(metadata)
    metadata["versions"] = {**metadata["versions"], "producer": PRODUCER,
                            # The LEDGER's capability_version for string-single-add.
                            "capability": "0"}
    metadata["entity_mapping"] = mapping
    metadata["elapsed_ms"] = (time.monotonic() - started) * 1000
    metadata["provenance"].update(
        # The run solved first, then freed the panels it used: these say how many
        # circuits it committed, which ones it erased, what it added and in what
        # order, and how many panels it left unwired.
        strings_solved=len(solved["strings"]), strings_deleted=len(refs),
        deleted_strings=sorted(named), added_string="string:" + members[0],
        added_panels=list(members), added_length=len(members),
        strings_remaining=len(reopened["strings"]), panels_unassigned=len(unassigned))
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
    parser.add_argument("--add", action="append", default=[], required=True,
                        help="DWG handle of a panel for the new string; repeatable, ORDER MATTERS")
    args = parser.parse_args(argv)
    if args.grant_ref and not args.tenant:
        parser.error("live mode requires --tenant")
    if args.replay and (args.tenant or args.record_responses):
        parser.error("--tenant and --record-responses require live mode")
    try:
        produce(args)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError) as exc:
        # Never print an input object, request, grant reference or provider body.
        # The solve and delete producers raise their OWN ProducerError classes; those
        # messages are bounded too, so they pass through unchanged.
        bounded = isinstance(exc, (ProducerError, deleter.ProducerError, solve.ProducerError))
        message = str(exc) if bounded else getattr(exc, "code", None)
        message = message or getattr(exc, "classification", "invalid producer input or output")
        print(f"solar-w1-studio-string-add: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
