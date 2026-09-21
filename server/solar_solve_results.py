"""Revision-bound solar results for the existing drawing mutation owner.

The broker retains bindings and candidates in its job records. These helpers
never start jobs, call the solver, or create a second version store. Graph
candidates travel in the existing intake/version transaction; undo uses that
transaction's immutable prior version, including all validity flags.
"""
from __future__ import annotations

import copy
import hashlib
import math

from leaf_cloud_client import StringerRequest, canonical_bytes, proposal_provenance
from solar_dependencies import affected_entities
from solar_design_graph import (
    GraphValidationError, _bounded_json, entities, new_id, validate_graph,
)
from solar_sizing_client import advance, checked_graph


DERIVED_KINDS = {"string", "inverter", "route", "schedule"}
SETTINGS_FIELDS = {
    "panel_layer_contains", "panel_group_layer", "string_layer", "home_run_layer",
    "panels_in_sequence", "num_mppt", "strings_per_mppt", "optimizer_ratio",
    "use_l2_collectors", "panel_group_number", "string_number", "inverter_number",
    "mppt_letter",
}


def digest(value):
    _bounded_json(value)
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def upstream_basis(graph):
    """Detect upstream edits even when another builtin has not marked validity."""
    return digest({
        "source_hash": graph["source_hash"], "catalog_versions": graph["catalog_versions"],
        "project": graph["project"], "zones": graph["electrical_zones"],
        "settings": {key: value for key, value in graph["settings"].items()
                     if key not in {"rev", "provenance", "string_number"}},
        "frames": [{key: frame[key] for key in (
            "id", "panel_refs", "module_rows", "module_columns", "electrical_zone_ref",
            "module_power_watts", "module_width_along_row", "module_height_across_row",
        )} for frame in graph["frames"]],
        "panels": [{key: panel[key] for key in (
            "id", "frame_ref", "matrix_cell", "centre", "angle",
        )} for panel in graph["panels"]],
    })


def _frame_request(graph, frame_ref, request):
    """Bind original (not truncated) cells to application-owned panel ids."""
    try:
        parsed = StringerRequest.model_validate(request)
        frame = next(f for f in graph["frames"] if f["id"] == frame_ref)
        panels = {p["id"]: p for p in graph["panels"]}
        if (len(parsed.grid.Rows) != frame["module_rows"]
                or len(parsed.grid.Rows[0].Panels) != frame["module_columns"]):
            raise ValueError()
        for r, row in enumerate(parsed.grid.Rows):
            for c, wire in enumerate(row.Panels):
                cell = frame["matrix"][r][c]
                ref = cell["panel_ref"]
                if (wire.Code == 1) != (ref is not None):
                    raise ValueError()
                if ref is not None:
                    panel = panels[ref]
                    if wire.Id != ref or any(
                        not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
                        for a, b in ((wire.X, panel["centre"][0]),
                                     (wire.Y, panel["centre"][1]),
                                     (wire.Angle, panel["angle"]))
                    ):
                        raise ValueError()
        return frame, parsed
    except (ValueError, TypeError, KeyError, IndexError, StopIteration):
        raise GraphValidationError("SOLVE_GRID_MISMATCH") from None


def bind_request(graph, request, *, expected_rev, frame_ref, tenant_id, job_id,
                 phase="initial"):
    """Capture before submission, then persist with the authenticated job.

    tenant_id and job_id come from the broker, never from caller parameters.
    A background search is submitted against the committed initial graph.
    """
    graph = checked_graph(graph, expected_rev)
    _bounded_json(request)
    if (phase not in ("initial", "background")
            or any(type(v) is not str or not 1 <= len(v) <= 256
                   for v in (tenant_id, job_id))):
        raise GraphValidationError("INVALID_SOLVE_BINDING")
    frame, parsed = _frame_request(graph, frame_ref, request)
    if phase == "background" and not frame["extra"].get("solve", {}).get("initial_complete"):
        raise GraphValidationError("INITIAL_SOLVE_REQUIRED")
    return {"source_rev": graph["rev"], "source_hash": graph["source_hash"],
            "graph_sha256": digest(graph), "frame_ref": frame["id"],
            "request": parsed.model_dump(), "tenant_id": tenant_id,
            "job_id": job_id, "phase": phase}


