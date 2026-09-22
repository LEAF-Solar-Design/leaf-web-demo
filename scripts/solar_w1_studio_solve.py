#!/usr/bin/env python3
"""Produce Studio's W1 committed solve through its own graph and solve builtins.

Replay files contain {"responses": [<raw stringer response>, ...]}. CAD handles
in recordings are remapped to the panels imported from the bound intake. Live
recordings retain raw response bodies and use stable fixture-owned panel ids.
Sizing always uses the supplied recording, including in live solve mode.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from unittest.mock import patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

import leaf_cloud_client as cloud
from leaf_cloud_grants import CloudGrant
from mutation_plan import plan_sha256
from solar_design_graph import deserialize_graph, serialize_graph, validate_graph
from solar_graph_seed import new_empty_graph
import solar_sizing_client as sizing_cloud
from solar_solve_request import build_stringer_request
from solar_solve_results import bind_request, complete_search


class ProducerError(ValueError):
    """A bounded, payload-free producer refusal."""


def builtin(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / "builtins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path):
    raw = path.read_bytes()
    return json.loads(raw), sha256(raw)


def normalized_handle(handle):
    if not isinstance(handle, str) or not re.fullmatch(r"[0-9a-fA-F]{1,32}", handle):
        raise ProducerError("invalid source handle")
    return handle.upper().lstrip("0") or "0"


def fixture_revision(fixture):
    """Use the containing repository and the fixture's last committed revision."""
    def git(*args, cwd):
        try:
            result = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                    text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ProducerError("fixture revision unavailable: git required") from None
        if result.returncode:
            raise ProducerError("fixture revision unavailable: fixture must be tracked in git")
        return result.stdout.strip()

    root = Path(git("rev-parse", "--show-toplevel", cwd=fixture.parent))
    try:
        relative = fixture.relative_to(root.resolve()).as_posix()
    except ValueError:
        raise ProducerError("fixture must belong to its git repository") from None
    git("ls-files", "--error-unmatch", "--", relative, cwd=root)
    revision = git("log", "-1", "--format=%H", "--", relative, cwd=root)
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ProducerError("fixture revision unavailable: a committed fixture is required")
    return revision


def entity(kind, identity, graph, created_at, **fields):
    # Stable ids allow raw live recordings to be replayed in a later process.
    raw = hashlib.sha256((graph["source_hash"] + ":" + kind + ":" + identity).encode()).digest()[:16]
    return {
        "id": f"leaf:{kind}:{UUID(bytes=raw, version=4)}", "kind": kind,
        "rev": graph["rev"], "extra": {}, "validity": {"state": "valid", "reasons": []},
        "provenance": {"created_by": "solar_w1_studio_solve.v1", "created_at": created_at,
                       "last_writer": "solar_w1_studio_solve.v1", "source_rev": graph["rev"],
                       "source_hash": graph["source_hash"]}, **fields,
    }


def import_panels(graph, intake, created_at):
    by_handle, dimensions = {}, {}
    polylines = intake.get("polylines")
    if not isinstance(polylines, list) or not polylines:
        raise ProducerError("intake requires panel polylines")
    for polyline in polylines:
        handle = normalized_handle(polyline["handle"])
        points = polyline["pts"]
        if (handle in by_handle or not isinstance(points, list) or len(points) < 3
                or any(not isinstance(p, list) or len(p) < 2
                       or any(type(v) not in (int, float) or not math.isfinite(v)
                              for v in p[:2]) for p in points)):
            raise ProducerError("invalid or duplicate panel polyline")
        dx, dy = points[1][0] - points[0][0], points[1][1] - points[0][1]
        width = math.hypot(dx, dy)
        height = math.dist(points[1][:2], points[2][:2])
        if not width or not height:
            raise ProducerError("panel polyline has a degenerate edge")
        panel = entity(
            "panel", handle, graph, created_at, frame_ref=None, matrix_cell=None,
            centre=[sum(p[i] for p in points) / len(points) for i in (0, 1)],
            angle=math.degrees(math.atan2(dy, dx)), assignment={"string_ref": None, "seq": None})
        panel["provenance"]["source_handle"] = polyline["handle"]
        by_handle[handle] = panel
        dimensions[panel["id"]] = (width, height)
        graph["panels"].append(panel)
    return by_handle, dimensions


