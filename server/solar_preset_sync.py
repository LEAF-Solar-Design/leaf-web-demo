"""The two-store rule between a drawing's preset settings and the design graph's own settings.

The plugin keeps a design preset's settings in Properties.Settings.Default (what LEAFPROFILE Swap
rewrites through ProjectPreset.ApplyToSettings) and the drawing's own settings in
DrawingPropertiesJson (what Studio's graph["settings"] mirrors: the electrical bridge names
num_mppt and strings_per_mppt "NumMppt" and "StringPerMppt", the DrawingPropertiesJson spelling).
DrawingPropertiesJson.SyncFromGlobalSettings (DrawingPropertiesJson.cs:602-663) is the plugin's
standing path from the first store to the second; it runs when a drawing's properties load (:575)
and at the entry of the stringing commands (BranchCmd.cs:1560, 6770, 17214, 18890):

  always overwritten   StringLayer, HomeRunLayer, PanelGroupLayer, UseL2Collectors (:608-623)
  seeded when <= 0     PanelsInSequence from NumPanelsInSequence (:631-634), StringPerMppt from
                       StringsPerMppt when that is > 0 (:638-641), NumMppt when the preset's is > 0
                       (:646-649), OptimizerRatio from ParseNumerator (:657-662)

So after every preset commit Studio applies that rule to graph["settings"] (sync), and before one
it reads the three always-overwritten layer names back from the graph (effective_current), because
in the plugin those three live in Settings.Default (the settings form writes them there,
BranchSettings.cs) and the graph is where Studio edits them (solar-settings). Everything else the
plugin keeps only in Settings.Default stays in the store.

Not synced, each for a stated reason: use_l2_collectors (the graph contract requires every
inverter to carry an equipment_type in L2 mode, EQUIPMENT_TYPE_REQUIRED, so flipping the mode is
a topology edit, the declared follow-up sf-w2-design-presets-l2-mode); optimizer_ratio (the graph
schema requires it > 0, so the <= 0 seed can never fire); panel_layer_contains
(SyncFromGlobalSettings never reads ModuleLayer; the plugin keeps PanelLayerContains per drawing);
project.installation_design (a project field solar-settings cannot edit; the declared follow-up
sf-w2-design-presets-installation); the counters panel_group_number, string_number,
inverter_number and mppt_letter (DrawingStateSnapshot fields the store keeps at the plugin's
defaults). Pure, constant work, no I/O.
"""
import solar_preset_store as preset_store
from solar_design_graph import GraphValidationError

GRAPH_SETTINGS_OUT_OF_RANGE = "DESIGN_PRESET_GRAPH_SETTINGS_OUT_OF_RANGE"
# (graph settings key, preset settings field), always overwritten by SyncFromGlobalSettings.
ALWAYS = (("string_layer", "StringLayer"), ("home_run_layer", "HomeRunLayer"),
          ("panel_group_layer", "PanelGroupLayer"))
# (graph settings key, preset settings field, the preset value must be > 0), seeded only when the
# graph's own value is <= 0.
SEEDED = (("panels_in_sequence", "NumPanelsInSequence", False),
          ("strings_per_mppt", "StringsPerMppt", True),
          ("num_mppt", "NumMppt", True))


def effective_current(graph, stored, *, check_bounds=True):
    """The drawing's current preset settings: the stored ones with the three always-synced layer
    names read from graph["settings"]. None stays None. When check_bounds is True, a graph value
    the store cannot hold (a layer name over 256 characters) is
    DESIGN_PRESET_GRAPH_SETTINGS_OUT_OF_RANGE. Reads can return those values without persisting."""
    if stored is None:
        return None
    current = dict(stored)
    settings = graph["settings"]
    for key, field in ALWAYS:
        current[field] = settings[key]
    if check_bounds and preset_store.settings_error(current) is not None:
        raise GraphValidationError(GRAPH_SETTINGS_OUT_OF_RANGE)
    return current


def sync(graph, current):
    """SyncFromGlobalSettings from `current` onto graph["settings"], in place, through the
    solar-settings rule (builtins/solar_settings.py): when any value changes, sizing confirmation
    is cleared and settings.extra.string_sizing dropped. Returns the changed entities for advance
    ([] or [settings])."""
    settings = graph["settings"]
    changes = {}
    for key, field in ALWAYS:
        if settings[key] != current[field]:
            changes[key] = current[field]
    for key, field, positive in SEEDED:
        value = current[field]
        if settings[key] <= 0 and (value > 0 or not positive) and settings[key] != value:
            changes[key] = value
    if not changes:
        return []
    settings.update(changes)
    # Editing a drawing setting requires explicit sizing confirmation again (solar_settings.py).
    settings["global_string_sizing_confirmed"] = False
    settings["extra"].pop("string_sizing", None)
    return [settings]