def _check_binding(graph, binding):
    _bounded_json(binding)
    if type(binding) is not dict or set(binding) != {
        "source_rev", "source_hash", "graph_sha256", "frame_ref", "request",
        "tenant_id", "job_id", "phase",
    }:
        raise GraphValidationError("INVALID_SOLVE_BINDING")
    if (type(binding["source_rev"]) is not int
            or binding["source_rev"] != graph["rev"]
            or binding["source_hash"] != graph["source_hash"]
            or binding["graph_sha256"] != digest(graph)):
        raise GraphValidationError("STALE_SOLVE_RESULT")
    expected = bind_request(
        graph, binding["request"], expected_rev=graph["rev"],
        frame_ref=binding["frame_ref"], tenant_id=binding["tenant_id"],
        job_id=binding["job_id"], phase=binding["phase"],
    )
    if binding != expected:
        raise GraphValidationError("INVALID_SOLVE_BINDING")


def complete_search(graph, binding, proposal):
    """Validate completion without accepting or modifying the graph."""
    graph = validate_graph(graph)
    _check_binding(graph, binding)
    _bounded_json(proposal)
    try:
        path = proposal["proposal"]["data"]["best_result"]["info"]["visited_path"]
        if (type(path) is list and all(type(cell) is list and len(cell) == 2
                                      and all(type(n) is int for n in cell) for cell in path)):
            if len({tuple(cell) for cell in path}) != len(path):
                raise GraphValidationError("DUPLICATE_SOLVE_PANELS")
            frame, _ = _frame_request(graph, binding["frame_ref"], binding["request"])
            if len(path) < len(frame["panel_refs"]):
                raise GraphValidationError("UNASSIGNED_SOLVE_PANELS")
        proof = proposal_provenance(
            proposal, {"grant_ref": "receipt-validation", "request": binding["request"]},
            binding["tenant_id"], binding["job_id"],
        )
    except GraphValidationError:
        raise
    except (ValueError, KeyError, TypeError):
        raise GraphValidationError("INVALID_SOLVE_PROPOSAL") from None
    return {"binding": copy.deepcopy(binding), "proposal": copy.deepcopy(proposal),
            "proof": proof, "status": "completed", "accepted": False,
            "initial_complete": binding["phase"] == "initial",
            "background_complete": binding["phase"] == "background"}


def coverage(graph):
    counts = {}
    for string in graph["strings"]:
        for ref in string["ordered_panel_refs"]:
            counts[ref] = counts.get(ref, 0) + 1
    return {"duplicate_panel_refs": sorted(ref for ref, count in counts.items() if count > 1),
            "unassigned_panel_refs": sorted(p["id"] for p in graph["panels"]
                                            if p["id"] not in counts)}


def sync_assignments(graph):
    """Update all redundant membership views while retaining unknown fields."""
    report = coverage(graph)
    if report["duplicate_panel_refs"]:
        raise GraphValidationError("DUPLICATE_PANEL_MEMBERSHIP")
    panels = {p["id"]: p for p in graph["panels"]}
    for panel in panels.values():
        panel["assignment"].update(string_ref=None, seq=None)
    for string in graph["strings"]:
        refs = string["ordered_panel_refs"]
        string["module_count"] = len(refs)
        for seq, ref in enumerate(refs):
            if ref not in panels:
                raise GraphValidationError("MISSING_PANEL")
            panels[ref]["assignment"].update(string_ref=string["id"], seq=seq)
    inputs = {a["string_ref"]: (i["id"], a["input_number"])
              for i in graph["inverters"] for a in i["input_assignments"]}
    for frame in graph["frames"]:
        members = set(frame["panel_refs"])
        old_sequences = {s["string_ref"]: s for s in frame["sequences"]}
        frame["sequences"] = []
        for string in graph["strings"]:
            refs = [ref for ref in string["ordered_panel_refs"] if ref in members]
            if refs:
                sequence = old_sequences.get(string["id"], {})
                sequence.update(string_ref=string["id"], ordered_panel_refs=refs)
                frame["sequences"].append(sequence)
        for record in frame["panel_assignments"] + [
            cell for row in frame["matrix"] for cell in row if cell["panel_ref"] is not None
        ]:
            assignment = panels[record["panel_ref"]]["assignment"]
            record["seq"] = assignment["seq"]
            if "string_ref" in record:
                record["string_ref"] = assignment["string_ref"]
            record["inverter_id"], record["string_input_number"] = inputs.get(
                assignment["string_ref"], (None, None))
    return report


