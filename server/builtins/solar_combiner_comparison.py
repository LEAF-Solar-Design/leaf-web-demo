"""Compare nominal combiner input counts on the same bound drawing capture."""

import solar_combiner_graph as cg
from solar_design_graph import GraphValidationError, _bounded_json
from solar_local_graph import _load_builtin
from solar_sizing_client import checked_graph

INVALID = "INVALID_COMBINER_REQUEST"
OPTION_KEYS = cg.HARDWARE_KEYS | {"total_dc_inputs"}


def input_readiness(graph):
    return _load_builtin("solar-combiners").input_readiness(graph)


def run(graph, params, *, source_intake=None):
    _bounded_json(params)
    if (type(params) is not dict or set(params) != {"expected_rev", "options"}
            or type(params["expected_rev"]) is not int or type(params["options"]) is not list
            or not 2 <= len(params["options"]) <= 4):
        raise GraphValidationError(INVALID)
    placement = _load_builtin("solar-combiners")
    candidates = []
    models = set()
    for option in params["options"]:
        if (type(option) is not dict or set(option) != OPTION_KEYS
                or type(option["total_dc_inputs"]) is not int
                or not 1 <= option["total_dc_inputs"] <= cg.MAX_NUMBER):
            raise GraphValidationError(INVALID)
        _, hardware = placement._request({
            "expected_rev": params["expected_rev"],
            "hardware": {key: option[key] for key in cg.HARDWARE_KEYS},
        })
        model_key = hardware["model"].strip().casefold()
        if model_key in models:
            raise GraphValidationError(INVALID)
        models.add(model_key)
        candidates.append((hardware, option["total_dc_inputs"]))

    before = checked_graph(graph, params["expected_rev"])
    readiness = input_readiness(before)
    if not readiness["input_ready"]:
        raise GraphValidationError(readiness["input_reason"].upper())
    intake, groups = placement._drawing_inputs(source_intake)
    rows = []
    for hardware, inputs in candidates:
        try:
            cg.place_combiners(before, intake, groups, hardware=hardware, box_dc_inputs=inputs,
                               new_id=placement._minter(before),
                               created_at=before["project"]["provenance"]["created_at"])
            unassigned = []
        except cg.CombinerCapacityError as exc:
            unassigned = list(exc.unplaced_string_refs)
        except cg.CombinerGraphError as exc:
            raise GraphValidationError(placement.CODE_MAP[str(exc)], str(exc)) from None
        except cg.rb.ElectricalBridgeError as exc:
            raise GraphValidationError("CAPABILITY_NOT_READY", str(exc)[:64]) from None
        rows.append({"model": hardware["model"], "total_dc_inputs": inputs,
                     "unassigned_string_refs": unassigned})
    return {"schema": "leaf.solar-combiner-comparison.v1", "expected_rev": before["rev"],
            "string_count": len(before["strings"]), "basis": "nominal_input_capacity",
            "options": rows}
