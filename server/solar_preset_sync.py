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
layer names, the L1/L2 mode and the installation design back from the graph (effective_current),
because in the plugin those five live in Settings.Default (the settings form writes them there,
BranchSettings.cs, InstallationDesign into both stores at :802-803) and the graph is where Studio
holds them.
Everything else the plugin keeps only in Settings.Default stays in the store.

Installation design. The plugin resolves the stored text with BranchCmdCore.SetInstallationDesign
(:3091-3096): anything but exactly "Ground" or "Roof" is "Ground", and BranchSettings
ApplyProjectPreset (:1199-1200) reads every value but "Roof" as Ground, so installation_design(text)
is "Roof" for exactly "Roof" and "Ground" for every other string. The plugin lets a drawing hold
panel groups of both designs (each panelGroupJson keeps its own); the graph does not: every frame
carries the project's design (INSTALLATION_DESIGN_MISMATCH), an unmarked Ground frame must carry a
tracker; the explicit PVcase captured-matrix variant carries neither tracker nor ground_slots.
A Roof frame has no tracker, so no frame can be converted. The frozen policy, applied before anything is
written: when the preset's resolved design equals the drawing's, nothing changes; when it differs on
a drawing with no frame, project.installation_design takes it through the project-change rule
(solar_project.set_installation_design: sizing confirmation cleared, voc_cold reseeded, derived
outputs stale "project_changed"); when it differs on a drawing with any frame, the commit is refused
with DESIGN_PRESET_INSTALLATION_POPULATED and the drawing, the store and every preset stay as they
were. That holds for a Swap, for the delete of the active preset, and for supplied current_settings
(they are the drawing's settings at that moment, written through like the layer names).

L1/L2 mode. UseL2Collectors is written to settings.use_l2_collectors like the layer names, and the
graph contract ties the inverters to it: in L2 mode every inverter carries an equipment_type
(EQUIPMENT_TYPE_REQUIRED), in L1 mode none does (L2_MODE_REQUIRED), and an untyped inverter is never
an L2 (the schema's is_l2 const false). So the mode and the equipment types change together, in the
same commit. The frozen policy, applied before anything is written: when the preset's mode equals
the drawing's, nothing changes. Entering L2 mode types every inverter (all untyped, all L1) as an
unconnected "string_inverter" (l2_ref null), which is how the electrical bridge already reads an
untyped inverter (no combiner box input count, solar_electrical_state_bridge.py); the graph is then
validated, so two inverters sharing a number are refused with DUPLICATE_EQUIPMENT_NUMBER. Leaving L2
mode removes equipment_type and l2_ref from every inverter, and is refused with
DESIGN_PRESET_L2_EQUIPMENT_PRESENT when any inverter is a combiner box or a central inverter, or
names an L2: the plugin keeps those blocks and ignores them in L1 mode, the graph cannot hold them
in L1 mode, and dropping them would lose the collector topology and every feeder. Both directions
change the settings through the solar-settings rule, so the homeruns and schedules go stale
("settings_changed"); a retyped inverter keeps its validity and its input assignments.

Not synced, each for a stated reason: optimizer_ratio (the graph
schema requires it > 0, so the <= 0 seed can never fire); panel_layer_contains
(SyncFromGlobalSettings never reads ModuleLayer; the plugin keeps PanelLayerContains per drawing);
the counters panel_group_number, string_number, inverter_number and mppt_letter
(DrawingStateSnapshot fields the store keeps at the plugin's defaults). Pure, no I/O: constant work,
plus one pass over the routes and schedules when a setting changes (solar_settings_invalidation.py),
one pass over the inverters when the mode changes, and one pass over the entities when the
installation design changes (solar_project.py).
"""
from leaf_cloud_client import canonical_bytes
import solar_preset_store as preset_store
from solar_design_graph import GraphValidationError, entities
from solar_project import set_installation_design
from solar_settings_invalidation import invalidate_settings_dependents

GRAPH_SETTINGS_OUT_OF_RANGE = "DESIGN_PRESET_GRAPH_SETTINGS_OUT_OF_RANGE"
INSTALLATION_POPULATED = "DESIGN_PRESET_INSTALLATION_POPULATED"
L2_EQUIPMENT_PRESENT = "DESIGN_PRESET_L2_EQUIPMENT_PRESENT"
# (graph settings key, preset settings field), always overwritten by SyncFromGlobalSettings.
ALWAYS = (("string_layer", "StringLayer"), ("home_run_layer", "HomeRunLayer"),
          ("panel_group_layer", "PanelGroupLayer"), ("use_l2_collectors", "UseL2Collectors"))
# The equipment type every inverter takes on entering L2 mode (an L1 device that feeds no L2).
L1_ENTRY_TYPE = "string_inverter"
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
    names and UseL2Collectors read from graph["settings"] and InstallationDesign read from
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


def leaves_l2_blocked(graph):
    """True when the graph's equipment cannot be held in L1 mode: an inverter that is not an
    unconnected string inverter (a combiner box, a central inverter, or one naming an L2)."""
    return any(inverter.get("equipment_type") != L1_ENTRY_TYPE or inverter.get("l2_ref") is not None
               for inverter in graph["inverters"])


def retype_inverters(graph, l2_mode):
    """Give every inverter the equipment typing l2_mode requires, in place; returns the inverters
    this changed, in graph order. Entering L2 mode, an untyped inverter becomes an unconnected
    string inverter; leaving it, equipment_type and l2_ref are removed. Callers refuse first
    (leaves_l2_blocked) where the result could not be valid."""
    changed = []
    for inverter in graph["inverters"]:
        if l2_mode and "equipment_type" not in inverter:
            inverter["equipment_type"] = L1_ENTRY_TYPE
            inverter["l2_ref"] = None
            changed.append(inverter)
        elif not l2_mode and "equipment_type" in inverter:
            del inverter["equipment_type"]
            inverter.pop("l2_ref", None)
            changed.append(inverter)
    return changed


def sync(graph, current):
    """SyncFromGlobalSettings from `current` onto graph["settings"] and the project's installation
    design, in place. Settings go through the solar-settings rule (builtins/solar_settings.py):
    when any value changes, sizing confirmation is cleared, settings.extra.string_sizing dropped and
    the design outputs stale (solar_settings_invalidation.py). A different resolved installation
    design is refused with DESIGN_PRESET_INSTALLATION_POPULATED on a drawing with any frame, before
    anything is written, and otherwise written through solar_project.set_installation_design. A
    different UseL2Collectors retypes every inverter in the same commit (retype_inverters); leaving
    L2 mode with equipment L1 mode cannot hold is refused with DESIGN_PRESET_L2_EQUIPMENT_PRESENT,
    after the design check and before anything is written.
    Returns the changed entities for advance: [], or [settings] followed by every entity the
    settings-change rule staled and then every retyped inverter, or, when the design changed, every
    entity whose canonical bytes changed, in entities() order."""
    design = installation_design(current["InstallationDesign"])
    flip = design != graph["project"]["installation_design"]
    if flip and graph["frames"]:
        raise GraphValidationError(INSTALLATION_POPULATED)
    l2_mode = current["UseL2Collectors"]
    mode_flip = l2_mode != graph["settings"]["use_l2_collectors"]
    if mode_flip and not l2_mode and leaves_l2_blocked(graph):
        raise GraphValidationError(L2_EQUIPMENT_PRESENT)
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
    if mode_flip:
        changed += retype_inverters(graph, l2_mode)
    if not flip:
        return changed
    set_installation_design(graph, design)
    return [entity for entity in entities(graph) if canonical_bytes(entity) != before.get(entity["id"])]
