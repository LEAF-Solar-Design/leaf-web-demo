"""Compare engineering totals from two verified versions of one saved drawing."""
from decimal import Decimal, localcontext

from solar_design_graph import GraphValidationError
from solar_ground_graph_codec import decode_graph_slots
from solar_project_context import resolve_context
from solar_project_graph import resolve_project_graph_context
from solar_project_solaredge import read_tracking


_METRICS = ("panels", "optimizers", "inverters", "strings", "kw_dc")
_INVERTER_TYPES = {"string_inverter", "central_inverter", "combiner_box"}


def compare(tenant, project_id, drawing_id, before_version_id, after_version_id):
    def read(version_id):
        verified = resolve_context(tenant, project_id, version_id, drawing_id=drawing_id)
        context = resolve_project_graph_context(verified)
        receipt = {"input_version_id": str(context.parent_version_id),
                   "intake_sha256": context.intake_sha256, "graph_sha256": context.graph_sha256}
        return receipt, _totals(tenant, context)

    before_receipt, before = read(before_version_id)
    after_receipt, after = (before_receipt.copy(), before) if before_version_id == after_version_id else read(after_version_id)
    with localcontext() as precision:
        # Values carry at most 18 integer and 18 fractional digits.
        precision.prec = 40
        metrics = []
        for name, (left, left_reason), (right, right_reason) in zip(_METRICS, before, after, strict=True):
            delta = right - left if left is not None and right is not None else None
            metrics.append({"metric": name, "before": _decimal_text(left), "before_reason": left_reason,
                            "after": _decimal_text(right), "after_reason": right_reason,
                            "delta": _decimal_text(delta)})
    return {"before": before_receipt, "after": after_receipt, "metrics": metrics}


def _decimal_text(value):
    if value is None:
        return None
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _totals(tenant, context):
    graph = context.graph
    if graph is None:
        return [(None, "saved_graph_unavailable")] * len(_METRICS)
    slots = decode_graph_slots(graph)
    panel_count = len(graph["panels"]) + sum(len(table.ids) for table in slots.values())
    devices = [item.get("equipment_type") for item in graph["inverters"]]
    inverter_count = ((sum(item != "combiner_box" for item in devices), None)
                      if all(item in _INVERTER_TYPES for item in devices)
                      else (None, "saved_equipment_type_unavailable"))
    dc_kw = _dc_capacity(graph["frames"], slots, panel_count)
    return [(panel_count, None), _optimizer_count(tenant, context), inverter_count,
            (len(graph["strings"]), None),
            (dc_kw, None if dc_kw is not None else "complete_module_power_unavailable")]


def _dc_capacity(frames, slots, panel_count):
    if panel_count == 0:
        return None
    covered = set()
    with localcontext() as precision:
        precision.prec = 50
        watts = Decimal(0)
        for frame in frames:
            table = slots.get(frame["id"])
            refs = table.ids if table is not None else frame["panel_refs"]
            power = frame.get("module_power_watts")
            if power is None or power <= 0 or power >= 10**18 or covered.intersection(refs):
                return None
            module_watts = Decimal(str(power)).normalize()
            if module_watts.as_tuple().exponent < -15:
                return None
            covered.update(refs)
            watts += module_watts * len(refs)
        kw = watts / 1000
        return kw if len(covered) == panel_count and kw < 10**18 else None


def _optimizer_count(tenant, context):
    try:
        tracking = read_tracking(tenant, context, {"section": "frames"})
    except GraphValidationError as exc:
        return None, exc.code
    measurement = tracking.get("optimizer_measurement")
    if (measurement is None or not measurement["complete"] or
            tracking.get("acceptance", {}).get("status", "complete") != "complete"):
        return None, "verified_optimizer_measurement_unavailable"
    return measurement["total_count"], None
