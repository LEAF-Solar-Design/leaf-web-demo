"""The two-store rule between a drawing's preset settings and the design graph's own settings.

The plugin keeps a design preset's settings in Properties.Settings.Default (what LEAFPROFILE Swap
rewrites through ProjectPreset.ApplyToSettings) and the drawing's own settings in
DrawingPropertiesJson (what Studio's graph["settings"] mirrors: the electrical bridge names
num_mppt and strings_per_mppt "NumMppt" and "StringPerMppt", the DrawingPropertiesJson spelling).
DrawingPropertiesJson.SyncFromGlobalSettings (DrawingPropertiesJson.cs:602-663) is the plugin's
standing path from the first store to the second; it runs when a drawing's properties load (:575)
and at the entry of the stringing commands (BranchCmd.cs:1560, 6770, 17214, 18890):

  always overwritten   StringLayer, HomeRunLayer, PanelGroupLayer, InstallationDesign,
                       UseL2Collectors (:608-623)
  seeded when <= 0     PanelsInSequence from NumPanelsInSequence (:631-634), StringPerMppt from
                       StringsPerMppt when that is > 0 (:638-641), NumMppt when the preset's is > 0
                       (:646-649), OptimizerRatio from ParseNumerator (:657-662)

So after every preset commit Studio applies that rule to graph["settings"] and
graph["project"]["installation_design"] (sync), and before one it reads the three always-overwritten
layer names and the installation design back from the graph (effective_current), because in the
plugin those four live in Settings.Default (the settings form writes them there, BranchSettings.cs,
InstallationDesign into both stores at :802-803) and the graph is where Studio holds them.
Everything else the plugin keeps only in Settings.Default stays in the store.

Installation design. The plugin resolves the stored text with BranchCmdCore.SetInstallationDesign
(:3091-3096): anything but exactly "Ground" or "Roof" is "Ground", and BranchSettings
ApplyProjectPreset (:1199-1200) reads every value but "Roof" as Ground, so installation_design(text)
is "Roof" for exactly "Roof" and "Ground" for every other string. The plugin lets a drawing hold
panel groups of both designs (each panelGroupJson keeps its own); the graph does not: every frame
carries the project's design (INSTALLATION_DESIGN_MISMATCH), a Ground frame must carry a tracker and
a Roof frame must not, so no frame can be converted. The frozen policy, applied before anything is
written: when the preset's resolved design equals the drawing's, nothing changes; when it differs on
a drawing with no frame, project.installation_design takes it through the project-change rule
(solar_project.set_installation_design: sizing confirmation cleared, voc_cold reseeded, derived
outputs stale "project_changed"); when it differs on a drawing with any frame, the commit is refused
with DESIGN_PRESET_INSTALLATION_POPULATED and the drawing, the store and every preset stay as they
were. That holds for a Swap, for the delete of the active preset, and for supplied current_settings
(they are the drawing's settings at that moment, written through like the layer names).

Not synced, each for a stated reason: use_l2_collectors (the graph contract requires every
inverter to carry an equipment_type in L2 mode, EQUIPMENT_TYPE_REQUIRED, so flipping the mode is
a topology edit, the declared follow-up sf-w2-design-presets-l2-mode); optimizer_ratio (the graph
schema requires it > 0, so the <= 0 seed can never fire); panel_layer_contains
(SyncFromGlobalSettings never reads ModuleLayer; the plugin keeps PanelLayerContains per drawing);
the counters panel_group_number, string_number, inverter_number and mppt_letter
(DrawingStateSnapshot fields the store keeps at the plugin's defaults). Pure, no I/O: constant work,
plus one pass over the routes and schedules when a setting changes (solar_settings_invalidation.py)
and one pass over the entities when the installation design changes (solar_project.py).
"""
from leaf_cloud_client import canonical_bytes
import solar_preset_store as preset_store
from solar_design_graph import GraphValidationError, entities
from solar_project import set_installation_design
from solar_settings_invalidation import invalidate_settings_dependents

