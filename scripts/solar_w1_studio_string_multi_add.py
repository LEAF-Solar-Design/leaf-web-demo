#!/usr/bin/env python3
"""Produce Studio's committed MULTISTRING: solve, delete named strings, re-solve a sub-matrix.

MULTISTRING is NOT the single-string add in a loop. Read from the licensed source
(Commands.cs:4402, BranchCmd.cs:17197 AddMultiString -> 17212 AddMultiStringAsync ->
17333 SolveManualStringAsync(useSingleString: false)), the command builds a TEMPORARY
PanelGroup from the selected panels (17299-17305), builds that group's panel matrix,
computes the string combination with StringComboCalculator.StringComboFunc
(LeafSolarDesign.Core/BranchCmdCore.cs:3173, ported at server/solar_string_combo.py),
writes `Sequences = [firstLen, secondLen, firstQ, secondQ]` (17446), and sends the
matrix to the SAME stringer the whole-drawing solve uses. The selection ORDER is
therefore discarded: the cut that comes back is the solver's serpentine over the
sub-matrix, and the count of circuits is the combination's, not the caller's.

Captured on licensed AutoCAD 2025 on 2026-09-22 (receipts/w2-string-multi-add-20260922):
strings A662 (14 panels) and A6BA (13 panels) were deleted on the solved rooftop
drawing, MULTISTRING ran over all 27 freed panels, and the plugin committed two NEW
strings of 14 and 13 whose memberships are neither of the originals. StringComboFunc
answers (14, 27) with one string of 14 and one of 13, which is exactly that.

This producer starts from the state the plugin started from, in the same steps: the
solve half IS solar_w1_studio_solve and the delete half IS solar_w1_studio_string_delete,
both imported as siblings and run with the same intake binding, the same recorded
responses and the same revision rules. Only the sub-matrix solve and the metadata are
new here.

Naming follows the other string producers: a circuit is named for deletion by its
NEUTRAL id `string:<handle>`, and a panel is named for the selection by its own DWG
HANDLE. `--add` is a SET, unlike the single-string add where the order is the output,
so the handles are sorted for the record and the matrix decides what gets strung
together.

The sub-matrix call replays from `--multi-replay` or runs live under the solve
producer's own grant handling, and a live run writes what it received to
`--record-multi` so a later run can replay it byte for byte.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

from leaf_cloud_grants import CloudGrant
import solar_string_combo as combo
from solar_design_graph import deserialize_graph, serialize_graph, validate_graph
from solar_grid_restitch import ordered_strings

PRODUCER = "solar_w1_studio_string_multi_add.v1"
# One AutoCAD selection set of panels; the stringer's own grid bound below is the
# real cap and the wire contract enforces it.
MAX_SELECTED = 900
# leaf_cloud_client.MatrixJson: a whole-frame grid is at most 30 rows of 30 panels.
# The plugin splits a group bigger than that; a selection that large is a different
# capability and is refused here rather than silently truncated.
MAX_GRID = 30
# One stringer call, so one job id; the service echoes it back as best_result.grid_id.
MULTI_JOB = "w1-multi-string"


class ProducerError(ValueError):
    """A bounded, payload-free producer refusal."""


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The solve and the delete halves of this run ARE the producers that own them: same
# intake binding, same recorded responses, same entity identities, same builtin
# commits, same neutral ids. `solve` is the delete producer's own module object, so a
# patch on it reaches both, and `cloud` is the client object those producers already
# hold, so a transport patch here is the same patch the solve producer makes.
deleter = _sibling("solar_w1_studio_string_delete")
solve = deleter.solve
cloud = solve.cloud
builtin = solve.builtin
normalized_handle = solve.normalized_handle
fixture_revision = solve.fixture_revision
neutral_string_id = deleter.neutral_string_id
deletion_plan = deleter.deletion_plan
commit_solve = deleter.commit_solve


def evidence_mapping(graph):
    """Rule G8: every panel the strings evidence names, and every circuit it holds.

    The added circuits are committed state, so they ARE in the mapping; the deleted
    ones are not, and their panels still are. One bounded pass.
    """
    mapping = {panel["id"]: normalized_handle(panel["provenance"]["source_handle"])
               for panel in graph["panels"]}
    mapping.update({string["id"]: "string:" + mapping[string["ordered_panel_refs"][0]]
                    for string in graph["strings"]})
    return mapping


def selection_plan(freed, handles):
    """The panel ids for already-normalized handles, as a SET, or a bounded refusal.

    Fails closed BEFORE the stringer is called: an unknown handle or a panel a
    surviving circuit still wires is a caller error the drawing cannot express,
    exactly as it is for the single-string add. One pass over the panels, one over
    the strings and one over the request: never a graph scan per handle.
    """
    by_handle = {normalized_handle(panel["provenance"]["source_handle"]): panel["id"]
                 for panel in freed["panels"]}
    if len(by_handle) != len(freed["panels"]):
        raise ProducerError("two panels share a source handle")
    wired = {ref for string in freed["strings"] for ref in string["ordered_panel_refs"]}
    refs = []
    for handle in handles:
        identifier = by_handle.get(handle)
        if identifier is None:
            raise ProducerError("the selection names a panel absent from the drawing")
        if identifier in wired:
            # The plugin drops an already-wired panel from its own selection
            # (GetPanelToStringMap), so reaching one here is a caller error.
            raise ProducerError("the selection names a panel a circuit already wires")
        refs.append(identifier)
    return refs


def submatrix(freed, selected):
    """The temporary PanelGroup's matrix (BranchCmd.cs:17299-17309), over the placement.

    Studio's layout is the recorded licensed placement, so the sub-matrix is cut from
    the frames the selection touches rather than rebuilt from drawing geometry: every
    row and column holding no selected panel drops out, which is the same compaction
    `StringerRequest.wire_payload` applies to the grid before it goes on the wire, and
    a selection spanning frames stacks its blocks in frame order. Cells the selection
    did not name become empty, so the solver sees the selection and nothing else.

    One pass per frame matrix, no rescan per panel, and the result is bounded by the
    stringer's own 30 by 30 grid.
    """
    blocks, matched = [], 0
    for frame in freed["frames"]:
        rows = [[cell if cell["panel_ref"] in selected else None for cell in row]
                for row in frame["matrix"]]
        keep_rows = [index for index, row in enumerate(rows)
                     if any(cell is not None for cell in row)]
        if not keep_rows:
            continue
        keep_columns = [column for column in range(len(rows[0]))
                        if any(rows[index][column] is not None for index in keep_rows)]
        block = [[rows[index][column] for column in keep_columns] for index in keep_rows]
        matched += sum(1 for row in block for cell in row if cell is not None)
        blocks.append(block)
    if matched != len(selected):
        raise ProducerError("the selection names a panel no group's matrix holds")
    width = max((len(block[0]) for block in blocks), default=0)
    cells = [row + [None] * (width - len(row)) for block in blocks for row in block]
    if not cells or len(cells) > MAX_GRID or width > MAX_GRID:
        raise ProducerError("the selection does not fit the stringer's 30 by 30 grid")
    return cells


def multi_request(freed, selected, *, max_string_length, dwgname):
    """The MULTISTRING request: the sub-matrix, sized by StringComboFunc.

    Returns the request and the flat Sequences the plugin writes at BranchCmd.cs:17446.
    The zero sentinel is refused here because the plugin refuses it too: 17396 shows
    "MultiString - Invalid Panel Count" and returns without touching the drawing.
    """
    sequences = combo.sequences(max_string_length, len(selected))
    if combo.is_sentinel([sequences[:2], sequences[2:]]):
        raise ProducerError("the selected panel count cannot be strung at this length")
    panels = {panel["id"]: panel for panel in freed["panels"]}
    rows = []
    for row in submatrix(freed, selected):
        wire_cells = []
        for cell in row:
            wire = {"Code": 0, "Id": "", "Seq": 0, "InverterId": -1,
                    "StringInputNumber": 0, "X": 0.0, "Y": 0.0, "Angle": 0.0}
            if cell is not None:
                panel = panels[cell["panel_ref"]]
                wire.update(Code=1, Id=panel["id"], X=panel["centre"][0],
                            Y=panel["centre"][1], Angle=panel["angle"])
            wire_cells.append(wire)
        rows.append({"Panels": wire_cells})
    request = {"grid": {"Dwgname": dwgname, "Sequences": sequences, "Rows": rows, "Modify": []}}
    try:
        cloud.StringerRequest.model_validate(request)
    except (ValueError, TypeError):
        # Never surface a validator report: it echoes the request back.
        raise ProducerError("the sub-matrix is not a valid stringer grid") from None
    return request, sequences


def solve_submatrix(args, freed, selected, *, tenant):
    """One stringer call over the sub-matrix, through the solve producer's own client.

    Replay takes the one recorded response echoing the sent grid; live resolves the
    real grant. Either way the answer goes through `cloud.proposal`, so the visited
    path, the echo, the job id and the sequence lengths are checked against the
    request before anything is committed.
    """
    request, sequences = multi_request(freed, selected,
                                       max_string_length=args.max_string_length,
                                       dwgname=args.dwgname)
    responses, replay_hash, used = [], None, set()
    if args.multi_replay:
        document, replay_hash = solve.read_json(args.multi_replay)
        responses = solve.replay_responses(document, {
            normalized_handle(panel["provenance"]["source_handle"]): panel
            for panel in freed["panels"]})
    bodies, hashes = [], []
    original_post = cloud.post_stringer

    def transport(sent, grant):
        raw = (solve.matching_response(sent, responses, used) if args.multi_replay
               else original_post(sent, grant))
        bodies.append(json.loads(raw))
        hashes.append(solve.sha256(raw))
        return raw

    # Replay resolves the grant exactly as the live path does; live uses the real one.
    grant = (patch.object(cloud, "resolve_grant", lambda *a: CloudGrant(tenant, ""))
             if args.multi_replay else nullcontext())
    with patch.object(cloud, "post_stringer", transport), grant:
        proposal = cloud.proposal(
            {"grant_ref": args.grant_ref or "recorded-multi-string", "request": request},
            tenant, MULTI_JOB)
    return {"request": request, "sequences": sequences, "proposal": proposal,
            "bodies": bodies, "hashes": hashes, "replay_sha256": replay_hash}


def cut_strings(proposal, selected):
    """The circuits the service cut over the sub-matrix, as ordered panel ids.

    ordered_strings is StringPlacement.DrawAllSequencesCore's own cut: final-grid Seq
    order, sliced by best_result.info.sequence_length. It hands back upper-cased ids
    because the plugin commits upper-cased handles, so they are mapped back here the
    same way the split solve commit does.
    """
    try:
        cut = ordered_strings(proposal["proposal"])
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        raise ProducerError("the stringer answer has no readable final-grid order") from None
    by_upper = {ref.upper(): ref for ref in selected}
    if len(by_upper) != len(selected):
        raise ProducerError("two selected panels share an identity")
    ordered = [[by_upper.get(identity) for identity in members] for members in cut]
    flat = [ref for members in ordered for ref in members]
    if (not ordered or any(not members for members in ordered) or None in flat
            or len(set(flat)) != len(flat) or set(flat) != selected):
        # Every selected panel, exactly once, and nothing else: the plugin's
        # MULTISTRING commits the whole temporary group or nothing.
        raise ProducerError("the cut does not cover every selected panel exactly once")
    return ordered


def commit_strings(graph, ordered):
    """Commit each cut circuit through the string-add builtin, in cut order.

    The builtin is the one that keeps every redundant membership view and
    extra.solve_coverage consistent, so the panels this wires stop being reported
    unassigned and every other circuit keeps every field it had.
    """
    adder = builtin("solar_string_add")
    committed = []
    for members in ordered:
        result = adder.add_string(graph, {"expected_rev": graph["rev"],
                                          "ordered_panel_refs": members})
        graph = result["graph"]
        committed.append(result["string_ref"])
    return graph, committed


def produce(args):
    started = time.monotonic()
    requested_delete, requested_add = args.delete, args.add
    if not isinstance(requested_delete, list) or not requested_delete:
        raise ProducerError("delete requires at least one string id")
    if not isinstance(requested_add, list) or not 1 <= len(requested_add) <= MAX_SELECTED:
        raise ProducerError("the selection requires at least one panel handle")
    # Refuse a malformed or repeated flag before the solve runs: a bad flag costs no
    # solver work. The plans below check both again against the committed drawing.
    named = [neutral_string_id(value) for value in requested_delete]
    # The selection is a SET (the matrix decides the cut, not the pick order), so the
    # handles are sorted once here and that order is what the record carries.
    members = sorted({normalized_handle(value) for value in requested_add})
    if len(set(named)) != len(named):
        raise ProducerError("a string may be named for deletion only once")
    if len(members) != len(requested_add):
        raise ProducerError("a panel may be named for the selection only once")
    tenant = args.tenant or "studio-replay"
    with tempfile.TemporaryDirectory(prefix="solar-w1-string-multi-add-") as scratch:
        solved, metadata = commit_solve(args, Path(scratch))
    refs = deletion_plan(solved, named)
    deletion = builtin("solar_string_delete").delete_strings(
        solved, {"expected_rev": solved["rev"], "string_refs": refs})
    freed = deletion["graph"]
    selected = set(selection_plan(freed, members))
    call = solve_submatrix(args, freed, selected, tenant=tenant)
    ordered = cut_strings(call["proposal"], selected)
    added_graph, committed = commit_strings(freed, ordered)
    reopened = deserialize_graph(serialize_graph(added_graph))
    validate_graph(reopened)
    survivors = {string["id"]: string for string in freed["strings"]}
    # Hoisted: one set for both passes below, never rebuilt per circuit.
    committed_ids = set(committed)
    added = [string for string in reopened["strings"] if string["id"] in committed_ids]
    if len(added) != len(ordered) or len(reopened["strings"]) != len(survivors) + len(ordered):
        raise ProducerError("the multi-add must commit exactly the circuits the solver cut")
    if [string["ordered_panel_refs"] for string in added] != ordered:
        raise ProducerError("a committed circuit does not carry the cut it was given")
    for string in reopened["strings"]:
        if string["id"] not in committed_ids and string != survivors.get(string["id"]):
            # MULTISTRING draws its own polylines and touches no other circuit.
            raise ProducerError("an existing circuit was rewritten by the multi-add")
    if {panel["id"] for panel in reopened["panels"]} != {panel["id"] for panel in solved["panels"]}:
        raise ProducerError("the multi-add must keep every panel it started with")
    if [frame["id"] for frame in reopened["frames"]] != [frame["id"] for frame in solved["frames"]]:
        raise ProducerError("the multi-add must leave every group in place")
    lengths = sorted(len(cut) for cut in ordered)
    expected = sorted([call["sequences"][0]] * call["sequences"][2]
                      + [call["sequences"][1]] * call["sequences"][3])
    if lengths != expected:
        # The combination IS the contract with the solver: what came back must be the
        # string sizes StringComboFunc asked for.
        raise ProducerError("the committed circuits do not match the computed combination")
    unassigned = set(deletion["panels_freed"]) - selected
    coverage = reopened["extra"]["solve_coverage"]
    if coverage["duplicate_panel_refs"] or set(coverage["unassigned_panel_refs"]) != unassigned:
        raise ProducerError("solve coverage does not match the circuits that remain")
    mapping = evidence_mapping(reopened)
    if len(set(mapping.values())) != len(mapping):
        raise ProducerError("entity mapping must be one-to-one")
    handles = {panel["id"]: normalized_handle(panel["provenance"]["source_handle"])
               for panel in reopened["panels"]}
    added_ids = ["string:" + handles[cut[0]] for cut in ordered]
    named_entities = set(mapping.values())
    if any(identifier not in named_entities for identifier in added_ids):
        # Rule G8 as an output check: every committed circuit is named by the evidence.
        raise ProducerError("an added string has no entity in the mapping")
    metadata = deepcopy(metadata)
    metadata["versions"] = {**metadata["versions"], "producer": PRODUCER,
                            # The LEDGER's capability_version for string-multi-add.
                            "capability": "0"}
    metadata["entity_mapping"] = mapping
    metadata["elapsed_ms"] = (time.monotonic() - started) * 1000
    metadata["provenance"].update(
        # The run solved first, then freed the panels it re-strung: these say how many
        # circuits it committed, which ones it erased, what combination it asked the
        # solver for, what came back, and how many panels it left unwired.
        strings_solved=len(solved["strings"]), strings_deleted=len(refs),
        deleted_strings=sorted(named), selected_panels=list(members),
        selected_count=len(members), multi_sequences=list(call["sequences"]),
        multi_response_sha256s=list(call["hashes"]), added_strings=added_ids,
        added_lengths=lengths, strings_remaining=len(reopened["strings"]),
        panels_unassigned=len(unassigned))
    if call["replay_sha256"]:
        metadata["provenance"]["multi_replay_sha256"] = call["replay_sha256"]
    if args.record_multi:
        args.record_multi.write_text(json.dumps({"responses": call["bodies"]}, indent=2,
                                                allow_nan=False) + "\n", encoding="utf-8")
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
    parser.add_argument("--multi-replay", type=Path,
                        help="recorded stringer responses for the sub-matrix call")
    parser.add_argument("--record-multi", type=Path,
                        help="write the sub-matrix call's responses here (live mode only)")
    parser.add_argument("--delete", action="append", default=[], required=True,
                        help="neutral id (string:<handle>) of a circuit to delete; repeatable")
    parser.add_argument("--add", action="append", default=[], required=True,
                        help="DWG handle of a panel to re-string; repeatable, a SET")
    args = parser.parse_args(argv)
    if args.grant_ref and not args.tenant:
        parser.error("live mode requires --tenant")
    if args.replay and (args.tenant or args.record_responses):
        parser.error("--tenant and --record-responses require live mode")
    if args.replay and not args.multi_replay:
        # A replayed solve that reached the network for its sub-matrix would be a
        # half-offline run, which is not a mode this producer has.
        parser.error("--replay requires --multi-replay")
    if args.record_multi and args.multi_replay:
        parser.error("--record-multi records a live sub-matrix call")
    try:
        produce(args)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError) as exc:
        # Never print an input object, request, grant reference or provider body.
        # The solve and delete producers and the combo port raise their OWN bounded
        # classes; those messages pass through unchanged.
        bounded = isinstance(exc, (ProducerError, deleter.ProducerError, solve.ProducerError,
                                   combo.StringComboError))
        message = str(exc) if bounded else getattr(exc, "code", None)
        message = message or getattr(exc, "classification", "invalid producer input or output")
        print(f"solar-w1-studio-string-multi-add: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
