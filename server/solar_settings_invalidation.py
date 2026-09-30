"""One invalidation rule for a change to the drawing's own settings (graph["settings"]).

Two writers change those settings: solar-settings (builtins/solar_settings.py) and a design preset
commit, through the plugin's SyncFromGlobalSettings rule (solar_preset_sync.sync). Both clear
sizing confirmation. Neither marked the outputs built under the old settings, so on a graph whose
frames carry no solve digest (extra.solve.upstream_sha256) the export guard,
solar_solve_results.require_current_export, still called those outputs current.

A change is material when any MATERIAL_SETTINGS field differs in its stored JSON value (canonical
bytes, so 1 and 1.0 differ: the comparison solar-settings already uses to find changed entities).
MATERIAL_SETTINGS is every field solar-settings may edit except string_number, the one editable
field solar_solve_results.upstream_basis leaves out, so a solved and an unsolved graph go out of
date on the same edits.

On a material change every homerun route (route_kind "start homerun" or "end homerun") and every
schedule becomes stale with the one reason "settings_changed": the outputs a tool regenerates
(solar-homeruns rebuilds the homeruns and records home_run_layer in each one's extra.layer,
solar-schedule rebuilds the schedule), the same kinds solar-assign-equipment and the string edits
already stale. Nothing else changes. A stale string or inverter never becomes valid again through
solar-assign-equipment (solar_equipment._validity keeps every non-EQUIPMENT_ reason and turns the
entity invalid, and an inverter cannot be removed), so staling one would lock the design. No tool
writes feeders or trenches yet, and feeders_follow_topology already guards a feeder at export.
Project, settings, zones, frames, panels, strings, inverters, feeders and trenches keep their
validity. An entity already stale keeps its state and reasons, so the first cause stays
(solar_project.py does the same).

One pass over the routes and schedules, no I/O, no allocation beyond the returned list.
"""
from leaf_cloud_client import canonical_bytes
from solar_solve_results import HOMERUN_KINDS

REASON = "settings_changed"
# Every field solar-settings may edit (builtins/solar_settings.py EDITABLE) except string_number.
MATERIAL_SETTINGS = ("panel_layer_contains", "panel_group_layer", "string_layer", "home_run_layer",
                     "panels_in_sequence", "num_mppt", "strings_per_mppt", "optimizer_ratio",
                     "use_l2_collectors", "panel_group_number", "inverter_number", "mppt_letter")


def material_changes(before, after):
    """The MATERIAL_SETTINGS keys whose stored JSON value differs, in MATERIAL_SETTINGS order.
    Both arguments are settings objects of validated graphs, which carry every key."""
    return tuple(key for key in MATERIAL_SETTINGS
                 if canonical_bytes(before[key]) != canonical_bytes(after[key]))


def settings_dependents(graph):
    """The entities a material settings change makes stale: homerun routes, then schedules."""
    return ([route for route in graph["routes"] if route["route_kind"] in HOMERUN_KINDS]
            + list(graph["schedules"]))


def invalidate_settings_dependents(graph, before_settings):
    """Mark graph's settings dependents stale when graph["settings"] differs materially from
    before_settings. Returns the entities this call changed, homerun routes then schedules, in
    graph order; an entity already stale is left as it is. [] when nothing material changed."""
    if not material_changes(before_settings, graph["settings"]):
        return []
    changed = []
    for entity in settings_dependents(graph):
        if entity["validity"]["state"] != "stale":
            entity["validity"] = {"state": "stale", "reasons": [REASON]}
            changed.append(entity)
    return changed
