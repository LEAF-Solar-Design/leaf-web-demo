"""Equipment placement adapter for the existing licensed mutation broker.

The broker provides licensed_equipment(plan, graph, timeout) on its isolated
preview lane and commits the returned candidate with its DWG via
write_loop.apply_graph_version. This module neither writes nor calls placement
services. The reply carries plan_sha256 and placements, each with id, handle
and configuration (the persisted equipment fields), read back from that DWG.
"""
import copy
import re

from mutation_plan import emit_plan, plan_sha256, validate_mutations
from solar_design_graph import GraphValidationError, _bounded_json, validate_graph
from solar_equipment import FIELDS, equipment_candidate, equipment_ready


def assign_equipment(graph, params, *, drawing_intake, licensed_equipment=None):
    candidate = equipment_candidate(graph, params)
    if params.get("cancel", False):
        return {"graph": candidate, "mutations": {}, "plan": None, "cancelled": True}
    _bounded_json(drawing_intake)
    old = {item["id"]: item for item in graph["inverters"]}
    mutations = {"added": []}
    retained_handles = {}
    for item in candidate["inverters"]:
        previous = old.get(item["id"])
        if previous is not None:
            handle = previous["provenance"].get("source_handle")
            matches = [insert for insert in drawing_intake.get("inserts", []) if insert.get("handle") == handle]
            if type(handle) is not str or len(matches) != 1:
                raise GraphValidationError("EQUIPMENT_HANDLE_REQUIRED")
            insert = matches[0]
            if any(insert.get(k) != item[v] for k, v in
                   (("name", "block_name"), ("layer", "layer"), ("pt", "position"),
                    ("rot", "rotation"), ("scale", "scale"))):
                raise GraphValidationError("EQUIPMENT_TRANSFORM_EDIT_UNSUPPORTED")
            retained_handles[item["id"]] = handle.upper()
        mutations["added"].append({"handle": item["id"], "kind": "INSERT",
                                   "name": item["block_name"], "layer": item["layer"],
                                   "pt": item["position"], "rot": item["rotation"], "scale": item["scale"]})
    canonical = validate_mutations(drawing_intake, mutations)
    by_id = {item["id"]: item for item in candidate["inverters"]}
    for insert in canonical["added"]:
        by_id[insert["handle"]].update(position=insert["pt"], rotation=insert["rot"], scale=insert["scale"],
                                        layer=insert["layer"])
    canonical["added"] = [insert for insert in canonical["added"] if insert["handle"] not in retained_handles]
    plan = emit_plan(canonical, base_sha256=graph["source_hash"], base_intake=drawing_intake)
    digest = plan_sha256(plan)
    preview = params.get("preview", False)
    if not preview:
        if licensed_equipment is None:
            raise GraphValidationError("LICENSED_EQUIPMENT_REQUIRED")
        reply = licensed_equipment(plan=plan, graph=copy.deepcopy(candidate), timeout=50)
        _bounded_json(reply)
        if (type(reply) is not dict or set(reply) != {"plan_sha256", "placements"}
                or reply["plan_sha256"] != digest or type(reply["placements"]) is not list
                or len(reply["placements"]) != len(by_id)):
            raise GraphValidationError("INVALID_LICENSED_EQUIPMENT")
        seen, handles = set(), set()
        existing = {entity.get("handle", "").upper() for name in
                    ("inserts", "polylines", "circles", "arcs", "texts")
                    for entity in drawing_intake.get(name, [])}
        for placement in reply["placements"]:
            if (type(placement) is not dict or set(placement) != {"id", "handle", "configuration"}
                    or type(placement["id"]) is not str or placement["id"] not in by_id
                    or placement["id"] in seen or type(placement["handle"]) is not str
                    or re.fullmatch(r"[0-9A-Fa-f]{1,32}", placement["handle"]) is None):
                raise GraphValidationError("INVALID_LICENSED_EQUIPMENT")
            item = by_id[placement["id"]]
            handle = placement["handle"].upper()
            if (handle in handles or (handle in existing and retained_handles.get(item["id"]) != handle)
                    or (item["id"] in retained_handles and retained_handles[item["id"]] != handle)
                    or placement["configuration"] != {key: item[key] for key in FIELDS}):
                raise GraphValidationError("INVALID_LICENSED_EQUIPMENT")
            seen.add(item["id"])
            handles.add(handle)
            item["provenance"]["source_handle"] = handle
    candidate = validate_graph(candidate)
    return {"graph": candidate, "mutations": canonical, "plan": plan.decode("utf-8"),
            "plan_sha256": digest, "cancelled": False, "preview": preview,
            "ready": equipment_ready(candidate)}


def run(intake, params):
    raise RuntimeError("solar-assign-equipment requires the licensed mutation broker")