def group_frames(graph, placement, by_handle, dimensions, placement_hash, created_at):
    groups, frames = [], []
    if not isinstance(placement.get("groups"), list) or not placement["groups"]:
        raise ProducerError("placement requires groups")
    for number, source in enumerate(placement["groups"], 1):
        name = f"Group {number}"
        if source.get("name") != name:
            raise ProducerError("placement group names must follow placement order")
        matrix, refs = [], []
        for row in source["matrix"]:
            cells = []
            for handle in row:
                panel = None if handle is None else by_handle.get(normalized_handle(handle))
                if handle is not None and panel is None:
                    raise ProducerError("placement references a panel absent from intake")
                if panel:
                    refs.append(panel["id"])
                cells.append({
                    "code": "panel" if panel else "empty", "panel_ref": panel["id"] if panel else None,
                    "seq": None, "inverter_id": None, "string_input_number": None,
                    "x": panel["centre"][0] if panel else 0.0,
                    "y": panel["centre"][1] if panel else 0.0,
                    "angle": panel["angle"] if panel else 0.0,
                })
            matrix.append(cells)
        if not refs or not matrix or not matrix[0] or any(len(row) != len(matrix[0]) for row in matrix):
            raise ProducerError("placement matrix must be nonempty and rectangular")
        width, height = dimensions[refs[0]]
        first = next(p for p in graph["panels"] if p["id"] == refs[0])
        frame = entity(
            "frame", name, graph, created_at, name=name, insertion_point=first["centre"][:],
            installation_design="Roof", panel_refs=refs, module_rows=len(matrix),
            module_columns=len(matrix[0]), module_slots=len(matrix) * len(matrix[0]),
            module_power_watts=0, module_width_along_row=width, module_height_across_row=height,
            electrical_zone_ref=None, matrix=matrix, sequences=[], panel_assignments=[
                {"panel_ref": ref, "string_ref": None, "seq": None,
                 "inverter_id": None, "string_input_number": None} for ref in refs])
        frame["provenance"].update(placement_sha256=placement_hash, licensed_placement="recorded")
        frames.append(frame)
        groups.append({"name": name, "panel_refs": refs})
    all_refs = [ref for group in groups for ref in group["panel_refs"]]
    if len(all_refs) != len(set(all_refs)) or set(all_refs) != {p["id"] for p in graph["panels"]}:
        raise ProducerError("placement must cover every intake panel exactly once")
    return groups, frames


def replay_responses(document, by_handle):
    responses = document.get("responses") if isinstance(document, dict) else document
    if not isinstance(responses, list):
        raise ProducerError("replay requires a responses array")
    result = deepcopy(responses)
    ids = {panel["id"] for panel in by_handle.values()}
    for response in result:
        for row in response["data"]["final_grid"]["Rows"]:
            for cell in row["Panels"]:
                if cell["Code"] == 1 and cell["Id"] not in ids:
                    panel = by_handle.get(normalized_handle(cell["Id"]))
                    if panel is None:
                        raise ProducerError("replay references a panel absent from intake")
                    cell["Id"] = panel["id"]
    return result


def matching_response(request, responses):
    sent = request.wire_payload()["grid"]
    matches = []
    for response in responses:
        final = deepcopy(response["data"]["final_grid"])
        for row in final["Rows"]:
            for cell in row["Panels"]:
                cell["Seq"] = 0
        if final == sent:
            matches.append(response)
    if len(matches) != 1:
        raise ProducerError(f"replay requires exactly one matching response (found {len(matches)})")
    return cloud.canonical_bytes(matches[0])