def invalidate_dependents(before, after, changed_ids, *, solved_ids=()):
    affected = affected_entities(before, after, changed_ids)
    for entity in entities(after):
        if (entity["id"] in affected and entity["id"] not in solved_ids
                and entity["kind"] in DERIVED_KINDS):
            entity["validity"] = {"state": "stale", "reasons": ["upstream_corrected"]}
    return affected


def finish_mutation(before, after, tool):
    old = {e["id"]: e for e in entities(before)}
    changed = [e for e in entities(after) if old.get(e["id"]) != e]
    after["extra"]["solve_coverage"] = coverage(after)
    return advance(after, changed, tool)


def accept_candidate(graph, candidate, *, expected_rev):
    """Build one graph version; publication remains the write owner's job."""
    before = checked_graph(graph, expected_rev)
    _bounded_json(candidate)
    if type(candidate) is not dict or "binding" not in candidate or "proposal" not in candidate:
        raise GraphValidationError("INVALID_SOLVE_CANDIDATE")
    verified = complete_search(before, candidate["binding"], candidate["proposal"])
    if candidate != verified:
        raise GraphValidationError("INVALID_SOLVE_CANDIDATE")
    result = copy.deepcopy(before)
    binding = verified["binding"]
    frame, _ = _frame_request(result, binding["frame_ref"], binding["request"])
    members = set(frame["panel_refs"])
    previous = [s for s in result["strings"] if members.intersection(s["ordered_panel_refs"])]
    if any(not set(s["ordered_panel_refs"]) <= members for s in previous):
        raise GraphValidationError("CROSS_FRAME_STRING_REQUIRES_CORRECTION")
    previous_ids = {s["id"] for s in previous}
    # Two nesting levels on purpose. The result envelope's top-level visited_path is
    # ALREADY mapped back to ORIGINAL grid indices by the client
    # (leaf_cloud_client.StringerResponse.original_visited_path), so it indexes
    # frame["matrix"] directly. The nested ["proposal"] below is the raw service
    # response, whose own coordinates are truncated-grid and must never be used here.
    path = verified["proposal"]["visited_path"]
    if any(not (0 <= r < frame["module_rows"] and 0 <= c < frame["module_columns"])
           for r, c in path):
        raise GraphValidationError("INVALID_PATH_INDICES")
    refs = [frame["matrix"][r][c]["panel_ref"] for r, c in path]
    panels = {p["id"]: p for p in result["panels"]}
    lengths = verified["proposal"]["proposal"]["data"]["best_result"]["info"]["sequence_length"]
    if (any(type(length) is not int or length <= 0 for length in lengths)
            or sum(lengths) != len(path)):
        raise GraphValidationError("TRUNCATED_SEQUENCE_LENGTH")
    strings, offset = [], 0
    tags = {s["circuit_tag"] for s in result["strings"] if s["id"] not in previous_ids}
    number = result["settings"]["string_number"]
    for i, length in enumerate(lengths):
        ordered = refs[offset:offset + length]
        offset += length
        string = copy.deepcopy(previous[i]) if i < len(previous) else {
            "id": new_id("string"), "kind": "string", "rev": result["rev"],
            "provenance": copy.deepcopy(frame["provenance"]), "extra": {},
            "tag_text_ref": None, "wire_gauge": "",
        }
        if i >= len(previous):
            while f"S{number}" in tags:
                number += 1
            string["circuit_tag"] = f"S{number}"
            number += 1
        if string["circuit_tag"] in tags:
            raise GraphValidationError("DUPLICATE_CIRCUIT_TAG")
        tags.add(string["circuit_tag"])
        points = [copy.deepcopy(panels[ref]["centre"]) for ref in ordered]
        # Graph geometry is in compute metres, irrespective of drawing units.
        length_m = sum(math.dist(a + [0] * (3 - len(a)), b + [0] * (3 - len(b)))
                       for a, b in zip(points, points[1:]))
        string.update(circuit_kind="String", ordered_panel_refs=ordered,
                      module_count=length, from_ref=ordered[0], to_ref=ordered[-1],
                      route=points, length_ft=length_m / 0.3048, inverter_ref=None,
                      validity={"state": "valid", "reasons": []})
        string["extra"]["polarity"] = {
            "source": "derived", "rule": "ordered-path-first-negative-last-positive",
            "negative_panel_ref": ordered[0], "positive_panel_ref": ordered[-1],
            "source_rev": before["rev"], "response_sha256": verified["proof"]["response_sha256"],
        }
        string["extra"]["length_provenance"] = {
            "source": "derived", "rule": "panel-centre-path", "point_units": "m",
            "length_units": "ft", "includes_home_runs": False,
        }
        string["provenance"].update(source_hash=before["source_hash"],
                                    catalog_versions=copy.deepcopy(before["catalog_versions"]))
        strings.append(string)
    result["settings"]["string_number"] = number
    result["strings"] = [s for s in result["strings"] if s["id"] not in previous_ids] + strings
    for inverter in result["inverters"]:
        inverter["input_assignments"] = [a for a in inverter["input_assignments"]
                                         if a["string_ref"] not in previous_ids]
    sync_assignments(result)
    invalidate_dependents(before, result, list(members | previous_ids),
                          solved_ids={s["id"] for s in strings})
    frame["extra"]["solve"] = {
        "initial_complete": True, "background_complete": verified["background_complete"],
        "accepted_phase": binding["phase"], "source_rev": before["rev"],
        "job_id": binding["job_id"], **verified["proof"],
        "upstream_sha256": upstream_basis(result),
    }
    return finish_mutation(before, result, "solar-commit-solve")


