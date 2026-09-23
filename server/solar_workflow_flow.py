"""Studio's Rooftop workflow flow: a pure progression engine (contract G34, capability
leaf-workflow-palette).

A LITERAL port of the plugin's Rooftop palette flow (Branch2025, cited by file:line against
Palette/): the ordered step list (FlowDefinitions.cs:42-62), each step's CanAdvance and
HasDrawingPrerequisites (Steps/*StepPanel.cs, with Steps/PaletteZoneAdvanceGate.cs and
Steps/PanelGroupStringLengthGate.cs), and the controller's navigation semantics
(PaletteController.cs: AdvanceStep 946-973, GoBack 978-1006, NavigateToStep 865-899,
TestJumpToStep 913-940, SetFlow 484-537, ValidateDrawingState 1020-1064), plus the BTHost ops the
G34a capture drives (TestsUI/Shared/BTHostEntry.cs: PaletteSetStep 7161-7397, PaletteRefresh
7492-7523, PaletteBack 7473-7486).

The controller keeps TWO status layers, and the port keeps both: each step object's in-memory
`Status` (Steps[i].Status) and the per-user state file's `StepStatuses` dictionary (absent key =
not-started). GoBack and NavigateToStep read the in-memory layer (PaletteController.cs:993, 886);
ValidateDrawingState reads the file layer (:1028); BTHost's palette_set_step clears and rewrites
the file layer (BTHostEntry.cs:7291-7303) but TestJumpToStep only ever sets earlier steps Complete
in memory (PaletteController.cs:925-932) and never clears later ones. So after the G34a capture
loop (palette_set_step 0..7) steps 0..6 stay Complete IN MEMORY, the jump to step 1 rewrites the
file to {0: complete}, and the Back from step 2 marks the file's steps 2..6 Stale although the file
never held them Complete: the observed plugin behaviour, reproduced here literally.

The plugin recomputes every gate from the drawing on each step activation; this engine takes the
same drawing facts as an intake (`validate_facts`) and derives the same answers. The intake is
exactly the plugin adapter's G34 intake (docs/parity/evidence/rooftop/flow/*.json) with G34b's
host input `host_use_l2_collectors`; each field and the plugin source it mirrors:

  flow                    "rooftop": the flow the facts were read under (FlowDefinitions.cs:42)
  electrical_zones        DrawingProperties.Default.ElectricalZones in stored order, each {name
                          (Name, null when absent), panels (PanelHandles, null read as empty),
                          panels_in_sequence (PanelsInSequence)} (ZonesStepPanel.cs:812)
  elevation_zones         DrawingProperties.Default.ElevationZones, each {name, panels}
                          (ZonesStepPanel.cs:813)
  missing_zone_panels     {electrical, elevation}: CountMissingZonePanelHandles over each zone
                          set's references (ZonesStepPanel.cs:816-817)
  panels_in_sequence      DrawingProperties.Default.PanelsInSequence (ZonesStepPanel.cs:814,
                          PanelGroupsStepPanel.cs:603)
  global_string_sizing_confirmed
                          DrawingProperties.Default.GlobalStringSizingConfirmed
                          (ZonesStepPanel.cs:815, PanelGroupsStepPanel.cs:604)
  panel_groups            CountPanelGroupBlocks (PanelGroupsStepPanel.cs:41, 596)
  strings                 {polylines: SolveStepPanel.ReadStringLengthBuckets's total, the Solve
                          gate (SolveStepPanel.cs:34, 476-478); paths: PaletteDrawingCounts
                          .ReadSnapshot's scan count (Steps/PaletteDrawingCounts.cs:72-74);
                          persisted: ReadPersistedSolvedStringCount, the next string number minus
                          one, which ReadSnapshot reports when its scan finds none (:92-93, 121-133)}
  inverters               App.gInverterList.Count after GetAllInverters, the L1 list
                          (InverterStepPanel.cs:590)
  l2_collectors           App.gL2CollectorList.Count (InverterStepPanel.cs:591)
  combiners               the Combiners step's count, gInverterList.Count
                          (CombinerStepPanel.cs:411, 55)
  homeruns                model-space entities on the homerun layers (HomerunsStepPanel.cs:25-66)
  host_use_l2_collectors  the capture host's per-user setting Properties.Settings.Default
                          .UseL2Collectors (G34b), the value the Equipment and Combiners gates read
                          (InverterStepPanel.cs:35 via _useL2, set at :586; CombinerStepPanel.cs:41,
                          49). Every L1/L2 branch below reads this field.
  use_l2_collectors       the drawing's mirrored copy of UseL2Collectors. Carried for the intake's
                          shape only: no gate reads it (G34b).

One gate input is per-user state, not a drawing fact, and is NOT in the intake: the string-length
setting Properties.Settings.Default.NumPanelsInSequence that PanelGroupStringLengthGate compares
(PanelGroupsStepPanel.cs:605, 618-622). The flow never reads it unset: SetFlow activates the Zones
step (PaletteController.cs:531-534), whose activation overwrites the setting with the drawing's
length whenever that length is confirmed (ZonesStepPanel.cs:818, 1731-1744), and the gate stops
before the comparison when it is not (PanelGroupStringLengthGate.cs:39-45). A restore that lands
past the Zones step without a known setting refuses rather than guess.

Scope: a real drawing (demo mode off: ValidateDrawingState returns at once in demo mode,
PaletteController.cs:1022, and the steps read canned results); the Rooftop flow only.

Fails closed: a malformed intake or an out-of-range navigation argument raises FlowInputError.
Pure: no I/O, no clock, no randomness; the engine holds only its own navigation state.
"""
from __future__ import annotations