GRAPH_SETTINGS_OUT_OF_RANGE = "DESIGN_PRESET_GRAPH_SETTINGS_OUT_OF_RANGE"
INSTALLATION_POPULATED = "DESIGN_PRESET_INSTALLATION_POPULATED"
# (graph settings key, preset settings field), always overwritten by SyncFromGlobalSettings.
ALWAYS = (("string_layer", "StringLayer"), ("home_run_layer", "HomeRunLayer"),
          ("panel_group_layer", "PanelGroupLayer"))
# (graph settings key, preset settings field, the preset value must be > 0), seeded only when the
# graph's own value is <= 0.
SEEDED = (("panels_in_sequence", "NumPanelsInSequence", False),
          ("strings_per_mppt", "StringsPerMppt", True),
          ("num_mppt", "NumMppt", True))


def installation_design(text):
    """The design the plugin resolves a stored InstallationDesign to: "Roof" for exactly "Roof",
    "Ground" for every other string (SetInstallationDesign, ApplyProjectPreset)."""
    return "Roof" if text == "Roof" else "Ground"


def effective_current(graph, stored, *, check_bounds=True):
    """The drawing's current preset settings: the stored ones with the three always-synced layer
    names read from graph["settings"] and InstallationDesign read from
    graph["project"]["installation_design"]. None stays None. When check_bounds is True, a graph
    value the store cannot hold (a layer name over 256 characters) is
    DESIGN_PRESET_GRAPH_SETTINGS_OUT_OF_RANGE. Reads can return those values without persisting."""
    if stored is None:
        return None
    current = dict(stored)
    settings = graph["settings"]
    for key, field in ALWAYS:
        current[field] = settings[key]
    current["InstallationDesign"] = graph["project"]["installation_design"]
    if check_bounds and preset_store.settings_error(current) is not None:
        raise GraphValidationError(GRAPH_SETTINGS_OUT_OF_RANGE)
    return current


def sync(graph, current):
    """SyncFromGlobalSettings from `current` onto graph["settings"] and the project's installation
    design, in place. Settings go through the solar-settings rule (builtins/solar_settings.py):
    when any value changes, sizing confirmation is cleared, settings.extra.string_sizing dropped and
    the design outputs stale (solar_settings_invalidation.py). A different resolved installation
    design is refused with DESIGN_PRESET_INSTALLATION_POPULATED on a drawing with any frame, before
    anything is written, and otherwise written through solar_project.set_installation_design.
    Returns the changed entities for advance: [], or [settings] followed by every entity the
    settings-change rule staled, or, when the design changed, every entity whose canonical bytes
    changed, in entities() order."""
    design = installation_design(current["InstallationDesign"])
    flip = design != graph["project"]["installation_design"]
    if flip and graph["frames"]:
        raise GraphValidationError(INSTALLATION_POPULATED)
    # A design change happens only on a drawing with no frame, so this snapshot is small.
    before = {entity["id"]: canonical_bytes(entity) for entity in entities(graph)} if flip else None
    settings = graph["settings"]
    changes = {}
    for key, field in ALWAYS:
        if settings[key] != current[field]:
            changes[key] = current[field]
    for key, field, positive in SEEDED:
        value = current[field]
        if settings[key] <= 0 and (value > 0 or not positive) and settings[key] != value:
            changes[key] = value
    changed = []
    if changes:
        previous = dict(settings)
        settings.update(changes)
        # Editing a drawing setting requires explicit sizing confirmation again (solar_settings.py).
        settings["global_string_sizing_confirmed"] = False
        settings["extra"].pop("string_sizing", None)
        changed = [settings] + invalidate_settings_dependents(graph, previous)
    if not flip:
        return changed
    set_installation_design(graph, design)
    return [entity for entity in entities(graph) if canonical_bytes(entity) != before.get(entity["id"])]