def correct_graph(graph, params):
    _bounded_json(params)
    if type(params) is not dict or set(params) - {
        "expected_rev", "memberships", "settings_changes", "cancel",
    } or type(params.get("cancel", False)) is not bool:
        raise GraphValidationError("INVALID_CORRECTION")
    before = checked_graph(graph, params.get("expected_rev"))
    if params.get("cancel", False):
        return before
    memberships, settings = params.get("memberships", []), params.get("settings_changes", {})
    if (type(memberships) is not list or len(memberships) > 1000
            or type(settings) is not dict or set(settings) - SETTINGS_FIELDS
            or not (memberships or settings)):
        raise GraphValidationError("INVALID_CORRECTION")
    result, changed = copy.deepcopy(before), []
    strings = {s["id"]: s for s in result["strings"]}
    for edit in memberships:
        if (type(edit) is not dict or set(edit) != {"string_ref", "ordered_panel_refs"}
                or type(edit["string_ref"]) is not str or edit["string_ref"] not in strings
                or edit["string_ref"] in changed
                or type(edit["ordered_panel_refs"]) is not list
                or len(edit["ordered_panel_refs"]) > 100000
                or any(type(ref) is not str for ref in edit["ordered_panel_refs"])):
            raise GraphValidationError("INVALID_CORRECTION")
        string = strings[edit["string_ref"]]
        refs = copy.deepcopy(edit["ordered_panel_refs"])
        string.update(ordered_panel_refs=refs, module_count=len(refs),
                      from_ref=refs[0] if refs else None, to_ref=refs[-1] if refs else None)
        changed.append(string["id"])
    if settings:
        result["settings"].update(copy.deepcopy(settings))
        result["settings"]["global_string_sizing_confirmed"] = False
        result["settings"]["extra"].pop("string_sizing", None)
        changed.append(result["settings"]["id"])
    sync_assignments(result)
    invalidate_dependents(before, result, changed)
    return finish_mutation(before, result, "solar-correct-string")


def require_current_export(graph):
    """Export adapters must call this before labelling output current."""
    graph = validate_graph(graph)
    basis = upstream_basis(graph)
    if (any(e["validity"]["state"] != "valid" for e in entities(graph))
            or any(f["extra"].get("solve", {}).get("upstream_sha256", basis) != basis
                   for f in graph["frames"])
            or any(coverage(graph).values())):
        raise GraphValidationError("SOLAR_OUTPUT_NOT_CURRENT")
    return graph


def version_companion(intake, before, after):
    """Embed graph and digest in the existing intake, without dropping CAD data."""
    _bounded_json(intake)
    before, after = validate_graph(before), validate_graph(after)
    if (type(intake) is not dict or intake.get("solar_design_graph") != before
            or intake.get("solar_design_graph_sha256") != digest(before)
            or after["rev"] != before["rev"] + 1
            or after["project"]["id"] != before["project"]["id"]):
        raise GraphValidationError("STALE_GRAPH_COMPANION")
    result = copy.deepcopy(intake)
    result.update(solar_design_graph=after, solar_design_graph_sha256=digest(after))
    _bounded_json(result)
    return result