import re
import unicodedata

FLOW = "rooftop"
# FlowDefinitions.cs:51-61, in order, under neutral names (G34): Zones, Panel Groups, Solve,
# Equipment (InverterStepPanel), Combiners, Homeruns, Export, Project Summary (ReOptStepPanel).
STEP_NAMES = ("zones", "panel-groups", "solve", "inverters", "combiners", "homeruns", "export", "reopt")
STEP_COUNT = len(STEP_NAMES)

# IStepPanel.cs StepStatus {NotStarted, Complete, Stale}, under G34's names.
NOT_STARTED = "not-started"
COMPLETE = "complete"
STALE = "stale"
STATUSES = (NOT_STARTED, COMPLETE, STALE)

# Bounds: a drawing's zone tables and counts, far past any real drawing, still refuse a runaway.
MAX_ZONES = 10_000
MAX_ZONE_PANELS = 2_000_000
MAX_NAME_CHARS = 1024
MAX_PANEL_REF_CHARS = 1024
INT32_MIN, INT32_MAX = -(2 ** 31), 2 ** 31 - 1

COUNT_FIELDS = ("panel_groups", "inverters", "l2_collectors", "combiners", "homeruns")
BOOL_FIELDS = ("global_string_sizing_confirmed", "host_use_l2_collectors", "use_l2_collectors")
LIST_FIELDS = ("electrical_zones", "elevation_zones")
MISSING_FIELDS = frozenset({"electrical", "elevation"})
STRING_FIELDS = frozenset({"polylines", "paths", "persisted"})
INTAKE_FIELDS = frozenset(COUNT_FIELDS + BOOL_FIELDS + LIST_FIELDS
                          + ("flow", "missing_zone_panels", "panels_in_sequence", "strings"))
ELECTRICAL_ZONE_FIELDS = frozenset({"name", "panels", "panels_in_sequence"})
ELEVATION_ZONE_FIELDS = frozenset({"name", "panels"})


class FlowInputError(ValueError):
    """A malformed intake or navigation argument: a named refusal, never a flow verdict."""


