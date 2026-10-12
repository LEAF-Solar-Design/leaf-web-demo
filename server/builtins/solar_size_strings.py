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
    prepared = cloud.prepare_graph_sizing(graph, params)
    if prepared["cancelled"]:
        return {"graph": prepared["graph"], "confirmed": False, "records": {}}
    records = {
        target_id: cloud.size(request, tenant_id, job_id)
        for target_id, request in prepared["validated"].items()
    }
    if not params.get("confirm", False):
        return {"graph": prepared["graph"], "confirmed": False, "records": records}
    return cloud.apply_sizing_records(prepared, records)


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