def publish_version(backend, tenant_id, drawing_id, *, parent_version, before, after,
                    holder, fence, job_id, request_sha256):
    """Broker-only adapter to the existing fenced, compare-and-set write lane.

    Intake-backed versions carry the graph atomically in the version payload.
    Raw DWG publication must use the licensed writer's companion transaction;
    this adapter refuses to replace DWG bytes with JSON. No caller-selected
    backend, identity or fencing value belongs in tool parameters.

    The checkout pre-check is outside the store lock. The commit re-checks
    ownership under its own lock and compare-and-sets the head, so another
    session's lease cannot authorize this write. A legacy lease can expire
    between these checks without a new owner, preserving single-writer safety;
    the postgres authority refuses that expired lease too.
    """
    import json
    import write_loop
    import store
    from tenant_id_validator import validate_tenant_id

    validate_tenant_id(tenant_id)
    validate_tenant_id(drawing_id, kind="drawing id")
    if type(parent_version) is not int or parent_version < 1:
        raise GraphValidationError("INVALID_PARENT_VERSION")
    if (type(job_id) is not str or not 1 <= len(job_id) <= 128
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
                   for c in job_id)
            or type(request_sha256) is not str or len(request_sha256) != 64
            or any(c not in "0123456789abcdef" for c in request_sha256)):
        raise GraphValidationError("INVALID_JOB_BINDING")
    if (type(holder) is not str or not holder or holder == store.ANONYMOUS_HOLDER
            or type(fence) is not int or fence < 1):
        raise GraphValidationError("CHECKOUT_REQUIRED")
    manifest = store.load_manifest(
        backend, store.sanitize_id(tenant_id), store.sanitize_id(drawing_id))
    workitem_id = "solar-graph:" + job_id
    note = "solar-graph-commit:" + request_sha256
    for entry in manifest["versions"]:
        if entry.get("workitem_id") != workitem_id:
            continue
        if entry.get("note") != note:
            raise GraphValidationError("JOB_BINDING_REUSED")
        # A successful intake-backed commit has an immutable JSON parent.
        # Read its bytes directly so replay cannot mint an intake cache proof.
        _, parent_key = store.resolve_version(backend, tenant_id, drawing_id, parent_version)
        payload = version_companion(json.loads(backend.get(parent_key)), before, after)
        if entry.get("sha256") != hashlib.sha256(canonical_bytes(payload)).hexdigest():
            raise GraphValidationError("JOB_BINDING_REUSED")
        return {"version": entry["v"], "parent_version": entry["parent"],
                "graph_sha256": digest(after), "intake_sha256": entry["sha256"],
                "job_id": job_id, "request_sha256": request_sha256, "replayed": True}
    checkout = manifest.get("checkout")
    if not store.checkout_active(checkout):
        raise GraphValidationError("CHECKOUT_REQUIRED")
    if checkout.get("holder") != holder or checkout.get("fence") != fence:
        raise GraphValidationError("CHECKOUT_DENIED")
    version, intake = write_loop.read_intake(backend, tenant_id, drawing_id, "head")
    if version != parent_version:
        raise GraphValidationError("STALE_GRAPH_REVISION")
    payload = version_companion(intake, before, after)
    _, key = store.resolve_version(backend, tenant_id, drawing_id, version)
    try:
        if json.loads(backend.get(key)) != intake:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise GraphValidationError("LICENSED_GRAPH_COMMIT_REQUIRED") from None
    data = canonical_bytes(payload)
    with write_loop.drawing_mutation_refusal_guard() as refusal:
        if refusal is not None:
            raise GraphValidationError("DRAWING_MUTATION_REFUSED")
        try:
            new_version = write_loop._put_bytes_version(
                backend, tenant_id, drawing_id, data, parent_version=parent_version,
                meta={"tool": "solar-graph", "workitem_id": workitem_id, "note": note},
                holder=holder, fence=fence, require_parent_is_head=True,
            )
        except store.CheckoutDenied:
            raise GraphValidationError("CHECKOUT_DENIED") from None
        except ValueError as exc:
            if str(exc).startswith("stale parent"):
                raise GraphValidationError("STALE_GRAPH_REVISION") from None
            raise
    return {"version": new_version, "parent_version": parent_version,
            "graph_sha256": digest(after), "intake_sha256": hashlib.sha256(data).hexdigest(),
            "job_id": job_id, "request_sha256": request_sha256, "replayed": False}