# ------------------------------------------------------------------ intake --

def _int(value, where, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        raise FlowInputError(f"{where} must be an integer in {lo}..{hi}")
    return value


def _counts(raw, fields, where):
    if not isinstance(raw, dict) or set(raw) != fields:
        raise FlowInputError(f"{where} must be an object with exactly {sorted(fields)}")
    return {key: _int(raw[key], f"{where}.{key}", 0, INT32_MAX) for key in sorted(fields)}


def _zone(raw, fields, where, panel_budget):
    """One stored zone, copied: name as stored (None when absent), its panel references, and for
    an electrical zone PanelsInSequence."""
    if not isinstance(raw, dict) or set(raw) != fields:
        raise FlowInputError(f"{where} must be an object with exactly {sorted(fields)}")
    name = raw["name"]
    if name is not None and (not isinstance(name, str) or len(name) > MAX_NAME_CHARS):
        raise FlowInputError(f"{where}.name must be null or a string of at most {MAX_NAME_CHARS} chars")
    panels = raw["panels"]
    if not isinstance(panels, list) or not all(isinstance(p, str) and len(p) <= MAX_PANEL_REF_CHARS
                                               for p in panels):
        raise FlowInputError(f"{where}.panels must be a list of panel reference strings")
    panel_budget[0] -= len(panels)
    if panel_budget[0] < 0:
        raise FlowInputError(f"the zones carry more than {MAX_ZONE_PANELS} panel references")
    zone = {"name": name, "panels": list(panels)}
    if "panels_in_sequence" in fields:
        zone["panels_in_sequence"] = _int(raw["panels_in_sequence"], f"{where}.panels_in_sequence",
                                          INT32_MIN, INT32_MAX)
    return zone


def validate_facts(raw):
    """The G34 drawing-facts intake, validated and copied (fails closed on any other shape)."""
    if not isinstance(raw, dict) or set(raw) != INTAKE_FIELDS:
        missing = sorted(INTAKE_FIELDS - set(raw)) if isinstance(raw, dict) else []
        extra = sorted(set(raw) - INTAKE_FIELDS) if isinstance(raw, dict) else []
        raise FlowInputError(f"the flow intake must carry exactly {sorted(INTAKE_FIELDS)}"
                             f" (missing {missing}, unexpected {extra})")
    if raw["flow"] != FLOW:
        raise FlowInputError(f"flow must be {FLOW!r}")
    facts = {"flow": FLOW}
    for key in COUNT_FIELDS:
        facts[key] = _int(raw[key], key, 0, INT32_MAX)
    facts["panels_in_sequence"] = _int(raw["panels_in_sequence"], "panels_in_sequence", INT32_MIN, INT32_MAX)
    for key in BOOL_FIELDS:
        if type(raw[key]) is not bool:
            raise FlowInputError(f"{key} must be a boolean")
        facts[key] = raw[key]
    facts["missing_zone_panels"] = _counts(raw["missing_zone_panels"], MISSING_FIELDS, "missing_zone_panels")
    facts["strings"] = _counts(raw["strings"], STRING_FIELDS, "strings")
    budget = [MAX_ZONE_PANELS]
    for key, fields in (("electrical_zones", ELECTRICAL_ZONE_FIELDS), ("elevation_zones", ELEVATION_ZONE_FIELDS)):
        zones = raw[key]
        if not isinstance(zones, list) or len(zones) > MAX_ZONES:
            raise FlowInputError(f"{key} must be a list of at most {MAX_ZONES} zones")
        facts[key] = [_zone(z, fields, f"{key}[{i}]", budget) for i, z in enumerate(zones)]
    return facts


# ---------------------------------------------------------- .NET string rules --

_DOTNET_EXTRA_WHITESPACE = frozenset("\t\n\v\f\r\x85")


def _dotnet_trim(text):
    """string.Trim(): strips char.IsWhiteSpace (Zs, Zl, Zp, U+0009..U+000D, U+0085), which is
    narrower than Python's str.strip (that also strips U+001C..U+001F)."""
    def ws(ch):
        return ch in _DOTNET_EXTRA_WHITESPACE or unicodedata.category(ch) in ("Zs", "Zl", "Zp")
    start, end = 0, len(text)
    while start < end and ws(text[start]):
        start += 1
    while end > start and ws(text[end - 1]):
        end -= 1
    return text[start:end]


def _ordinal_ignore_case_key(text):
    """StringComparer.OrdinalIgnoreCase: per-char simple upper-casing (a char whose upper case
    is more than one char, like U+00DF, stays itself)."""
    out = []
    for ch in text:
        up = ch.upper()
        out.append(up if len(up) == 1 else ch)
    return "".join(out)


def _zone_names(zones):
    # PaletteZoneAdvanceGate.cs:84 / 99: z?.Name == null ? "" : z.Name.Trim()
    return [("" if z["name"] is None else _dotnet_trim(z["name"])) for z in zones]


# ------------------------------------------------------------------- gates --

def has_confirmed_global_string_sizing(panels_in_sequence, global_string_sizing_confirmed):
    """PaletteZoneAdvanceGate.HasConfirmedGlobalStringSizing (PaletteZoneAdvanceGate.cs:41-44)."""
    return panels_in_sequence > 0 and global_string_sizing_confirmed


def zone_advance_gate(facts):
    """PaletteZoneAdvanceGate.Evaluate (PaletteZoneAdvanceGate.cs:63-111): the status name of
    the first failing rule, or "Ready"."""
    electrical = facts["electrical_zones"]
    elevation = facts["elevation_zones"]
    missing = facts["missing_zone_panels"]
    if len(electrical) == 0 and not has_confirmed_global_string_sizing(
            facts["panels_in_sequence"], facts["global_string_sizing_confirmed"]):
        return "MissingGlobalStringSizing"                                         # :74-80
    if len(electrical) > 0:
        names = _zone_names(electrical)
        if any(len(n) == 0 for n in names):
            return "EmptyElectricalZoneName"                                       # :85-86
        if len(names) != len({_ordinal_ignore_case_key(n) for n in names}):
            return "DuplicateElectricalZoneName"                                   # :87-88
        if any(len(z["panels"]) == 0 for z in electrical):
            return "EmptyElectricalZonePanels"                                     # :89-90
        if missing["electrical"] > 0:
            return "MissingElectricalZonePanelHandles"                             # :91-92
        if any(z["panels_in_sequence"] <= 0 for z in electrical):
            return "MissingElectricalZoneSizing"                                   # :93-94
    if len(elevation) > 0:
        names = _zone_names(elevation)
        if any(len(n) == 0 for n in names):
            return "EmptyElevationZoneName"                                        # :100-101
        if len(names) != len({_ordinal_ignore_case_key(n) for n in names}):
            return "DuplicateElevationZoneName"                                    # :102-103
        if any(len(z["panels"]) == 0 for z in elevation):
            return "EmptyElevationZonePanels"                                      # :104-105
        if missing["elevation"] > 0:
            return "MissingElevationZonePanelHandles"                              # :106-107
    return "Ready"                                                                 # :110


def panel_group_string_length_gate(panel_group_count, has_electrical_zones, drawing_panels_in_sequence,
                                   global_string_sizing_confirmed, current_settings_string_length):
    """PanelGroupStringLengthGate.Evaluate (PanelGroupStringLengthGate.cs:29-54). The per-user
    setting is None when unknown; the gate refuses only when it would read it."""
    if panel_group_count <= 0 or has_electrical_zones:                             # :36-37
        return "Ready"
    if not has_confirmed_global_string_sizing(drawing_panels_in_sequence, global_string_sizing_confirmed):
        return "MissingDrawingStringSizing"                                        # :39-45
    if current_settings_string_length is None:
        raise FlowInputError("the per-user string-length setting is unknown and the panel-group gate reads it")
    if current_settings_string_length > 0 and current_settings_string_length != drawing_panels_in_sequence:
        return "StringLengthChangedAfterPanelGroups"                               # :46-51
    return "Ready"                                                                 # :53


def sync_settings_on_zones_activation(facts, settings_string_length):
    """ZonesStepPanel.SyncSettingsFromConfirmedGlobalStringSizing (ZonesStepPanel.cs:1731-1744),
    run on every Zones activation (:818): a confirmed drawing length overwrites the setting."""
    if not has_confirmed_global_string_sizing(facts["panels_in_sequence"], facts["global_string_sizing_confirmed"]):
        return settings_string_length
    return facts["panels_in_sequence"]


def step_can_advance(index, facts, settings_string_length=None):
    """Each Rooftop step's CanAdvance over the drawing facts."""
    _step_index(index)
    if index == 0:    # ZonesStepPanel.cs:29, 1033-1042
        return zone_advance_gate(facts) == "Ready"
    if index == 1:    # PanelGroupsStepPanel.cs:28, 496-500, 594-606
        gate = panel_group_string_length_gate(
            facts["panel_groups"], len(facts["electrical_zones"]) > 0, facts["panels_in_sequence"],
            facts["global_string_sizing_confirmed"], settings_string_length)
        return facts["panel_groups"] > 0 and gate == "Ready"
    if index == 2:    # SolveStepPanel.cs:34, 476-478: the string-layer buckets, no persisted fallback
        return facts["strings"]["polylines"] > 0
    if index == 3:    # InverterStepPanel.cs:35, 586-592: _useL2 is the per-user setting (:586),
        # _inverterCount is L1 + L2
        if facts["host_use_l2_collectors"]:
            return facts["l2_collectors"] > 0
        return facts["inverters"] + facts["l2_collectors"] > 0
    if index == 4:    # CombinerStepPanel.cs:37-44 (the per-user setting at :41), 411
        if not facts["host_use_l2_collectors"]:
            return True
        return facts["combiners"] > 0
    if index == 5:    # HomerunsStepPanel.cs:25, 301-325
        return facts["homeruns"] > 0
    return True       # ExportStepPanel.cs:24, ReOptStepPanel.cs:29


def step_has_prerequisites(index, facts):
    """Each Rooftop step's HasDrawingPrerequisites over the drawing facts."""
    _step_index(index)
    if index == 0:    # ZonesStepPanel.cs:56-73: an electrical zone with at least one panel reference
        return any(z["panels"] for z in facts["electrical_zones"])
    if index == 1:    # PanelGroupsStepPanel.cs:38-45
        return facts["panel_groups"] > 0
    if index == 2:    # SolveStepPanel.cs:36-43 -> CountStringPolylines -> PaletteDrawingCounts.cs:72-74, 92-93
        return facts["strings"]["paths"] > 0 or facts["strings"]["persisted"] > 0
    if index == 3:    # InverterStepPanel.cs:37-45
        return facts["inverters"] + facts["l2_collectors"] > 0
    if index == 4:    # CombinerStepPanel.cs:46-55 (the per-user setting at :49)
        if not facts["host_use_l2_collectors"]:
            return True
        return facts["combiners"] > 0
    if index == 5:    # HomerunsStepPanel.cs:26-66
        return facts["homeruns"] > 0
    return True       # ExportStepPanel.cs:25, ReOptStepPanel.cs:30 (action-only)


def _step_index(index):
    if type(index) is not int or not 0 <= index < STEP_COUNT:
        raise FlowInputError(f"step index must be an integer in 0..{STEP_COUNT - 1}")
    return index


_STATE_FILE_STATUS = {1: COMPLETE, 2: STALE}   # StepStatus {NotStarted 0, Complete 1, Stale 2}


def _state_statuses(raw):
    """A per-user file's StepStatuses, as {index: status}: a dict keyed by step index (int or
    decimal string, as the JSON file writes it) with values 0/1/2 or the G34 names. Keys outside
    the flow are dropped, as SetFlow's restore ignores them (PaletteController.cs:505); a
    NotStarted value is kept as absent."""
    if not isinstance(raw, dict) or len(raw) > 4 * STEP_COUNT:
        raise FlowInputError("state statuses must be an object of at most "
                             f"{4 * STEP_COUNT} step index entries")
    out = {}
    for key, value in raw.items():
        if isinstance(key, str) and re.fullmatch(r"-?[0-9]{1,10}", key):
            key = int(key)
        if type(key) is not int:
            raise FlowInputError("a state status key must be a step index")
        if value in STATUSES:
            status = value
        elif type(value) is int and value in (0, 1, 2):
            status = _STATE_FILE_STATUS.get(value, NOT_STARTED)
        else:
            raise FlowInputError(f"a state status must be 0, 1, 2 or one of {STATUSES}")
        if 0 <= key < STEP_COUNT and status != NOT_STARTED:
            out[key] = status
    return out


def _setting(value):
    if value is not None:
        _int(value, "settings_string_length", INT32_MIN, INT32_MAX)
    return value


# -------------------------------------------------------------- controller --

class RooftopFlow:
    """PaletteController's navigation over the Rooftop steps.

    `step_statuses[i]` is Steps[i].Status, the in-memory layer (a fresh step object starts
    NotStarted: FlowDefinitions.cs:51-61, IStepPanel Status initialisers). `state_statuses` is
    State.StepStatuses, the per-user file layer, {index: status}, a missing key reading as
    not-started. `state_index` is State.CurrentStepIndex, which PersistAndNotify copies from the
    controller's index (PaletteController.cs:1153-1158). `settings_string_length` is the per-user
    string-length setting the Zones activation syncs and the Panel Groups gate reads (None when
    unknown)."""

    def __init__(self, facts, settings_string_length):
        self.facts = facts
        self.settings_string_length = settings_string_length
        self.current_index = 0
        self.step_statuses = [NOT_STARTED] * STEP_COUNT
        self.state_statuses = {}
        self.state_index = 0

    # SetFlow(Rooftop), user-initiated (PaletteController.cs:484-537, restoring false): fresh step
    # objects, the file's statuses cleared, index 0, the Zones step activated.
    @classmethod
    def open(cls, facts, settings_string_length=None):
        facts = validate_facts(facts)
        flow = cls(facts, _setting(settings_string_length))
        flow._activate(0)                                                          # :531-534
        return flow

    # InitializeInstance (PaletteController.cs:125-134): SetFlow restoring from the file (:497-509,
    # fresh step objects take the file's statuses), then ValidateDrawingState.
    @classmethod
    def restore(cls, facts, state_index, state_statuses, settings_string_length=None):
        facts = validate_facts(facts)
        statuses = _state_statuses(state_statuses)
        if type(state_index) is not int:
            raise FlowInputError("state_index must be an integer")
        flow = cls(facts, _setting(settings_string_length))
        flow.state_statuses = statuses
        flow.state_index = state_index
        flow.current_index = max(0, min(state_index, STEP_COUNT - 1))              # :500
        for i, status in statuses.items():                                         # :503-509
            flow.step_statuses[i] = status
        flow._activate(flow.current_index)                                         # :531-534
        flow.validate_drawing_state()                                              # :134
        return flow

    def _set(self, index, status):
        """Steps[i].Status and State.StepStatuses[i] written together, as every controller
        mutation does (PaletteController.cs:888-889, 929-930, 962-963, 995-996)."""
        self.step_statuses[index] = status
        self.state_statuses[index] = status

    def _persist(self):
        self.state_index = self.current_index                                      # :1155

    def _activate(self, index):
        # Only the Zones step's activation writes state the gates read (ZonesStepPanel.cs:818).
        if index == 0:
            self.settings_string_length = sync_settings_on_zones_activation(self.facts, self.settings_string_length)

    def can_advance(self, index=None):
        return step_can_advance(self.current_index if index is None else index, self.facts,
                                self.settings_string_length)

    def has_prerequisites(self, index):
        return step_has_prerequisites(index, self.facts)

    def advance(self):
        """AdvanceStep (PaletteController.cs:946-973). True when the flow moved."""
        if self.current_index >= STEP_COUNT - 1:                                   # :952-953
            return False
        if not self.can_advance():                                                 # :955-956
            return False
        self._set(self.current_index, COMPLETE)                                    # :962-963
        self.current_index += 1                                                    # :967
        self._activate(self.current_index)                                         # :968
        self._persist()                                                            # :970
        return True

    def go_back(self):
        """GoBack (PaletteController.cs:978-1006): every step from the current one on whose
        IN-MEMORY status is Complete becomes Stale in both layers (:991-998). The file layer is
        not consulted, so a step the file never held Complete still turns Stale when its step
        object is Complete. True when the flow moved."""
        if self.current_index <= 0:                                                # :981-982
            return False
        for i in range(self.current_index, STEP_COUNT):                            # :991-998
            if self.step_statuses[i] == COMPLETE:
                self._set(i, STALE)
        self.current_index -= 1                                                    # :1000
        self._activate(self.current_index)                                         # :1001
        self._persist()                                                            # :1003
        return True

    def navigate_to_step(self, index):
        """NavigateToStep (PaletteController.cs:865-899): backward (or to the current step) only;
        steps after the target up to the current one whose in-memory status is Complete become
        Stale in both layers."""
        if type(index) is not int:
            raise FlowInputError("step index must be an integer")
        if index < 0 or index >= STEP_COUNT:                                       # :868-869
            return False
        if index > self.current_index:                                             # :873-874
            return False
        for i in range(index + 1, min(self.current_index, STEP_COUNT - 1) + 1):    # :884-891
            if self.step_statuses[i] == COMPLETE:
                self._set(i, STALE)
        self.current_index = index                                                 # :893
        self._activate(index)                                                      # :894
        self._persist()                                                            # :896
        return True

    def test_jump_to_step(self, index, mark_prior_complete=True):
        """TestJumpToStep (PaletteController.cs:913-940): every earlier step Complete in both
        layers; later steps keep whatever they held."""
        if type(index) is not int:
            raise FlowInputError("step index must be an integer")
        if index < 0 or index >= STEP_COUNT:                                       # :917
            return False
        if mark_prior_complete:                                                    # :925-932
            for i in range(index):
                self._set(i, COMPLETE)
        self.current_index = index                                                 # :934
        self._activate(index)                                                      # :935
        self._persist()                                                            # :937
        return True

    def palette_set_step(self, index):
        """BTHost's palette_set_step on a live Rooftop controller (BTHostEntry.cs:7161-7397, demo
        off, no flow switch): the index clamped to the flow (:7279-7282), the FILE layer cleared and
        rewritten with every earlier step Complete (:7287-7306), then TestJumpToStep(index, true)
        (:7315-7316). The in-memory layer is not cleared."""
        if type(index) is not int:
            raise FlowInputError("step index must be an integer")
        target = min(max(index, 0), STEP_COUNT - 1)
        self.state_index = target
        self.state_statuses = {i: COMPLETE for i in range(target)}
        self.test_jump_to_step(target, True)
        return target

    def palette_refresh(self):
        """BTHost's palette_refresh (BTHostEntry.cs:7492-7523): the current step re-activated."""
        self._activate(self.current_index)

    def validate_drawing_state(self):
        """ValidateDrawingState (PaletteController.cs:1020-1064): the first step the FILE layer
        holds Complete whose drawing prerequisites are gone (Stale steps are not checked, :1028),
        and every step after it, reset to NotStarted in both layers (the file keys removed), and
        the flow moves to that step. Returns the reset index or None."""
        reset_to = None
        for i in range(STEP_COUNT):                                                # :1026-1036
            if self.state_statuses.get(i) == COMPLETE and not self.has_prerequisites(i):
                reset_to = i
                break
        if reset_to is None:                                                       # :1038
            return None
        for i in range(reset_to, STEP_COUNT):                                      # :1041-1045
            self.step_statuses[i] = NOT_STARTED
            self.state_statuses.pop(i, None)
        self.current_index = reset_to                                              # :1051
        self._activate(reset_to)                                                   # :1052
        self._persist()                                                            # :1054
        return reset_to

    def reopen(self, facts=None):
        """A drawing opened into the running palette (OnDocumentActivated, PaletteController.cs:
        149-178): the flow alignment is a no-op for a Rooftop drawing, then the ValidateDrawingState
        cascade over the (possibly new) drawing facts (:174), then the current step re-activated.
        The flow keeps its index and both status layers. Returns the reset index or None."""
        if facts is not None:
            self.facts = validate_facts(facts)
        reset_to = self.validate_drawing_state()                                   # :174
        self._activate(self.current_index)
        return reset_to

    def statuses(self):
        """G34a `statuses`: the file layer as 8 entries (absent key = not-started)."""
        return [self.state_statuses.get(i, NOT_STARTED) for i in range(STEP_COUNT)]

    def snapshot(self):
        """The per-user file after an event: CurrentStepIndex and the statuses."""
        return {"current_index": self.state_index, "statuses": self.statuses()}


# ------------------------------------------------------------- G34 outputs --

def capture_steps(flow):
    """The G34a per-step capture on an opened flow: for each step, palette_set_step i, then
    palette_refresh, then the step's CanAdvance (palette_state's canAdvance). Leaves every step
    before the last Complete in the in-memory layer."""
    out = []
    for i in range(STEP_COUNT):
        flow.palette_set_step(i)
        flow.palette_refresh()
        out.append(flow.can_advance())
    return out


def flow_steps(facts):
    """G34a `flow-step` values for one drawing state, in step order: the flow opened, then the
    per-step capture."""
    flow = RooftopFlow.open(facts)
    return [{"flow": FLOW, "index": i, "step": STEP_NAMES[i], "can_advance": can}
            for i, can in enumerate(capture_steps(flow))]


def flow_events(facts, reopen_facts):
    """G34a `flow-event` values, the event script on one drawing state (the zones fixture) after
    its per-step capture: `jump` (palette_set_step to the first step whose can_advance is true,
    else 0, then palette_refresh), `advance` until an advance is refused (each row records
    `advanced`; the refused one is recorded too), one `back`, a `reopen` of the same drawing
    state, then a `reopen` of `reopen_facts` (the unsplit fixture) with the same persisted state.
    Every row carries the file's current_index and statuses after the event."""
    flow = RooftopFlow.open(facts)
    reopen_facts = validate_facts(reopen_facts)
    can = capture_steps(flow)
    start = next((i for i, c in enumerate(can) if c), 0)
    flow.palette_set_step(start)
    flow.palette_refresh()
    events = [dict(event="jump", **flow.snapshot())]
    for _ in range(STEP_COUNT):              # at most STEP_COUNT - 1 moves, then a refusal
        advanced = flow.advance()
        events.append(dict(event="advance", advanced=advanced, **flow.snapshot()))
        if not advanced:
            break
    flow.go_back()
    events.append(dict(event="back", **flow.snapshot()))
    flow.reopen()
    events.append(dict(event="reopen", **flow.snapshot()))
    flow.reopen(reopen_facts)
    events.append(dict(event="reopen", **flow.snapshot()))
    return events
