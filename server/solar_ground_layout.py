"""Studio port of the plugin's ground layout engines: LEAFMODULE, LEAFSPACING,
LEAFTRACK, LEAFSAT and LEAFSETBACK.

Literal ports of Branch2025 (read 2026-09-23 at C:/tmp/solar-parity/wt-b25-s17):

  Terrain/ModuleCommand.cs            module preset list, prompt defaults, the nine
                                      settings LEAFMODULE writes, GetActiveModule
  Terrain/RowSpacingCommand.cs        LEAFSPACING prompt defaults and its one write
  Terrain/RowSpacingCalculator.cs     ComputeMinimumPitch, PitchFromGcr, GcrFromPitch
  Terrain/BacktrackingCalculator.cs   SolarDeclination, ComputeShadeLimitAngle
  Terrain/TrackerCommand.cs           LEAFTRACK prompts, PromptOrUseStoredModule,
                                      PromptModuleDims, TryReadTerrain
  Terrain/TerrainSlopeQuery.cs        GetEWSlopeRad, the variable-pitch slope
  Terrain/TrackerRowGenerator.cs      Generate, ComputeGcr, PitchFromGcr (the sweep is
                                      server/solar_terrain.py generate_tracker_rows,
                                      already a line-for-line port of cs:341-579)
  Terrain/SatCommand.cs               LEAFSAT prompts and its one write; TrackerDrawer
                                      (the block reference per row and its row fields)
  LeafSolarDesign.Core/GcrPrompt.cs   the GCR default and the bounded re-prompt
  Pvcase/LeafSetbackCommand.cs        LEAFSETBACK kind, distance, OffsetInward
  DrawingPropertiesJson.cs            declared setting defaults (C# field defaults and
                                      the constructor, :499-560)
  DrawingProperties.cs                how a command loads the settings (:228, :245-251)
  HomerunRoutingConfig.cs             the routing settings default (:12-80)

Pure functions over plain data. No AutoCAD, no I/O, no network. The engines take
and return NEUTRAL structures only: drawing settings as a dict of product setting
names to JSON values, a boundary as its [x, y] vertices, the terrain grid as the
neutral grid of server/solar_ground_terrain.py, a tracker row as its placement
(insert, rotation, scale) and its named row fields. How the plugin encodes any of
those in a drawing is not part of this module.

Floating point work is ordered exactly as the C# orders it (IEEE doubles on both
sides, banker's rounding where C# uses Math.Round), so a pitch, a slope and a row
reproduce the plugin's numbers bit for bit.

Every input is bounded and every malformed input fails closed with
LayoutInputError (a ValueError); a bound breach raises LayoutBoundsError.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import math
from numbers import Real
from pathlib import Path


def _load_sibling(name):
    """Load a server module by path so the import works from any cwd."""
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_sweep = _load_sibling("solar_terrain")        # TrackerRowGenerator.Generate port
_ground = _load_sibling("solar_ground_terrain")  # units keyword, neutral grid

# ---------------------------------------------------------------------------
#  Constants (each cites the line that defines it)
# ---------------------------------------------------------------------------

# LEAFMODULE preset list: keyword -> (along m, cross m, Pmax W), ModuleCommand.cs:83-91.
MODULE_PRESETS = {"1": (1.000, 2.100, 400.0), "2": (1.134, 2.278, 540.0), "3": (1.303, 2.384, 665.0)}
MODULE_KEYWORDS = ("1", "2", "3", "Manual")          # ModuleCommand.cs:69-72
MODULE_DEFAULT_KEYWORD = "1"                          # ModuleCommand.cs:81
MODULE_PRESET_GAP_M = 0.020                           # ModuleCommand.cs:78
DEFAULT_RAIL_OVERHANG_M = 0.05                        # ModuleCommand.cs:101
DEFAULT_TORQUE_TUBE_HEIGHT_M = 1.5                    # ModuleCommand.cs:118
DEFAULT_PMAX_W = 550.0                                # ModuleCommand.cs:174
DEFAULT_TUBE_RADIUS_M = 0.08                          # ModuleCommand.cs:191
MANUAL_CROSS_M = 2.1                                  # ModuleCommand.cs:264
MANUAL_ALONG_M = 1.0                                  # ModuleCommand.cs:276
MANUAL_GAP_MM = 20.0                                  # ModuleCommand.cs:288

DEFAULT_LATITUDE_DEG = 35.0                           # RowSpacingCommand.cs:32, SatCommand.cs:149
LATITUDE_CLAMP_DEG = 89.9                             # RowSpacingCommand.cs:50
DEFAULT_FRAME_LENGTH_M = 2.0                          # RowSpacingCommand.cs:56
FRAME_LENGTH_FROM_MODULE = (0.1, 10.0)                # RowSpacingCommand.cs:61, exclusive
DEFAULT_TILT_DEG = 20.0                               # RowSpacingCommand.cs:85
MAX_TILT_INPUT_DEG = 89.9                             # RowSpacingCommand.cs:94
DEFAULT_NO_SHADE_START = 9.0                          # RowSpacingCommand.cs:102
DEFAULT_NO_SHADE_END = 15.0                           # RowSpacingCommand.cs:114
DESIGN_DAY_NORTH = 355                                # RowSpacingCommand.cs:132 (Dec 21)
DESIGN_DAY_SOUTH = 172                                # RowSpacingCommand.cs:132 (Jun 21)

FALLBACK_PITCH_M = 6.0                                # TrackerCommand.cs:79
DEFAULT_AZIMUTH_DEG = 0.0                             # TrackerCommand.cs:107, SatCommand.cs:165
PROMPT_ALONG_M = 1.000                                # TrackerCommand.cs:346
PROMPT_CROSS_M = 2.100                                # TrackerCommand.cs:354
PROMPT_GAP_M = 0.020                                  # TrackerCommand.cs:367

GCR_SUGGESTED_DEFAULT = 0.40                          # GcrPrompt.cs:34
GCR_MAX_ATTEMPTS = 64                                 # GcrPrompt.cs:40
GCR_DERIVE_MIN_PITCH_M = 0.001                        # SatCommand.cs:123
GCR_DERIVED_RANGE = (0.01, 0.99)                      # SatCommand.cs:126, exclusive
SAT_MAX_TILT_DEG = 60.0                               # SatCommand.cs:177

TRACKER_BLOCK = "LEAFSAT"                             # SatCommand.cs:306, the block definition name
TRACKER_MODEL = "single_axis_tracker"                 # SatCommand.cs:450
DRAWER_MAX_TILT_DEG = 60.0                            # SatCommand.cs:308
MIN_AXIS_LENGTH_DU = 1e-9                             # SatCommand.cs:364
INT16 = (-32768, 32767)                               # SatCommand.cs:485-490

SETBACK_KINDS = {"Fence": "fence", "Array": "array", "Collection": "collection"}  # LeafSetbackCommand.cs:38-52
SETBACK_DEFAULT_KIND = "Array"                        # LeafSetbackCommand.cs:45
SETBACK_MARK_THRESHOLD = 1e-9                         # LeafSetbackCommand.cs:86
SETBACK_AREA_EPS = 1e-9                               # LeafSetbackCommand.cs:233

# Declared default of every drawing setting these engines read or write: the C#
# field default (0.0 for a double, DrawingPropertiesJson.cs:309-385) or the
# constructor's value (HomerunRouting, DrawingPropertiesJson.cs:533). A setting
# absent from a drawing reads as this value.
SETTING_DEFAULTS = {
    "LeafSpacingMinPitchM": 0.0,
    "ShadeLimitAngleDeg": 0.0,
    "TorqueTubeHeightM": 0.0,
    "TrackerCorridorGapM": 0.0,
    "TrackerModuleAlongAxisM": 0.0,
    "TrackerModuleCrossAxisM": 0.0,
    "TrackerModuleGapM": 0.0,
    "TrackerModulePmaxW": 0.0,
    "TrackerRailOverhangM": 0.0,
    "TrackerSecondaryCorridorGapM": 0.0,
    "TrackerTorqueTubeRadiusM": 0.0,
    "DrawingUnitIsFeet": False,
}
ROUTING_SETTING = "HomerunRouting"

# Studio-side bounds. The plugin has none; these refuse inputs that would pin a worker.
MAX_BOUNDARY_VERTICES = 20_000
MAX_SWEEP_WORK = 50_000_000       # sweep columns x boundary vertices
MAX_TRACKER_ROWS = 20_000
MAX_SETBACK_VERTICES = 2_000      # the simple-ring proof is quadratic in vertices
MAX_ANSWER_ENTRIES = GCR_MAX_ATTEMPTS


class LayoutInputError(ValueError):
    """Malformed input: the engine refuses rather than guess."""


class LayoutBoundsError(LayoutInputError):
    """Input exceeds a Studio bound (vertices, sweep work, rows)."""


def _num(value, what):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise LayoutInputError(f"{what} must be a number, got {type(value).__name__}")
    value = float(value)
    if not math.isfinite(value):
        raise LayoutInputError(f"{what} must be finite, got {value!r}")
    return value


def _setting_num(settings, name):
    return _num(settings.get(name, SETTING_DEFAULTS[name]), name)


def _answers(answers, allowed):
    """A prompt-answer mapping; None or an absent key is Enter (the prompt's default)."""
    if answers is None:
        return {}
    if not isinstance(answers, dict):
        raise LayoutInputError("answers must be a mapping of prompt name to value")
    unknown = set(answers) - set(allowed)
    if unknown:
        raise LayoutInputError(f"unknown prompt answers {sorted(unknown)}")
    return answers


def _prompt_double(answers, key, default, allow_negative=False, allow_zero=True):
    """PromptDoubleOptions with AllowNone: Enter takes the default; a value AutoCAD
    would reject (negative, zero) is refused here instead of re-prompted."""
    value = answers.get(key)
    if value is None:
        return default
    value = _num(value, key)
    if value < 0 and not allow_negative:
        raise LayoutInputError(f"{key} must not be negative")
    if value == 0 and not allow_zero:
        raise LayoutInputError(f"{key} must not be zero")
    return value


def net_round(value, digits):
    """C# Math.Round(double, int), MidpointRounding.ToEven: scale, round the
    double half to even, unscale (the .NET implementation's order)."""
    if abs(value) < 1e16:
        power10 = 10.0 ** digits
        return round(value * power10) / power10
    return value


# ---------------------------------------------------------------------------
#  Drawing settings: declared defaults, the load a command sees, what Save writes
# ---------------------------------------------------------------------------

def routing_default():
    """HomerunRoutingConfig.CreateDefault(), HomerunRoutingConfig.cs:14-80: the field
    defaults, then EnsureDefaults adds the two catalog cables to the empty list."""
    return {
        "DcHomerunLayer": "LEAF-DC-HOMERUN",
        "FullRoutingStringThreshold": 100,
        "RoadCostMultiplier": 3.0,
        "FenceCostMultiplier": 3.0,
        "OpenGroundCostMultiplier": 1.0,
        "TrayCostMultiplier": 0.45,
        "TrenchCostMultiplier": 2.0,
        "RoadCrossingCostMultiplier": 0.3,
        "MinBendRadiusDrawingUnits": 36.0,
        "FenceBufferDrawingUnits": 2.0,
        "CableCatalog": [
            {"CircuitType": "DC", "Gauge": "3 AWG", "MaxLengthFt": 400.0, "Description": "Default DC homerun"},
            {"CircuitType": "DC", "Gauge": "1/0 AWG", "MaxLengthFt": 1000000.0,
             "Description": "Upsized long DC homerun"},
        ],
    }


def declared_default(name):
    if name == ROUTING_SETTING:
        return routing_default()
    if name not in SETTING_DEFAULTS:
        raise LayoutInputError(f"setting {name!r} has no declared default in this module")
    return SETTING_DEFAULTS[name]


def load_settings(stored):
    """The settings object a command works on, DrawingProperties.cs:228 and :245-251.

    Declared defaults overlaid by the stored values. The routing settings follow the
    deserializer the plugin uses: the constructor already holds the default routing
    object with its two catalog cables (DrawingPropertiesJson.cs:533), and the stored
    object is populated INTO it, so a stored catalog is appended to the default two
    rather than replacing them (reused object, reused list). A command that then
    saves writes the longer catalog back; that is the plugin's committed state and is
    reproduced here, not corrected."""
    if stored is None:
        stored = {}
    if not isinstance(stored, dict):
        raise LayoutInputError("stored settings must be a mapping")
    loaded = {name: copy.deepcopy(value) for name, value in SETTING_DEFAULTS.items()}
    routing = routing_default()
    for name, value in stored.items():
        if name == ROUTING_SETTING:
            if not isinstance(value, dict):
                raise LayoutInputError("stored routing settings must be an object")
            for key, item in value.items():
                if key == "CableCatalog":
                    if not isinstance(item, list):
                        raise LayoutInputError("stored cable catalog must be a list")
                    routing["CableCatalog"].extend(copy.deepcopy(item))
                else:
                    routing[key] = copy.deepcopy(item)
        else:
            loaded[name] = copy.deepcopy(value)
    loaded[ROUTING_SETTING] = routing
    return loaded


def save_settings(stored, writes):
    """What a command's DrawingProperties.Save() commits: the loaded settings (see
    load_settings) with the command's writes applied. Returns the new stored dict."""
    saved = load_settings(stored)
    for name, value in writes.items():
        if name != ROUTING_SETTING and name not in SETTING_DEFAULTS:
            raise LayoutInputError(f"setting {name!r} is not one this module writes")
        saved[name] = copy.deepcopy(value)
    return saved


def canonical_setting_text(value):
    """G20: a nested setting object compares as its canonical JSON text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def changed_settings(before, after):
    """G20 absent-is-default: [(name, new value)] for every setting whose value after
    the step differs from its value before, an absent setting reading as its
    declared default. Sorted by name."""
    before = before or {}
    changes = []
    for name in sorted(set(after) | set(before)):
        old = before.get(name, declared_default(name))
        new = after.get(name, declared_default(name))
        if canonical_setting_text(old) != canonical_setting_text(new):
            changes.append((name, copy.deepcopy(new)))
    return changes


# ---------------------------------------------------------------------------
#  LEAFMODULE
# ---------------------------------------------------------------------------

def get_active_module(settings):
    """ModuleCommand.GetActiveModule, ModuleCommand.cs:21-36, as the sweep's module."""
    if not isinstance(settings, dict):
        raise LayoutInputError("settings must be a mapping")
    def pick(name, fallback, strictly_positive):
        v = _setting_num(settings, name)
        return v if (v > 0 if strictly_positive else v >= 0) else fallback
    return _sweep.TrackerModuleSpec(
        along_axis_m=pick("TrackerModuleAlongAxisM", 1.000, True),
        cross_axis_m=pick("TrackerModuleCrossAxisM", 2.100, True),
        gap_m=pick("TrackerModuleGapM", 0.020, False),
        rail_overhang_m=pick("TrackerRailOverhangM", 0.0, False),
        corridor_gap_m=pick("TrackerCorridorGapM", 0.0, False),
        secondary_corridor_gap_m=pick("TrackerSecondaryCorridorGapM", 0.0, False),
        pmax_w=pick("TrackerModulePmaxW", 0.0, False))


MODULE_ANSWERS = ("preset", "manual_cross_m", "manual_along_m", "manual_gap_mm", "rail_overhang_m",
                  "torque_tube_height_m", "corridor_gap_m", "secondary_corridor_gap_m", "pmax_w",
                  "torque_tube_radius_m")


def module_command(settings, answers=None):
    """LEAFMODULE, ModuleCommand.cs:39-249. Returns the nine settings it writes
    (cs:223-231) as {name: value}; every prompt takes its default unless answered."""
    if not isinstance(settings, dict):
        raise LayoutInputError("settings must be a mapping")
    a = _answers(answers, MODULE_ANSWERS)
    keyword = a.get("preset") or MODULE_DEFAULT_KEYWORD                    # cs:81
    if keyword not in MODULE_KEYWORDS:
        raise LayoutInputError(f"preset must be one of {MODULE_KEYWORDS}")
    gap_m = MODULE_PRESET_GAP_M                                           # cs:78
    preset_pmax = 0.0                                                     # cs:79
    if keyword in MODULE_PRESETS:                                         # cs:83-91
        along_m, cross_m, preset_pmax = MODULE_PRESETS[keyword]
    else:                                                                 # cs:255-299
        cross_m = _prompt_double(a, "manual_cross_m", MANUAL_CROSS_M, allow_zero=False)
        along_m = _prompt_double(a, "manual_along_m", MANUAL_ALONG_M, allow_zero=False)
        gap_m = _prompt_double(a, "manual_gap_mm", MANUAL_GAP_MM) / 1000.0
    s = lambda name: _setting_num(settings, name)
    overhang = _prompt_double(a, "rail_overhang_m",
                              s("TrackerRailOverhangM") if s("TrackerRailOverhangM") > 0
                              else DEFAULT_RAIL_OVERHANG_M)                # cs:101-113
    tt_height = _prompt_double(a, "torque_tube_height_m",
                               s("TorqueTubeHeightM") if s("TorqueTubeHeightM") > 0
                               else DEFAULT_TORQUE_TUBE_HEIGHT_M, allow_zero=False)  # cs:118-130
    corridor = _prompt_double(a, "corridor_gap_m",
                              s("TrackerCorridorGapM") if s("TrackerCorridorGapM") > 0 else 0.0)  # cs:135-147
    secondary = _prompt_double(a, "secondary_corridor_gap_m",
                               s("TrackerSecondaryCorridorGapM") if s("TrackerSecondaryCorridorGapM") > 0
                               else 0.0)                                   # cs:154-167
    default_pmax = (preset_pmax if preset_pmax > 0
                    else (s("TrackerModulePmaxW") if s("TrackerModulePmaxW") > 0 else DEFAULT_PMAX_W))  # cs:173-174
    pmax = _prompt_double(a, "pmax_w", default_pmax, allow_zero=False)    # cs:175-186
    radius = _prompt_double(a, "torque_tube_radius_m",
                            s("TrackerTorqueTubeRadiusM") if s("TrackerTorqueTubeRadiusM") > 0
                            else DEFAULT_TUBE_RADIUS_M, allow_zero=False)  # cs:191-203
    return {                                                              # cs:223-231
        "TrackerModuleAlongAxisM": along_m,
        "TrackerModuleCrossAxisM": cross_m,
        "TrackerModuleGapM": gap_m,
        "TrackerRailOverhangM": overhang,
        "TorqueTubeHeightM": tt_height,
        "TrackerCorridorGapM": corridor,
        "TrackerSecondaryCorridorGapM": secondary,
        "TrackerModulePmaxW": pmax,
        "TrackerTorqueTubeRadiusM": radius,
    }


# ---------------------------------------------------------------------------
#  LEAFSPACING
# ---------------------------------------------------------------------------

def solar_declination(day_of_year):
    """BacktrackingCalculator.SolarDeclination, cs:161-172 (Spencer 1971)."""
    b = 2.0 * math.pi * (day_of_year - 1) / 365.0
    return (180.0 / math.pi) * (
        0.006918
        - 0.399912 * math.cos(b)
        + 0.070257 * math.sin(b)
        - 0.006758 * math.cos(2 * b)
        + 0.000907 * math.sin(2 * b)
        - 0.002697 * math.cos(3 * b)
        + 0.001480 * math.sin(3 * b))


def compute_minimum_pitch(latitude_deg, frame_tilt_deg, frame_length_m, design_day_of_year=DESIGN_DAY_NORTH,
                          no_shade_start_hour=DEFAULT_NO_SHADE_START, no_shade_end_hour=DEFAULT_NO_SHADE_END):
    """RowSpacingCalculator.ComputeMinimumPitch, cs:73-157. None when the sun is below
    the horizon at both window boundaries (cs:138-143)."""
    latitude_deg = _num(latitude_deg, "latitude_deg")
    frame_tilt_deg = _num(frame_tilt_deg, "frame_tilt_deg")
    frame_length_m = _num(frame_length_m, "frame_length_m")
    start = _num(no_shade_start_hour, "no_shade_start_hour")
    end = _num(no_shade_end_hour, "no_shade_end_hour")
    if isinstance(design_day_of_year, bool) or not isinstance(design_day_of_year, int):
        raise LayoutInputError("design_day_of_year must be an integer")
    if frame_tilt_deg < 0 or frame_tilt_deg >= 90:                        # cs:81-82
        raise LayoutInputError("frameTiltDeg must be in [0, 90).")
    if frame_length_m <= 0:                                               # cs:83-84
        raise LayoutInputError("frameLengthM must be > 0.")
    if start >= end:                                                      # cs:85-87
        raise LayoutInputError("noShadeStartHour must be less than noShadeEndHour.")
    tilt_rad = frame_tilt_deg * math.pi / 180.0
    lat_rad = latitude_deg * math.pi / 180.0
    dec_deg = solar_declination(design_day_of_year)
    dec_rad = dec_deg * math.pi / 180.0
    footprint_m = frame_length_m * math.cos(tilt_rad)                     # cs:95
    vert_height_m = frame_length_m * math.sin(tilt_rad)                   # cs:96
    worst_pitch = 0.0
    worst_elev_deg = 0.0
    worst_shadow_m = 0.0
    worst_hour_angle_deg = 0.0
    for solar_hour in (start, end):                                       # cs:106-136
        hour_angle_deg = (solar_hour - 12.0) * 15.0
        hour_angle_rad = hour_angle_deg * math.pi / 180.0
        sin_elev = (math.sin(lat_rad) * math.sin(dec_rad)
                    + math.cos(lat_rad) * math.cos(dec_rad) * math.cos(hour_angle_rad))
        if sin_elev <= 0:
            continue
        elev_rad = math.asin(sin_elev)
        shadow_behind_m = vert_height_m / math.tan(elev_rad)
        min_pitch_this = footprint_m + shadow_behind_m
        if min_pitch_this > worst_pitch:
            worst_pitch = min_pitch_this
            worst_elev_deg = elev_rad * 180.0 / math.pi
            worst_shadow_m = shadow_behind_m
            worst_hour_angle_deg = hour_angle_deg
    if worst_pitch <= 0:
        return None
    return {"min_pitch_m": worst_pitch, "gcr": footprint_m / worst_pitch,
            "worst_case_solar_elevation_deg": worst_elev_deg, "shadow_length_behind_m": worst_shadow_m,
            "frame_footprint_m": footprint_m, "worst_case_hour_angle_deg": worst_hour_angle_deg,
            "design_day_of_year": design_day_of_year}


def spacing_pitch_from_gcr(frame_length_m, frame_tilt_deg, target_gcr):
    """RowSpacingCalculator.PitchFromGcr, cs:167-176."""
    if target_gcr <= 0 or target_gcr >= 1.0:
        raise LayoutInputError("targetGcr must be in (0, 1).")
    if frame_length_m <= 0:
        raise LayoutInputError("frameLengthM must be > 0.")
    return frame_length_m * math.cos(frame_tilt_deg * math.pi / 180.0) / target_gcr


def spacing_gcr_from_pitch(frame_length_m, frame_tilt_deg, pitch_m):
    """RowSpacingCalculator.GcrFromPitch, cs:181-190."""
    if pitch_m <= 0:
        raise LayoutInputError("pitchM must be > 0.")
    if frame_length_m <= 0:
        raise LayoutInputError("frameLengthM must be > 0.")
    return frame_length_m * math.cos(frame_tilt_deg * math.pi / 180.0) / pitch_m


SPACING_ANSWERS = ("latitude_deg", "frame_length_m", "tilt_deg", "start_hour", "end_hour")


def spacing_command(settings, site_latitude_deg=None, answers=None):
    """LEAFSPACING, RowSpacingCommand.cs:21-207. Returns {"result", "writes"}: writes
    is {"LeafSpacingMinPitchM": pitch} (cs:156-160), or {} when the command stops
    before its save (start not before end, cs:123-127; sun below the horizon, cs:140-151).

    site_latitude_deg is the site location the drawing resolves (ShadeCommand.TryReadLatLon);
    None when it has none, and the prompt then offers 35.0 (cs:32-34)."""
    if not isinstance(settings, dict):
        raise LayoutInputError("settings must be a mapping")
    a = _answers(answers, SPACING_ANSWERS)
    default_lat = DEFAULT_LATITUDE_DEG if site_latitude_deg is None else _num(site_latitude_deg, "site latitude")
    lat = _prompt_double(a, "latitude_deg", default_lat, allow_negative=True)          # cs:40-49
    lat = max(-LATITUDE_CLAMP_DEG, min(LATITUDE_CLAMP_DEG, lat))                        # cs:50
    default_len = DEFAULT_FRAME_LENGTH_M                                                # cs:56-64
    cross = _setting_num(settings, "TrackerModuleCrossAxisM")
    if FRAME_LENGTH_FROM_MODULE[0] < cross < FRAME_LENGTH_FROM_MODULE[1]:
        default_len = cross
    frame_len = _prompt_double(a, "frame_length_m", default_len, allow_zero=False)      # cs:66-77
    tilt = min(_prompt_double(a, "tilt_deg", DEFAULT_TILT_DEG), MAX_TILT_INPUT_DEG)     # cs:82-94
    start = _prompt_double(a, "start_hour", DEFAULT_NO_SHADE_START)                     # cs:99-109
    end = _prompt_double(a, "end_hour", DEFAULT_NO_SHADE_END)                           # cs:111-121
    if start >= end:                                                                    # cs:123-127
        return {"result": None, "writes": {}}
    design_day = DESIGN_DAY_NORTH if lat >= 0 else DESIGN_DAY_SOUTH                     # cs:132
    result = compute_minimum_pitch(lat, tilt, frame_len, design_day, start, end)        # cs:137-138
    if result is None:                                                                  # cs:140-151
        return {"result": None, "writes": {}}
    return {"result": result, "writes": {"LeafSpacingMinPitchM": result["min_pitch_m"]}}


# ---------------------------------------------------------------------------
#  Terrain slope (TerrainSlopeQuery, TrackerCommand.TryReadTerrain)
# ---------------------------------------------------------------------------

class TerrainSlopeQuery:
    """TerrainSlopeQuery.cs:14-100: the mean E-W slope at a drawing X."""

    __slots__ = ("elevations", "rows", "cols", "x_min", "x_max", "meters_per_unit")

    def __init__(self, elevations, rows, cols, drawing_x_min, drawing_x_max, meters_per_unit=1.0):
        self.elevations = elevations
        self.rows = rows
        self.cols = cols
        self.x_min = drawing_x_min
        self.x_max = drawing_x_max
        self.meters_per_unit = meters_per_unit if meters_per_unit > 0 else 1.0   # cs:54

    def ew_slope_rad(self, drawing_x):
        """GetEWSlopeRad, cs:70-99."""
        if self.cols < 2:
            return 0.0
        x_range = self.x_max - self.x_min
        if x_range <= 0:
            return 0.0
        frac = (drawing_x - self.x_min) / x_range * (self.cols - 1)
        frac = max(0.0, min(self.cols - 1.0 - 1e-9, frac))
        col_left = int(frac)
        col_right = col_left + 1
        col_spacing_m = x_range * self.meters_per_unit / (self.cols - 1)
        if col_spacing_m <= 0:
            return 0.0
        slope_sum = 0.0
        for r in range(self.rows):
            left = self.elevations[r * self.cols + col_left]
            right = self.elevations[r * self.cols + col_right]
            slope_sum += math.atan2(right - left, col_spacing_m)
        return slope_sum / self.rows if self.rows > 0 else 0.0


def terrain_slope_query(grid, meters_per_unit):
    """TrackerCommand.TryReadTerrain, cs:211-268: None where the plugin returns null
    (no grid, or anything malformed: its catch-all degrades to flat ground)."""
    if grid is None:
        return None
    try:
        clean = _ground.neutral_grid(grid)
        for key in ("x_min", "x_max"):
            if not math.isfinite(clean[key]):
                return None
        return TerrainSlopeQuery(clean["elevations"], clean["rows"], clean["cols"], clean["x_min"],
                                 clean["x_max"], _num(meters_per_unit, "meters_per_unit"))
    except (_ground.TerrainInputError, LayoutInputError):
        return None


# ---------------------------------------------------------------------------
#  LEAFTRACK and LEAFSAT
# ---------------------------------------------------------------------------

def compute_gcr(cross_axis_m, pitch_m):
    """TrackerRowGenerator.ComputeGcr, cs:585-590."""
    if pitch_m <= 0:
        raise LayoutInputError("pitchM must be > 0.")
    return cross_axis_m / pitch_m


def pitch_from_gcr(cross_axis_m, target_gcr):
    """TrackerRowGenerator.PitchFromGcr, cs:596-601."""
    if target_gcr <= 0 or target_gcr >= 1.0:
        raise LayoutInputError("targetGcr must be in the open interval (0, 1).")
    return cross_axis_m / target_gcr


def compute_shade_limit_angle(gcr, max_tilt_deg):
    """BacktrackingCalculator.ComputeShadeLimitAngle, cs:192-211."""
    gcr = _num(gcr, "gcr")
    max_tilt_deg = _num(max_tilt_deg, "max_tilt_deg")
    if gcr <= 0 or gcr >= 1.0:
        raise LayoutInputError("gcr must be in the open interval (0, 1).")
    if max_tilt_deg <= 0 or max_tilt_deg >= 90.0:
        raise LayoutInputError("maxTiltDeg must be in the open interval (0°, 90°).")
    tilt_rad = max_tilt_deg * math.pi / 180.0
    denominator = 1.0 - gcr * math.cos(tilt_rad)
    if denominator <= 0:
        return 90.0
    return math.atan(gcr * math.sin(tilt_rad) / denominator) * 180.0 / math.pi


def _boundary(boundary):
    if not isinstance(boundary, (list, tuple)) or not 3 <= len(boundary) <= MAX_BOUNDARY_VERTICES:
        raise LayoutBoundsError(f"boundary must hold 3 to {MAX_BOUNDARY_VERTICES} vertices")
    pts = []
    for i, v in enumerate(boundary):
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise LayoutInputError(f"boundary[{i}] must be [x, y]")
        pts.append((_num(v[0], f"boundary[{i}].x"), _num(v[1], f"boundary[{i}].y")))
    return pts


def _units(answers):
    """TerrainUnitsPrompt with the drawing's stored choice as its default."""
    try:
        return _ground.meters_per_unit_for_keyword(answers.get("units"), bool(answers.get("_default_is_feet")))
    except _ground.TerrainInputError as exc:
        raise LayoutInputError(str(exc)) from None


def _stored_or_prompted_module(settings, answers, mpu):
    """TrackerCommand.PromptOrUseStoredModule, cs:175-204, and PromptModuleDims, cs:339-369."""
    stored = get_active_module(settings)
    if _setting_num(settings, "TrackerModuleCrossAxisM") > 0:
        use = answers.get("use_stored_module")
        if use is None or str(use).lower() == "yes":
            return stored
        if str(use).lower() != "no":
            raise LayoutInputError("use_stored_module must be Yes or No")
    along = _prompt_double(answers, "module_along", net_round(PROMPT_ALONG_M / mpu, 2), allow_zero=False)
    cross = _prompt_double(answers, "module_cross", net_round(PROMPT_CROSS_M / mpu, 2), allow_zero=False)
    return _sweep.TrackerModuleSpec(along_axis_m=along * mpu, cross_axis_m=cross * mpu, gap_m=PROMPT_GAP_M)


def _generate(boundary_pts, pitch_m, module, mpu, azimuth_deg, slope):
    """TrackerRowGenerator.Generate through the shared sweep port, bounded first."""
    cos_in = math.cos(azimuth_deg * math.pi / 180.0)
    sin_in = math.sin(azimuth_deg * math.pi / 180.0)
    xs = [x * cos_in - y * sin_in for x, y in boundary_pts]
    # Each advance is at least a tenth of the nominal pitch (cs:574).
    columns = (max(xs) - min(xs)) / (pitch_m / mpu * 0.1) + 1.0
    if columns * len(boundary_pts) > MAX_SWEEP_WORK:
        raise LayoutBoundsError(f"sweep work {columns:.0f} x {len(boundary_pts)} exceeds {MAX_SWEEP_WORK}")
    try:
        rows = _sweep.generate_tracker_rows(boundary_pts, pitch_m, module, mpu, azimuth_deg,
                                            None if slope is None else slope.ew_slope_rad)
    except (TypeError, ValueError) as exc:
        raise LayoutInputError(str(exc)) from None
    if len(rows) > MAX_TRACKER_ROWS:
        raise LayoutBoundsError(f"{len(rows)} tracker rows exceed {MAX_TRACKER_ROWS}")
    return rows


def _int16(value):
    """SatCommand.cs:485-490, ClampToInt16."""
    return max(INT16[0], min(INT16[1], value))


def draw_rows(rows, module, meters_per_unit, azimuth_deg, pitch_m, source_command):
    """TrackerDrawer.DrawRows, SatCommand.cs:333-390, and BuildRowXData, cs:428-483:
    one block reference per row, in row order, with its row fields (neutral names)."""
    cross_du = module.cross_axis_m / meters_per_unit                      # cs:342
    gcr = module.cross_axis_m / pitch_m if pitch_m > 0.0 else 0.0          # cs:436
    placed = []
    for row in rows:
        ax, ay = row.axis_start
        bx, by = row.axis_end
        dx = bx - ax
        dy = by - ay
        len_du = math.sqrt(dx * dx + dy * dy)                              # cs:363
        if len_du <= MIN_AXIS_LENGTH_DU:                                   # cs:364
            continue
        placed.append({
            "block": TRACKER_BLOCK,
            "insert": ((ax + bx) * 0.5, (ay + by) * 0.5, 0.0),             # cs:366
            "rotation_rad": math.atan2(dy, dx),                            # cs:375
            "scale": (len_du, cross_du, 1.0),                              # cs:376
            "row_index": _int16(row.row_index),                            # cs:440
            "slots": _int16(row.module_slots),                             # cs:441
            "source_command": source_command or "",                        # cs:448
            "tracker_model": TRACKER_MODEL,                                # cs:450
            "axis_start": (ax, ay),                                        # cs:452-455
            "axis_end": (bx, by),                                          # cs:456-459
            "cross_axis_width_du": cross_du,                               # cs:460-461
            "row_length_m": row.length_meters,                             # cs:463-464
            "rail_overhang_m": row.rail_overhang_m,                        # cs:465-466
            "row_pitch_m": pitch_m,                                        # cs:467-468
            "axis_azimuth_deg": azimuth_deg,                               # cs:469-470
            "max_tilt_deg": DRAWER_MAX_TILT_DEG,                           # cs:471-472
            "gcr": gcr,                                                    # cs:473-474
            "module_along_axis_m": module.along_axis_m,                    # cs:476-477
            "module_cross_axis_m": module.cross_axis_m,                    # cs:478-479
            "module_gap_m": module.gap_m,                                  # cs:480-481
        })
    return placed


TRACK_ANSWERS = ("units", "_default_is_feet", "pitch", "use_stored_module", "module_along", "module_cross",
                 "azimuth_deg")


def tracker_command(boundary, settings, grid=None, answers=None):
    """LEAFTRACK, TrackerCommand.cs:18-164. Returns {"placements", "pitch_m", "gcr",
    "module", "slots", "variable_pitch"}; placements is empty when no row fits (the
    command draws nothing, cs:126-130). LEAFTRACK writes no drawing setting."""
    if not isinstance(settings, dict):
        raise LayoutInputError("settings must be a mapping")
    pts = _boundary(boundary)                                                          # cs:38-61
    a = dict(_answers(answers, TRACK_ANSWERS))
    a.setdefault("_default_is_feet", bool(settings.get("DrawingUnitIsFeet", False)))   # cs:66-68
    mpu = _units(a)                                                                     # cs:70-72
    fallback_pitch_m = FALLBACK_PITCH_M                                                 # cs:79-86
    spacing_min = _setting_num(settings, "LeafSpacingMinPitchM")
    if spacing_min > 0.0:
        fallback_pitch_m = spacing_min
    default_pitch_du = net_round(fallback_pitch_m / mpu, 1)                             # cs:88
    pitch_m = _prompt_double(a, "pitch", default_pitch_du, allow_zero=False) * mpu      # cs:90-95
    module = _stored_or_prompted_module(settings, a, mpu)                               # cs:100-101
    azimuth = _prompt_double(a, "azimuth_deg", DEFAULT_AZIMUTH_DEG, allow_negative=True)  # cs:106-110
    gcr = compute_gcr(module.cross_axis_m, pitch_m)                                     # cs:115
    slope = terrain_slope_query(grid, mpu)                                              # cs:119
    rows = _generate(pts, pitch_m, module, mpu, azimuth, slope)                         # cs:123-124
    placements = draw_rows(rows, module, mpu, azimuth, pitch_m, "LEAFTRACK") if rows else []  # cs:135-136
    return {"placements": placements, "pitch_m": pitch_m, "gcr": gcr, "module": module,
            "slots": sum(r.module_slots for r in rows), "variable_pitch": slope is not None}


def resolve_gcr(entries, default):
    """GcrPrompt.TryResolve, GcrPrompt.cs:96-124: the first in-range entry wins, Enter
    (None) takes the default, an out-of-range entry is re-prompted and never replaced
    by a default. None when the entries run out or after 64 rejections."""
    if entries is None:
        entries = [None]
    if not isinstance(entries, (list, tuple)) or len(entries) > MAX_ANSWER_ENTRIES:
        raise LayoutInputError(f"gcr entries must be a list of at most {MAX_ANSWER_ENTRIES}")
    for attempt, entry in enumerate(entries):
        if attempt >= GCR_MAX_ATTEMPTS:
            return None
        value = default if entry is None else _num(entry, "gcr entry")
        if 0.0 < value < 1.0:
            return value
    return None


SAT_ANSWERS = ("units", "_default_is_feet", "use_stored_module", "module_along", "module_cross", "gcr_entries",
               "latitude_deg", "azimuth_deg")


def sat_command(boundary, settings, grid=None, answers=None):
    """LEAFSAT, SatCommand.cs:21-274. Returns {"placements", "pitch_m", "gcr",
    "shade_limit_angle_deg", "slots", "variable_pitch", "writes"}: writes is
    {"ShadeLimitAngleDeg": sla} once rows are drawn (cs:243-246), {} otherwise."""
    if not isinstance(settings, dict):
        raise LayoutInputError("settings must be a mapping")
    pts = _boundary(boundary)                                                          # cs:41-65
    a = dict(_answers(answers, SAT_ANSWERS))
    a.setdefault("_default_is_feet", bool(settings.get("DrawingUnitIsFeet", False)))   # cs:93-95
    mpu = _units(a)                                                                     # cs:97-99
    module = _stored_or_prompted_module(settings, a, mpu)                               # cs:104-105
    spacing_min = _setting_num(settings, "LeafSpacingMinPitchM")                        # cs:112-118
    suggested = GCR_SUGGESTED_DEFAULT                                                   # cs:121
    if spacing_min > GCR_DERIVE_MIN_PITCH_M and module.cross_axis_m > 0:               # cs:123-131
        derived = module.cross_axis_m / spacing_min
        if GCR_DERIVED_RANGE[0] < derived < GCR_DERIVED_RANGE[1]:
            suggested = derived
    gcr = resolve_gcr(a.get("gcr_entries"), suggested)                                  # cs:133-144
    if gcr is None:
        return {"placements": [], "pitch_m": None, "gcr": None, "shade_limit_angle_deg": None,
                "slots": 0, "variable_pitch": False, "writes": {}}
    _prompt_double(a, "latitude_deg", DEFAULT_LATITUDE_DEG, allow_negative=True)        # cs:149-159, report only
    azimuth = _prompt_double(a, "azimuth_deg", DEFAULT_AZIMUTH_DEG, allow_negative=True)  # cs:164-168
    pitch_m = pitch_from_gcr(module.cross_axis_m, gcr)                                  # cs:173
    sla = compute_shade_limit_angle(gcr, SAT_MAX_TILT_DEG)                              # cs:177-178
    slope = terrain_slope_query(grid, mpu)                                              # cs:204
    rows = _generate(pts, pitch_m, module, mpu, azimuth, slope)                         # cs:228-229
    if not rows:                                                                        # cs:231-235
        return {"placements": [], "pitch_m": pitch_m, "gcr": gcr, "shade_limit_angle_deg": sla,
                "slots": 0, "variable_pitch": slope is not None, "writes": {}}
    placements = draw_rows(rows, module, mpu, azimuth, pitch_m, "LEAFSAT")             # cs:240-241
    return {"placements": placements, "pitch_m": pitch_m, "gcr": gcr, "shade_limit_angle_deg": sla,
            "slots": sum(r.module_slots for r in rows), "variable_pitch": slope is not None,
            "writes": {"ShadeLimitAngleDeg": sla}}                                      # cs:244-246


# ---------------------------------------------------------------------------
#  LEAFSETBACK
# ---------------------------------------------------------------------------

def signed_area(pts):
    """Shoelace, positive counter-clockwise."""
    n = len(pts)
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n))