def produce(args):
    started = time.monotonic()
    maximum = args.max_string_length
    if type(maximum) is not int or not 1 <= maximum <= 900:
        raise ProducerError("max string length must be an integer in 1..900")
    fixture = args.fixture.resolve()
    fixture_hash = sha256(fixture.read_bytes())
    intake, intake_hash = read_json(args.intake)
    placement, placement_hash = read_json(args.placement)
    if (intake.get("source", {}).get("dwg_sha256") != fixture_hash
            or placement.get("fixture_sha256") != fixture_hash):
        raise ProducerError("fixture hash mismatch with intake or placement")
    revision = fixture_revision(fixture)
    sizing_record, sizing_hash = read_json(args.sizing_response)
    # The committed length is the plugin's recommendation, standard.string_length.
    standard = sizing_record["response"]["simulation_results"]["standard"]
    if type(standard["string_length"]) not in (int, float) or int(standard["string_length"]) != maximum:
        raise ProducerError("sizing response must recommend the requested max string length")
    tenant = args.tenant or "studio-replay"
    created_at = datetime.now(timezone.utc).isoformat()
    graph = new_empty_graph(
        tenant_id=tenant, drawing_id="w1-" + fixture_hash[:16], source_hash=intake_hash,
        units={"drawing_units": "in", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
               "elevation_datum": "unrecorded", "crs": ""}, created_at=created_at)
    by_handle, dimensions = import_panels(graph, intake, created_at)
    graph = validate_graph(graph)

    def recorded_sizing(request, grant):
        if request.wire() != sizing_record["request"]:
            raise ProducerError("sizing request does not match its recording")
        return sizing_cloud.canonical_bytes(sizing_record["response"])

    # Scope transport substitution to this CLI invocation and retain the builtin's
    # request, response, confirmation and graph validation in both solve modes.
    with patch.object(sizing_cloud, "resolve_grant", lambda *a: CloudGrant(tenant, "")), \
            patch.object(sizing_cloud, "post_string_length", recorded_sizing):
        graph = builtin("solar_size_strings").size_strings(graph, {
            "expected_rev": graph["rev"], "mode": "global", "confirm": True,
            "grant_ref": "recorded-sizing", "requests": {graph["settings"]["id"]: sizing_record["request"]},
        }, tenant_id=tenant, job_id="w1-sizing")["graph"]
    groups, frames = group_frames(graph, placement, by_handle, dimensions, placement_hash, created_at)

    def licensed_matrix(*, plan, **kwargs):
        return {"plan_sha256": plan_sha256(plan), "frames": deepcopy(frames)}

    graph = builtin("solar_panel_groups").create_groups(graph, {
        "expected_rev": graph["rev"], "groups": groups,
    }, drawing_intake=intake, licensed_matrix=licensed_matrix)["graph"]
    replay_hash = None
    responses = []
    if args.replay:
        document, replay_hash = read_json(args.replay)
        responses = replay_responses(document, by_handle)
    recorded_bodies, response_hashes = [], []
    original_post = cloud.post_stringer

    def transport(request, grant):
        raw = matching_response(request, responses) if args.replay else original_post(request, grant)
        recorded_bodies.append(json.loads(raw))
        response_hashes.append(sha256(raw))
        return raw

    commit = builtin("solar_commit_solve")
    with patch.object(cloud, "post_stringer", transport):
        for number, frame in enumerate(frames, 1):
            request = build_stringer_request(graph, frame["id"], max_string_length=maximum,
                                             dwgname=args.dwgname)
            job = f"w1-solve-{number}"
            binding = bind_request(graph, request, expected_rev=graph["rev"], frame_ref=frame["id"],
                                   tenant_id=tenant, job_id=job)
            params = {"grant_ref": args.grant_ref or "recorded-solve", "request": request}
            if args.replay:
                with patch.object(cloud, "resolve_grant", lambda *a: CloudGrant(tenant, "")):
                    proposal = cloud.proposal(params, tenant, job)
            else:
                proposal = cloud.proposal(params, tenant, job)
            candidate = complete_search(graph, binding, proposal)
            graph = commit.commit_solve(graph, {"expected_rev": graph["rev"]}, candidate=candidate)
    reopened = deserialize_graph(serialize_graph(graph))
    validate_graph(reopened)
    if any(reopened["extra"]["solve_coverage"].values()):
        raise ProducerError("committed solve has unassigned or duplicate panels")
    mapping = {p["id"]: normalized_handle(p["provenance"]["source_handle"]) for p in reopened["panels"]}
    mapping.update({s["id"]: "string:" + mapping[s["ordered_panel_refs"][0]] for s in reopened["strings"]})
    synthetic = ["before", "changes", "versions.solver"]
    if sizing_record.get("synthetic") is True:
        synthetic.append("settings/string_sizing")
    metadata = {
        "fixture_sha256": fixture_hash, "revision": revision,
        "parameters": {"family": "strings", "max_string_length": maximum},
        "versions": {"schema": "leaf.solar-w1-comparison.v1", "producer": "solar_w1_studio_solve.v1",
                     "capability": "0", "engine": "server-builtin", "catalog": "none",
                     "solver": "leaf-stringer-service"},
        "coordinate_system": "world", "geometry_units": "in", "angle_units": "deg",
        "entity_mapping": mapping, "before": {"recorded": False},
        "changes": {"created": [], "modified": [], "deleted": []}, "warnings": [], "rejected_inputs": [],
        "elapsed_ms": (time.monotonic() - started) * 1000,
        "execution_mode": "replay" if args.replay else "live", "state": "committed",
        "survived_reopen": True, "synthetic_fields": synthetic,
        "fallback_fields": ["panels/producer-built-from-intake", "frames/recorded-licensed-placement",
                            "frames/producer-derived-dimensions", "frames/module_power_watts/unrecorded"],
        "synthetic_flagged": True,
        "provenance": {"intake_sha256": intake_hash, "placement_sha256": placement_hash,
                       "sizing_response_sha256": sizing_hash, "response_sha256s": response_hashes},
    }
    if replay_hash:
        metadata["provenance"]["replay_sha256"] = replay_hash
    if args.record_responses:
        args.record_responses.write_text(json.dumps({"responses": recorded_bodies}, indent=2,
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
    args = parser.parse_args(argv)
    if args.grant_ref and not args.tenant:
        parser.error("live mode requires --tenant")
    if args.replay and (args.tenant or args.record_responses):
        parser.error("--tenant and --record-responses require live mode")
    try:
        produce(args)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError) as exc:
        # Never print an input object, request, grant reference or provider body.
        message = str(exc) if isinstance(exc, ProducerError) else getattr(exc, "code", None)
        message = message or getattr(exc, "classification", "invalid producer input or output")
        print(f"solar-w1-studio-solve: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
