"""Drawing-owned settings candidate for the existing mutation transaction."""
import copy

from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError, _bounded_json, entities
from solar_project import apply_project_changes, normalize_project_changes
from solar_sizing_client import advance, checked_graph

EDITABLE = {"panel_layer_contains", "panel_group_layer", "string_layer", "home_run_layer",
            "panels_in_sequence", "num_mppt", "strings_per_mppt", "optimizer_ratio",
            "use_l2_collectors", "panel_group_number", "string_number", "inverter_number",
            "mppt_letter"}


def run(intake, params):
    # Name project-coordinate refusals before the generic JSON finite-number guard.
    if (type(params) is dict and "project_changes" in params
            and not set(params) - {"expected_rev", "changes", "project_changes", "cancel"}
            and params.get("cancel", False) is False):
        normalize_project_changes(params["project_changes"])
    _bounded_json(params)
    if (type(params) is not dict
            or set(params) - {"expected_rev", "changes", "project_changes", "cancel"}
            or type(params.get("cancel", False)) is not bool):
        raise GraphValidationError("INVALID_SETTINGS_REQUEST")
    graph = checked_graph(intake, params.get("expected_rev"))
    before = copy.deepcopy(graph)
    if params.get("cancel", False):
        return graph
    if "changes" not in params and "project_changes" not in params:
        raise GraphValidationError("INVALID_SETTINGS_REQUEST")
    if "changes" in params:
        changes = params["changes"]
        if type(changes) is not dict or not changes or set(changes) - EDITABLE:
            raise GraphValidationError("INVALID_SETTINGS_REQUEST")
        settings = graph["settings"]
        settings.update(copy.deepcopy(changes))
        # Editing a drawing setting requires explicit sizing confirmation again.
        settings["global_string_sizing_confirmed"] = False
        settings["extra"].pop("string_sizing", None)
    if "project_changes" in params:
        apply_project_changes(graph, params["project_changes"])
    old = {entity["id"]: entity for entity in entities(before)}
    changed = [entity for entity in entities(graph)
               if canonical_bytes(old.get(entity["id"])) != canonical_bytes(entity)]
    return advance(graph, changed, "solar-settings")
