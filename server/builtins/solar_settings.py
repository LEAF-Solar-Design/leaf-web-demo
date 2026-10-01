"""Drawing-owned settings candidate for the existing mutation transaction.

A material settings change stales the design outputs built under the old settings
(solar_settings_invalidation.py); a project change keeps its own rule (solar_project.py).

L1/L2 mode. The graph contract ties the inverters to settings.use_l2_collectors: in L2 mode every
inverter carries an equipment_type (EQUIPMENT_TYPE_REQUIRED), in L1 mode none does
(L2_MODE_REQUIRED). So a boolean use_l2_collectors that differs from the drawing's mode moves the
inverters in the same commit, by the design-preset rule (solar_preset_sync.py): entering L2 mode types
every inverter as an unconnected string inverter (equipment_type "string_inverter", l2_ref null),
then the graph is validated (two inverters sharing a number are DUPLICATE_EQUIPMENT_NUMBER); leaving
it removes equipment_type and l2_ref, and is refused with DESIGN_PRESET_L2_EQUIPMENT_PRESENT before
anything is written when any inverter is a combiner box or a central inverter or names an L2. An
equal value, or a value that is not a boolean, takes the path it always took (the latter is refused
by graph validation). One pass over the inverters when the mode changes, none otherwise."""
import copy

from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError, _bounded_json, entities
from solar_project import apply_project_changes, normalize_project_changes
from solar_preset_sync import L2_EQUIPMENT_PRESENT, leaves_l2_blocked, retype_inverters
from solar_settings_invalidation import invalidate_settings_dependents
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
        mode = changes.get("use_l2_collectors")
        # Only a boolean that differs from the drawing's mode retypes the inverters.
        mode_flip = type(mode) is bool and mode != settings["use_l2_collectors"]
        if mode_flip and not mode and leaves_l2_blocked(graph):
            raise GraphValidationError(L2_EQUIPMENT_PRESENT)
        settings.update(copy.deepcopy(changes))
        # Editing a drawing setting requires explicit sizing confirmation again.
        settings["global_string_sizing_confirmed"] = False
        settings["extra"].pop("string_sizing", None)
        if mode_flip:
            retype_inverters(graph, mode)
    if "project_changes" in params:
        apply_project_changes(graph, params["project_changes"])
    invalidate_settings_dependents(graph, before["settings"])
    old = {entity["id"]: entity for entity in entities(before)}
    changed = [entity for entity in entities(graph)
               if canonical_bytes(old.get(entity["id"])) != canonical_bytes(entity)]
    return advance(graph, changed, "solar-settings")