def _dedupe(pts):
    out = []
    for p in pts:
        if not out or (abs(p[0] - out[-1][0]) > 1e-12 or abs(p[1] - out[-1][1]) > 1e-12):
            out.append(p)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) <= 1e-12 and abs(out[0][1] - out[-1][1]) <= 1e-12:
        out.pop()
    return out


def _segments_cross(p1, p2, q1, q2):
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) <= 1e-12 else (1 if v > 0 else -1)
    def on(a, b, c):
        return (min(a[0], b[0]) - 1e-12 <= c[0] <= max(a[0], b[0]) + 1e-12
                and min(a[1], b[1]) - 1e-12 <= c[1] <= max(a[1], b[1]) + 1e-12)
    o1, o2, o3, o4 = orient(p1, p2, q1), orient(p1, p2, q2), orient(q1, q2, p1), orient(q1, q2, p2)
    if o1 != o2 and o3 != o4:
        return True
    return ((o1 == 0 and on(p1, p2, q1)) or (o2 == 0 and on(p1, p2, q2))
            or (o3 == 0 and on(q1, q2, p1)) or (o4 == 0 and on(q1, q2, p2)))


def _is_simple(pts):
    n = len(pts)
    for i in range(n):
        a1, a2 = pts[i], pts[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            if _segments_cross(a1, a2, pts[j], pts[(j + 1) % n]):
                return False
    return True


def offset_ring(pts, signed_distance):
    """One closed polyline offset of straight segments with the gaps between shifted
    segments extended to their intersections (the offset AutoCAD's
    Polyline.GetOffsetCurves returns for a straight-edged closed polyline at the
    default gap type). Positive offsets to the LEFT of each edge. Vertex i of the
    result is the shifted vertex i of the source, so the source's order and start
    are kept. None when the shifted ring degenerates (flipped orientation, zero
    area or self-intersection): the Studio port returns no loop there rather than
    guess at AutoCAD's trimming of a ring that splits."""
    n = len(pts)
    lines = []
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        length = math.hypot(bx - ax, by - ay)
        ux, uy = (bx - ax) / length, (by - ay) / length
        lines.append((ax - signed_distance * uy, ay + signed_distance * ux, ux, uy))
    ring = []
    for i in range(n):
        px, py, pux, puy = lines[i - 1]
        qx, qy, qux, quy = lines[i]
        cross = pux * quy - puy * qux
        if abs(cross) <= 1e-12:                 # collinear edges: the vertex moves along the normal
            ring.append((qx, qy))
            continue
        t = ((qx - px) * quy - (qy - py) * qux) / cross
        ring.append((px + t * pux, py + t * puy))
    # A shifted edge that ran out (its two ends crossed over) reverses direction: the
    # offset has swallowed it, and the ring is not a plain offset any more.
    for i in range(n):
        (sx, sy), (ex, ey) = ring[i], ring[(i + 1) % n]
        if (ex - sx) * lines[i][2] + (ey - sy) * lines[i][3] <= 0:
            return None
    area = signed_area(ring)
    source_area = signed_area(pts)
    if area == 0 or (area > 0) != (source_area > 0) or not _is_simple(ring):
        return None
    return ring


def offset_inward(boundary, distance):
    """LeafSetbackCommand.OffsetInward, cs:205-250: offset both ways and keep the
    loops whose total area is below the source's, the smaller total when both are.
    Returns a list of rings (each a list of (x, y)), empty when neither side works."""
    pts = _dedupe(_boundary(boundary))
    if len(pts) < 3:
        return []
    if len(pts) > MAX_SETBACK_VERTICES:
        raise LayoutBoundsError(f"setback boundary holds more than {MAX_SETBACK_VERTICES} vertices")
    distance = _num(distance, "distance")
    source_area = abs(signed_area(pts))
    best, best_area = [], float("inf")
    for signed in (-distance, distance):                                   # cs:211
        ring = offset_ring(pts, signed)
        loops = [] if ring is None else [ring]
        total = sum(abs(signed_area(loop)) for loop in loops)
        if loops and total < source_area - SETBACK_AREA_EPS and total < best_area:   # cs:232-234
            best, best_area = loops, total
    return best


SETBACK_ANSWERS = ("kind", "distance")


def setback_command(boundary, answers=None):
    """LEAFSETBACK, LeafSetbackCommand.cs:24-133. Returns {"kind", "distance",
    "marked", "rings"}: a zero distance marks the boundary itself as the setback
    (cs:86-90, rings = [boundary], marked True); a positive one adds the inward
    offset loops (cs:93-111); rings is empty when no offset exists (cs:94-101,
    nothing is committed)."""
    a = _answers(answers, SETBACK_ANSWERS)
    keyword = a.get("kind") or SETBACK_DEFAULT_KIND
    match = next((k for k in SETBACK_KINDS if k.lower() == str(keyword).lower()), None)
    if match is None:
        raise LayoutInputError(f"kind must be one of {sorted(SETBACK_KINDS)}")
    pts = _boundary(boundary)
    distance = _prompt_double(a, "distance", 0.0)                          # cs:62-72
    if distance <= SETBACK_MARK_THRESHOLD:
        return {"kind": SETBACK_KINDS[match], "distance": distance, "marked": True, "rings": [pts]}
    return {"kind": SETBACK_KINDS[match], "distance": distance, "marked": False,
            "rings": offset_inward(pts, distance)}
