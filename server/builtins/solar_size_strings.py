"""Sizing orchestration called by the authenticated cloud broker.

Requests are the plugin's StringSizer requests. The committed length is the plugin's
recommendation (standard.string_length) and commits only when its cold-Voc guard passes,
as StringSizerInputForm does; confirm=True is the palette's explicit Confirm step.
Returns a complete graph candidate for the existing drawing transaction. Neither
preview, cancellation nor a partial zone response writes durable state here.
"""
import copy

import solar_sizing_client as cloud
from solar_design_graph import GraphValidationError, _bounded_json


def size_strings(graph, params, *, tenant_id, job_id):
    _bounded_json(params)
    if (type(params) is not dict
            or set(params) - {"expected_rev", "mode", "requests", "grant_ref", "confirm", "cancel"}
            or type(params.get("cancel", False)) is not bool
            or type(params.get("confirm", False)) is not bool):
        raise GraphValidationError("INVALID_SIZING_REQUEST")
    result = cloud.checked_graph(graph, params.get("expected_rev"))
    if params.get("cancel", False):
        return {"graph": result, "confirmed": False, "records": {}}
    mode = params.get("mode")
    targets = cloud.sizing_targets(result, mode)
    requests = params.get("requests")
    if type(requests) is not dict or set(requests) != set(targets):
        raise GraphValidationError("INVALID_SIZING_COVERAGE")
    validated = {}
    for target_id, target in targets.items():
        parsed = cloud.validate_params({"grant_ref": params.get("grant_ref"),
                                        "request": requests[target_id]})
        if mode == "zones" and (parsed.request.module_name != target["module_model"]
                                or parsed.request.full_inverter_name != target["inverter_model_a"]):
            raise GraphValidationError("SIZING_MODEL_MISMATCH")
        validated[target_id] = {"grant_ref": parsed.grant_ref, "request": parsed.request.wire()}
    records = {target_id: cloud.size(request, tenant_id, job_id)
               for target_id, request in validated.items()}
    if not params.get("confirm", False):
        return {"graph": result, "confirmed": False, "records": records}
    if any(not record["sizing"]["voc_cold"]["passes"] for record in records.values()):
        raise GraphValidationError("COLD_VOLTAGE_FAILED")
    for target_id, target in targets.items():
        target.update(copy.deepcopy(records[target_id]["sizing"]))
    settings = result["settings"]
    settings["global_string_sizing_confirmed"] = mode == "global"
    settings["extra"]["string_sizing"] = {
        "mode": mode, "basis_sha256": cloud.sizing_basis(result), "records": records}
    changed = list(targets.values())
    if mode != "global":
        changed.append(settings)
    result = cloud.advance(result, changed, "solar-size-strings")
    cloud.require_sizing(result)
    return {"graph": result, "confirmed": True, "records": copy.deepcopy(records)}


def run(intake, params):
    raise RuntimeError("solar-size-strings requires the authenticated cloud broker")
