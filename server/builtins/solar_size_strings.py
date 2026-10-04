"""Sizing orchestration called by the authenticated cloud broker.

Requests are the plugin's StringSizer requests. The committed length is the plugin's
recommendation (standard.string_length) and commits only when its cold-Voc guard passes,
as StringSizerInputForm does; confirm=True is the palette's explicit Confirm step.
Returns a complete graph candidate for the existing drawing transaction. Neither
preview, cancellation nor a partial zone response writes durable state here.

The broker's local graph commit rail calls run_bound with the job's tenant and job id.
It refuses anything but a confirmed or cancelled request before any outbound call, and
names every CloudError as a GraphValidationError so a refusal never publishes a version.
run() stays unbound: sizing without a tenant grant is refused.
"""
import copy

import solar_sizing_client as cloud
from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_power import frame_module_power, record_module_power


def manual_size_strings(graph, params):
    _bounded_json(params)
    if (type(params) is not dict
            or set(params) - {"expected_rev", "mode", "confirm", "cancel"}
            or type(params.get("cancel", False)) is not bool
            or type(params.get("confirm", False)) is not bool):
        raise GraphValidationError("INVALID_SIZING_REQUEST")
    result = cloud.checked_graph(graph, params.get("expected_rev"))
    if params.get("cancel", False):
        return {"graph": result, "confirmed": False, "records": {}}
    if params.get("confirm") is not True:
        raise GraphValidationError("INVALID_SIZING_REQUEST")
    slots = cloud.compact_slot_tables(result)
    evidence = cloud.manual_sizing_evidence(result, slots)
    settings = result["settings"]
    settings["global_string_sizing_confirmed"] = True
    settings["extra"]["string_sizing"] = evidence
    settings["voc_cold"] = cloud.manual_voltage()
    result = cloud.advance(result, [settings], "solar-size-strings")
    cloud.require_sizing(result, slots)
    return {"graph": result, "confirmed": True, "records": {}}


def size_strings(graph, params, *, tenant_id, job_id):
    if type(params) is dict and params.get("mode") == "manual-global":
        return manual_size_strings(graph, params)
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
    # One decode of the compact Ground slot blocks (codec leaf.solar-ground-slots.v1) serves the
    # targets, the basis and the frame members below; sizing never changes a block.
    slots = cloud.compact_slot_tables(result)
    targets = cloud.sizing_targets(result, mode, slots)
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
    project_zip = result["project"]["zip_code"].strip()[:5]
    if not project_zip or any(request["request"]["zip_code"] != project_zip
                              for request in validated.values()):
        raise GraphValidationError("SIZING_PROJECT_MISMATCH")
    records = {target_id: cloud.size(request, tenant_id, job_id)
               for target_id, request in validated.items()}
    if not params.get("confirm", False):
        return {"graph": result, "confirmed": False, "records": records}
    if any(not record["sizing"]["voc_cold"]["passes"] for record in records.values()):
        raise GraphValidationError("COLD_VOLTAGE_FAILED")
    try:
        for record in records.values():
            record_module_power(record)
    except GraphValidationError:
        raise GraphValidationError("CLOUD_RESPONSE_INVALID") from None
    for target_id, target in targets.items():
        target.update(copy.deepcopy(records[target_id]["sizing"]))
    settings = result["settings"]
    settings["global_string_sizing_confirmed"] = mode == "global"
    settings["extra"]["string_sizing"] = {
        "mode": mode, "basis_sha256": cloud.sizing_basis(result, slots), "records": records}
    changed = list(targets.values())
    if mode != "global":
        changed.append(settings)
    # A compact Ground frame stores no panel_refs: its slot panel ids are its members, exactly
    # the panel_refs of its expansion.
    zone_members = ({zone["id"]: set(zone["panel_refs"]) for zone in result["electrical_zones"]}
                    if mode == "zones" else {})
    for frame in result["frames"]:
        zone_ref = frame["electrical_zone_ref"]
        table = slots.get(frame["id"])
        members = frame["panel_refs"] if table is None else list(table.ids)
        if mode == "zones":
            covering = [zone_id for zone_id, refs in zone_members.items() if refs.issuperset(members)]
            # Existing frames may span zones after switching from global sizing.
            power = (frame_module_power(result, members, covering[0])
                     if len(covering) == 1 else 0.0)
        else:
            power = frame_module_power(result, members, zone_ref)
        if frame["module_power_watts"] != power:
            frame["module_power_watts"] = power
            changed.append(frame)
    result = cloud.advance(result, changed, "solar-size-strings")
    cloud.require_sizing(result)
    return {"graph": result, "confirmed": True, "records": copy.deepcopy(records)}


def run_bound(graph, params, *, tenant_id, job_id):
    """Commit a confirmed sizing under the job's tenant; fails closed before any call."""
    if (type(params) is not dict
            or (params.get("confirm") is not True and params.get("cancel") is not True)):
        raise GraphValidationError("INVALID_SIZING_REQUEST")
    try:
        return size_strings(graph, params, tenant_id=tenant_id, job_id=job_id)["graph"]
    except cloud.CloudError as exc:
        raise GraphValidationError(exc.classification.upper()) from None


def run(intake, params):
    raise RuntimeError("solar-size-strings requires the authenticated cloud broker")
