"""Drawing-owned settings candidate for the existing mutation transaction."""
import copy

from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import advance, checked_graph

EDITABLE = {"panel_layer_contains", "panel_group_layer", "string_layer", "home_run_layer",
            "panels_in_sequence", "num_mppt", "strings_per_mppt", "optimizer_ratio",
            "use_l2_collectors", "panel_group_number", "string_number", "inverter_number",
            "mppt_letter"}


def run(intake, params):
    _bounded_json(params)
    if (type(params) is not dict or set(params) - {"expected_rev", "changes", "cancel"}
            or type(params.get("cancel", False)) is not bool):
        raise GraphValidationError("INVALID_SETTINGS_REQUEST")
    graph = checked_graph(intake, params.get("expected_rev"))
    if params.get("cancel", False):
        return graph
    changes = params.get("changes")
    if type(changes) is not dict or not changes or set(changes) - EDITABLE:
        raise GraphValidationError("INVALID_SETTINGS_REQUEST")
    settings = graph["settings"]
    settings.update(copy.deepcopy(changes))
    # Editing a drawing setting requires explicit sizing confirmation again.
    settings["global_string_sizing_confirmed"] = False
    settings["extra"].pop("string_sizing", None)
    return advance(graph, [settings], "solar-settings")
