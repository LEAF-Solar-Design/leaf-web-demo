"""Pure standard-library port of Branch2025's ground frame engines.

Commands covered: LEAFGENERATE and LEAFGENERATEMULTI (frame packing and the frame
polylines they draw), LEAFCOLLISION (frame overlap markers), LEAFPILING (native
LEAF-TRACKERS frames only) and LEAFCOLLISIONRANGE (pile length window markers).
Plugin source is read-only at C:/tmp/solar-parity/wt-b25-s17; every function names
the file and lines it ports.

Contract:
- Pure functions over plain data. No AutoCAD, no network, no file or clock access.
  Preset and pile template stores are passed in as JSON text.
- Deterministic order, identical to the plugin: boundaries in the order given, frames
  row-major from the boundary bbox minimum (rows by Y, columns by X), collisions by
  (i, j) with i < j, piles per source in frame order then grid order (vertical outer,
  horizontal inner) followed by joint piles, range hits in entity order.
- Units: frame vertices and pile X/Y/Z are drawing units. Terrain samplers take
  drawing-unit X/Y and return metres or None, exactly like the plugin's
  TerrainGridInterpolator.InterpolateZ; this module divides by meters_per_unit.
- Every input is bounded (vertex, entity, frame, pile and collision caps below) and
  malformed input fails closed with GroundFramesError carrying a stable `code`.
  Where the plugin would spin forever (a zero or negative frame footprint) this port
  refuses instead; that is the one intended behavioural difference.
- Store loading replays Newtonsoft's default ObjectCreationHandling.Auto: a list that
  a constructor or lazy getter already populated is appended to, not replaced. The
  plugin's own pile_templates.json shows the effect (RevealBucketBoundariesM grows by
  five entries per save). For frame presets it means a persisted TrackerPack comes back
  with its segments duplicated, which changes the frame footprint. Pass
  replicate_list_reuse=False to read the files the way their author intended.

Not ported (outside the Studio surface or reporting only): PVcase BlockReference pile
sources and their block mappings, PilePlaneSeating (it only seats BlockReference
sources, so native frames never move), TrackerSlopeValidator status text, telemetry,
progress UI and store mutation (Save, Delete, Import, Export).
"""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Context, Decimal


# --- names and codes (LayerNames.cs:36,53,104; LeafGenerateCommand.cs:32-34;
# LeafCollisionCommand.cs:29-30; LeafPilingCommand.cs:36-38) ---------------------
LAYER_TRACKERS = "LEAF-TRACKERS"
LAYER_PILING = "LEAF-PILING"
LAYER_COLLISION = "LEAF-COLLISION"
LAYER_SHADING_RESTRICTION = "LEAF-PVCASE-SHADING-RESTRICTION"
XDATA_FRAMECELL = "LEAFFRAMECELL"
XDATA_PILING = "LEAFPILING"
DXF_REGAPP = 1001
DXF_ASCII = 1000
DXF_INT16 = 1070
SHORT_MAX = 32767
TRACKERS_LAYER_ACI = 3
PILING_LAYER_ACI = 1
COLLISION_LAYER_ACI = 1
BY_LAYER = {"method": "ByLayer", "index": 256}
NATIVE_MAPPING_SIGNATURE = "NATIVE|LEAF-TRACKERS|ACTIVE-PRESET"

FRAMING_TYPES = ("FixedTilt", "SingleAxisTracker", "EastWest")
ORIENTATIONS = ("Portrait", "Landscape")
PACK_SEGMENT_KINDS = ("Modules", "JointGap", "Motor")
PLACEMENT_MODES = ("Grid", "AxisStations")
STATION_AXES = ("LocalX", "LocalY")
STATION_KINDS = ("Bearing", "Drive", "Joint", "End")

# --- bounds (fail closed above these) ------------------------------------------
MAX_JSON_CHARS = 4_000_000
MAX_JSON_LIST_ITEMS = 100_000
MAX_ENTITIES = 500_000
MAX_BOUNDARIES = 1_000
MAX_POLYGON_VERTICES = 20_000
MAX_EXCLUSIONS = 10_000
MAX_CANDIDATE_CELLS = 4_000_000
MAX_FRAMES = 200_000
MAX_POLES_PER_AXIS = 1_000
MAX_STATIONS = 10_000
MAX_JOINT_MARKERS = 10_000
MAX_PILES = 2_000_000
MAX_COLLISIONS = 1_000_000
INT32_MAX = 2_147_483_647
INT32_MIN = -2_147_483_648

_INT_TEXT = re.compile(r"^\s*[+-]?\d+\s*$")
_FLOAT_TEXT = re.compile(r"^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*$")
_WIDE = Context(prec=400)


class GroundFramesError(ValueError):
    """Named refusal. `code` is stable for callers and tests."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


# ================================================================ data model ==

@dataclass
class PackSegment:
    """PackSegment, TrackerPack.cs:21-36. `kind` is a PACK_SEGMENT_KINDS name (or an
    undefined enum integer, which every switch in the plugin skips)."""
    kind: object = "Modules"
    count: int = 0


@dataclass
class PackAxisMarker:
    """PackAxisMarker, TrackerPack.cs:38-43."""
    kind: str
    offset_m: float
    gap_width_m: float


@dataclass
class TrackerPack:
    """TrackerPack, TrackerPack.cs:45-272."""
    segments: list | None = field(default_factory=list)
    joint_gap_width_m: float = 0.05
    motor_gap_width_m: float = 0.30
    is_motor_gap_enabled: bool = True
    place_piles_at_joints: bool = True

    def _safe_segments(self):
        return [s for s in (self.segments or []) if s is not None]

    def has_intra_tracker_segments(self):
        """HasIntraTrackerSegments, TrackerPack.cs:61-69."""
        segs = [s for s in self._safe_segments() if s.count > 0]
        return len(segs) > 1 or any(s.kind != "Modules" for s in segs)

    def tracker_length_m(self, module_width_m):
        """TrackerLengthM, TrackerPack.cs:71-95 (accumulation order kept)."""
        total = 0.0
        for seg in self._safe_segments():
            count = max(0, seg.count)
            if count == 0:
                continue
            if seg.kind == "Modules":
                total += count * max(0.0, module_width_m)
            elif seg.kind == "JointGap":
                total += count * max(0.0, self.joint_gap_width_m)
            elif seg.kind == "Motor" and self.is_motor_gap_enabled:
                total += count * max(0.0, self.motor_gap_width_m)
        return total

    def joint_markers_along_axis_m(self, module_width_m):
        """JointMarkersAlongAxisM, TrackerPack.cs:103-151."""
        markers = []
        cursor = 0.0
        for seg in self._safe_segments():
            count = max(0, seg.count)
            if count == 0:
                continue
            if seg.kind == "Modules":
                cursor += count * max(0.0, module_width_m)
            elif seg.kind == "JointGap":
                width = max(0.0, self.joint_gap_width_m)
                for i in range(count):
                    _bounded_append(markers, PackAxisMarker("JointGap", cursor + width * (i + 0.5), width),
                                    MAX_JOINT_MARKERS, "joint_markers")
                cursor += count * width
            elif seg.kind == "Motor":
                width = max(0.0, self.motor_gap_width_m) if self.is_motor_gap_enabled else 0.0
                for i in range(count):
                    offset = cursor + (width * (i + 0.5) if width > 0.0 else 0.0)
                    _bounded_append(markers, PackAxisMarker("Motor", offset, width),
                                    MAX_JOINT_MARKERS, "joint_markers")
                cursor += count * width
        return markers

    @classmethod
    def default_uniform(cls, total_modules):
        """DefaultUniform, TrackerPack.cs:219-228."""
        return cls(segments=[PackSegment("Modules", max(0, total_modules))])

    @classmethod
    def parse_pvcase_tracker_packs(cls, raw):
        """ParsePvcaseTrackerPacks, TrackerPack.cs:187-217."""
        pack = cls()
        if raw is None or not str(raw).strip():
            return pack
        if len(raw) > MAX_JSON_CHARS:
            raise GroundFramesError("tracker_pack_too_long", "tracker pack text exceeds the size cap")
        cleaned = raw.strip().strip('"')
        for raw_part in cleaned.split(";"):
            if raw_part == "":
                continue
            part = raw_part.strip().strip('"')
            if not part:
                continue
            tokens = part.split(",")
            kind = _parse_pack_kind(tokens[0])
            if kind is None:
                continue
            count = _parse_pack_count(tokens[1], 1) if len(tokens) >= 2 else 1
            if kind == "Motor":
                count = max(1, count)
            if count <= 0 and kind != "Motor":
                continue
            _bounded_append(pack.segments, PackSegment(kind, count), MAX_JSON_LIST_ITEMS, "tracker_pack_segments")
        return pack


def _parse_pack_kind(token):
    """TryParseKind, TrackerPack.cs:235-261."""
    normalized = (token or "").strip().replace(" ", "").replace("_", "").replace("-", "").lower()
    if normalized in ("modules", "module"):
        return "Modules"
    if normalized in ("jointgap", "jointgaps"):
        return "JointGap"
    if normalized in ("motor", "motorgap"):
        return "Motor"
    return None


def _parse_pack_count(text, fallback):
    """ParseCount, TrackerPack.cs:263-271 (int, else banker's-rounded double)."""
    raw = (text or "").strip().strip('"')
    if _INT_TEXT.match(raw):
        value = int(raw)
        if INT32_MIN <= value <= INT32_MAX:
            return value
    if _FLOAT_TEXT.match(raw):
        value = round(float(raw))
        if INT32_MIN <= value <= INT32_MAX:
            return value
        raise GroundFramesError("tracker_pack_count_out_of_range", f"segment count {raw!r} overflows Int32")
    return fallback


@dataclass
class PilingConfig:
    """PilingConfig, FramePreset.cs:115-137."""
    horizontal_poles_per_frame: int = 4
    vertical_poles_per_group: int = 2
    pile_diameter_m: float = 0.15
    pile_reveal_m: float = 0.5
    pile_embedment_m: float = 1.5
    min_pile_length_m: float = 1.0
    max_pile_length_m: float = 6.0

    @property
    def pile_depth_m(self):
        return self.pile_reveal_m + self.pile_embedment_m

    def set_pile_depth_m(self, value):
        """PileDepthM setter, FramePreset.cs:125: embedment absorbs the change."""
        self.pile_embedment_m = max(0.0, value - self.pile_reveal_m)

    @property
    def piles_per_frame(self):
        return self.horizontal_poles_per_frame * self.vertical_poles_per_group


@dataclass
class FramePreset:
    """FramePreset, FramePreset.cs:28-113. `tracker_pack` None means the plugin's lazy
    DefaultUniform(TargetModuleCount), materialised on first read."""
    name: str | None = ""
    module_length_m: float = 0.0
    module_width_m: float = 0.0
    module_thickness_m: float = 0.0
    module_power_wp: int = 0
    inverter_type_name: str | None = ""
    color_index: int = 7
    framing_type: object = "FixedTilt"
    orientation: object = "Portrait"
    rows: int = 0
    columns: int = 0
    tilt_degrees: float = 0.0
    horizontal_gap_m: float = 0.0
    vertical_gap_m: float = 0.0
    axis_azimuth_deg: float = 180.0
    rom_min_deg: float = -60.0
    rom_max_deg: float = 60.0
    height_at_low_pose_m: float = 0.8
    height_at_high_pose_m: float = 1.62
    max_ns_slope_pct: float = 8.5
    max_row_to_row_ew_slope_pct: float = 10.0
    max_axial_slope_pct: float = 8.5
    max_cross_axis_slope_pct: float = 10.0
    max_row_to_row_slope_deg: float = 4.0
    max_slope_percent: float = 15.0
    tracker_pack: TrackerPack | None = None
    pile_template_name: str | None = "Full"
    piling: PilingConfig | None = field(default_factory=PilingConfig)

    @property
    def target_module_count(self):
        """TargetModuleCount, FramePreset.cs:102-112."""
        return min(max(0, self.rows) * max(0, self.columns), INT32_MAX)

    @property
    def frame_power_kwp(self):
        """FramePowerKWp, FramePreset.cs:99-100."""
        return self.rows * self.columns * self.module_power_wp / 1000.0

    def get_tracker_pack(self):
        """TrackerPack getter, FramePreset.cs:76-89 (lazy, cached)."""
        if self.tracker_pack is None:
            self.tracker_pack = TrackerPack.default_uniform(self.target_module_count)
        return self.tracker_pack


def default_frame_preset():
    """BuildDefault, FramePresetStore.cs:240-254."""
    return FramePreset(name="Default", module_length_m=2.384, module_width_m=1.303,
                       module_thickness_m=0.033, module_power_wp=715, framing_type="FixedTilt",
                       orientation="Portrait", rows=4, columns=12, tilt_degrees=20.0,
                       horizontal_gap_m=0.02, vertical_gap_m=0.02)


def default_reveal_bucket_boundaries_m():
    """DefaultRevealBucketBoundariesM, PileTemplate.cs:107-111."""
    ft = 0.3048
    return [3.0 * ft, 4.0 * ft, 5.0 * ft, 6.0 * ft, 7.0 * ft]


@dataclass
class PileStation:
    """PileStation, PileTemplate.cs:34-54."""
    offset_m: float = 0.0
    kind: object = "Bearing"
    label: str | None = ""
    cross_axis_offset_m: float = 0.0


@dataclass
class PileTemplate:
    """PileTemplate, PileTemplate.cs:60-302."""
    name: str | None = "Full"
    are_equal_margins: bool = False
    should_place_piles_at_joints: bool = False
    is_mirror_from_middle: bool = False
    distribution_type: int = 2
    horizontal_distances_m: list | None = field(default_factory=list)
    vertical_distances_m: list | None = field(default_factory=list)
    middle_distribution: float = 0.0
    selected_middle_pole: bool = False
    horizontal_pole_count: int = 2
    vertical_pole_count: int = 1
    placement_mode: object = "Grid"
    station_axis: object = "LocalX"
    reverse_station_start: bool = False
    stations: list | None = field(default_factory=list)
    pile_diameter_m: float = 0.0
    pile_reveal_m: float = 0.0
    pile_embedment_m: float = 0.0
    min_pile_length_m: float = 0.0
    max_pile_length_m: float = 0.0
    reveal_bucket_boundaries_m: list | None = field(default_factory=default_reveal_bucket_boundaries_m)

    @property
    def piles_per_frame(self):
        """PilesPerFrame, PileTemplate.cs:95-105."""
        if self.placement_mode == "AxisStations" and self.stations:
            return len(self.stations)
        return self.horizontal_pole_count * self.vertical_pole_count

    def clone(self):
        """Clone, PileTemplate.cs:166-196."""
        return deepcopy(self)

    @classmethod
    def from_legacy_config(cls, cfg, name="Default"):
        """FromLegacyConfig, PileTemplate.cs:143-164: empty distance lists keep the
        half-cell grid of PilePlacer.BuildAxisPositions."""
        if cfg is None:
            raise GroundFramesError("piling_config_missing", "PilingConfig is required")
        return cls(name=name if name and name.strip() else "Default",
                   horizontal_pole_count=max(1, cfg.horizontal_poles_per_frame),
                   vertical_pole_count=max(1, cfg.vertical_poles_per_group),
                   horizontal_distances_m=[], vertical_distances_m=[], placement_mode="Grid")


def default_full_pile_template():
    """BuildDefaultFull, PileTemplateStore.cs:215-227."""
    return PileTemplate(name="Full", horizontal_pole_count=2, vertical_pole_count=1,
                        horizontal_distances_m=[1.0, 1.0, 1.0], vertical_distances_m=[1.0, 1.0],
                        placement_mode="Grid")


# ======================================================= Newtonsoft replay ==

class _JsonObject(list):
    """Ordered (key, value) pairs so key order and duplicates survive parsing."""


def _parse_json(text, what):
    if not isinstance(text, str):
        raise GroundFramesError(f"{what}_not_text", f"{what} must be JSON text")
    if len(text) > MAX_JSON_CHARS:
        raise GroundFramesError(f"{what}_too_large", f"{what} exceeds {MAX_JSON_CHARS} characters")
    return json.loads(text, object_pairs_hook=_JsonObject)


class _Corrupt(Exception):
    """A JsonException in the plugin: the whole DTO is discarded."""


def _lookup(members, key):
    """Newtonsoft property match: exact name first, then OrdinalIgnoreCase."""
    return members.get(key) or members.get(key.lower())


def _members(fields):
    table = {}
    for name, spec in fields.items():
        table[name] = (name, spec)
        table[name.lower()] = (name, spec)
    return table


def _to_double(v):
    if isinstance(v, bool) or v is None or isinstance(v, (list, _JsonObject)):
        raise _Corrupt("double")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and _FLOAT_TEXT.match(v):
        return float(v)
    raise _Corrupt("double")


def _to_int(v):
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise _Corrupt("int")
    if isinstance(v, str):
        if not _INT_TEXT.match(v):
            raise _Corrupt("int")
        v = int(v)
    if not INT32_MIN <= v <= INT32_MAX:
        raise _Corrupt("int")
    return v


def _to_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v != 0
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    raise _Corrupt("bool")


def _to_string(v):
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "True" if v else "False"  # bool.ToString() via JsonReader.ReadAsString
    if isinstance(v, (int, float)):
        return str(v)
    raise _Corrupt("string")


def _to_enum(names):
    def convert(v):
        if isinstance(v, bool) or v is None:
            raise _Corrupt("enum")
        if isinstance(v, int):
            return names[v] if 0 <= v < len(names) else v
        if isinstance(v, str):
            for n in names:
                if n.lower() == v.strip().lower():
                    return n
        raise _Corrupt("enum")
    return convert


def _to_double_list(existing, v, reuse):
    """List<double> property: Auto handling appends to a live list."""
    if v is None:
        return None
    if not isinstance(v, list) or isinstance(v, _JsonObject):
        raise _Corrupt("list")
    base = list(existing) if (reuse and existing is not None) else []
    for item in v:
        base.append(_to_double(item))
    if len(base) > MAX_JSON_LIST_ITEMS:
        raise GroundFramesError("json_list_too_long", "a JSON list exceeds the item cap")
    return base


def _populate(target, obj, table, specials=None):
    """Assign each JSON member in document order, as the serializer does."""
    for key, value in obj:
        hit = _lookup(table, key)
        if hit is None:
            continue
        name, spec = hit
        if spec is None:
            continue  # [JsonIgnore] or get-only
        if callable(spec) and not isinstance(spec, tuple):
            spec(target, value)
        else:
            attr, convert = spec
            setattr(target, attr, convert(value))


def _frame_preset_table(reuse):
    def tracker_pack(p, value):
        if value is None:
            p.tracker_pack = TrackerPack.default_uniform(p.target_module_count)  # setter, FramePreset.cs:84-88
            return
        if not isinstance(value, _JsonObject):
            raise _Corrupt("TrackerPack")
        pack = p.get_tracker_pack() if reuse else TrackerPack()
        _populate(pack, value, _tracker_pack_table(reuse))
        p.tracker_pack = pack

    def piling(p, value):
        if value is None:
            p.piling = None
            return
        if not isinstance(value, _JsonObject):
            raise _Corrupt("Piling")
        cfg = p.piling if (reuse and p.piling is not None) else PilingConfig()
        _populate(cfg, value, _PILING_TABLE)
        p.piling = cfg

    return _members({
        "Name": ("name", _to_string),
        "ModuleLengthM": ("module_length_m", _to_double),
        "ModuleWidthM": ("module_width_m", _to_double),
        "ModuleThicknessM": ("module_thickness_m", _to_double),
        "ModulePowerWp": ("module_power_wp", _to_int),
        "InverterTypeName": ("inverter_type_name", _to_string),
        "ColorIndex": ("color_index", _to_int),
        "FramingType": ("framing_type", _to_enum(FRAMING_TYPES)),
        "Orientation": ("orientation", _to_enum(ORIENTATIONS)),
        "Rows": ("rows", _to_int),
        "Columns": ("columns", _to_int),
        "TiltDegrees": ("tilt_degrees", _to_double),
        "HorizontalGapM": ("horizontal_gap_m", _to_double),
        "VerticalGapM": ("vertical_gap_m", _to_double),
        "AxisAzimuthDeg": ("axis_azimuth_deg", _to_double),
        "RomMinDeg": ("rom_min_deg", _to_double),
        "RomMaxDeg": ("rom_max_deg", _to_double),
        "HeightAtLowPoseM": ("height_at_low_pose_m", _to_double),
        "HeightAtHighPoseM": ("height_at_high_pose_m", _to_double),
        "MaxNsSlopePct": ("max_ns_slope_pct", _to_double),
        "MaxRowToRowEwSlopePct": ("max_row_to_row_ew_slope_pct", _to_double),
        "MaxAxialSlopePct": ("max_axial_slope_pct", _to_double),
        "MaxCrossAxisSlopePct": ("max_cross_axis_slope_pct", _to_double),
        "MaxRowToRowSlopeDeg": ("max_row_to_row_slope_deg", _to_double),
        "MaxSlopePercent": ("max_slope_percent", _to_double),
        "TrackerPack": tracker_pack,
        "PileTemplateName": ("pile_template_name", _to_string),
        "Piling": piling,
        "FramePowerKWp": None,
        "TargetModuleCount": None,
    })


def _tracker_pack_table(reuse):
    def segments(pack, value):
        if value is None:
            pack.segments = None
            return
        if not isinstance(value, list) or isinstance(value, _JsonObject):
            raise _Corrupt("Segments")
        base = list(pack.segments) if (reuse and pack.segments is not None) else []
        for item in value:
            if item is None:
                base.append(None)
                continue
            if not isinstance(item, _JsonObject):
                raise _Corrupt("PackSegment")
            seg = PackSegment("Modules", 0)
            _populate(seg, item, _PACK_SEGMENT_TABLE)
            base.append(seg)
        if len(base) > MAX_JSON_LIST_ITEMS:
            raise GroundFramesError("json_list_too_long", "TrackerPack.Segments exceeds the item cap")
        pack.segments = base

    return _members({
        "Segments": segments,
        "JointGapWidthM": ("joint_gap_width_m", _to_double),
        "MotorGapWidthM": ("motor_gap_width_m", _to_double),
        "IsMotorGapEnabled": ("is_motor_gap_enabled", _to_bool),
        "PlacePilesAtJoints": ("place_piles_at_joints", _to_bool),
        "ModuleCountTotal": None,
        "HasIntraTrackerSegments": None,
    })


_PACK_SEGMENT_TABLE = _members({
    "Kind": ("kind", _to_enum(PACK_SEGMENT_KINDS)),
    "Count": ("count", _to_int),
})

_PILING_TABLE = _members({
    "HorizontalPolesPerFrame": ("horizontal_poles_per_frame", _to_int),
    "VerticalPolesPerGroup": ("vertical_poles_per_group", _to_int),
    "PileDiameterM": ("pile_diameter_m", _to_double),
    "PileRevealM": ("pile_reveal_m", _to_double),
    "PileEmbedmentM": ("pile_embedment_m", _to_double),
    "PileDepthM": lambda cfg, v: cfg.set_pile_depth_m(_to_double(v)),
    "MinPileLengthM": ("min_pile_length_m", _to_double),
    "MaxPileLengthM": ("max_pile_length_m", _to_double),
    "PilesPerFrame": None,
})

_STATION_TABLE = _members({
    "OffsetM": ("offset_m", _to_double),
    "Kind": ("kind", _to_enum(STATION_KINDS)),
    "Label": ("label", _to_string),
    "CrossAxisOffsetM": ("cross_axis_offset_m", _to_double),
})


def _pile_template_table(reuse):
    def dlist(attr):
        def assign(t, value):
            setattr(t, attr, _to_double_list(getattr(t, attr), value, reuse))
        return assign

    def stations(t, value):
        if value is None:
            t.stations = None
            return
        if not isinstance(value, list) or isinstance(value, _JsonObject):
            raise _Corrupt("Stations")
        base = list(t.stations) if (reuse and t.stations is not None) else []
        for item in value:
            if item is None:
                base.append(None)
                continue
            if not isinstance(item, _JsonObject):
                raise _Corrupt("PileStation")
            st = PileStation()
            _populate(st, item, _STATION_TABLE)
            base.append(st)
        if len(base) > MAX_STATIONS:
            raise GroundFramesError("stations_too_many", f"more than {MAX_STATIONS} stations")
        t.stations = base

    return _members({
        "Name": ("name", _to_string),
        "AreEqualMargins": ("are_equal_margins", _to_bool),
        "ShouldPlacePilesAtJoints": ("should_place_piles_at_joints", _to_bool),
        "IsMirrorFromMiddle": ("is_mirror_from_middle", _to_bool),
        "DistributionType": ("distribution_type", _to_int),
        "HorizontalDistancesM": dlist("horizontal_distances_m"),
        "VerticalDistancesM": dlist("vertical_distances_m"),
        "MiddleDistribution": ("middle_distribution", _to_double),
        "SelectedMiddlePole": ("selected_middle_pole", _to_bool),
        "HorizontalPoleCount": ("horizontal_pole_count", _to_int),
        "VerticalPoleCount": ("vertical_pole_count", _to_int),
        "PlacementMode": ("placement_mode", _to_enum(PLACEMENT_MODES)),
        "StationAxis": ("station_axis", _to_enum(STATION_AXES)),
        "ReverseStationStart": ("reverse_station_start", _to_bool),
        "Stations": stations,
        "PileDiameterM": ("pile_diameter_m", _to_double),
        "PileRevealM": ("pile_reveal_m", _to_double),
        "PileEmbedmentM": ("pile_embedment_m", _to_double),
        "MinPileLengthM": ("min_pile_length_m", _to_double),
        "MaxPileLengthM": ("max_pile_length_m", _to_double),
        "RevealBucketBoundariesM": dlist("reveal_bucket_boundaries_m"),
        "PilesPerFrame": None,
    })


def deserialize_pile_template(text, *, replicate_list_reuse=True):
    """JsonConvert.DeserializeObject<PileTemplate> (PileTemplateTests.cs:31-39).
    Raises GroundFramesError on anything Newtonsoft would reject."""
    try:
        obj = _parse_json(text, "pile_template")
        if obj is None:
            return None
        if not isinstance(obj, _JsonObject):
            raise _Corrupt("root")
        template = PileTemplate()
        _populate(template, obj, _pile_template_table(replicate_list_reuse))
        return template
    except (_Corrupt, ValueError) as exc:
        if isinstance(exc, GroundFramesError):
            raise
        raise GroundFramesError("pile_template_malformed", str(exc)) from None


# ============================================================ preset store ==

@dataclass
class FramePresetStore:
    """Loaded state of FramePresetStore.cs. `presets` is keyed by the lowered name
    (OrdinalIgnoreCase) in insertion order."""
    presets: dict
    active_name: str
    corrupt: bool = False
    legacy_piling_preset_names: list = field(default_factory=list)
    legacy_default_piling: PilingConfig | None = None

    def get(self, name):
        """Get, FramePresetStore.cs:48-52."""
        if not name:
            return None
        return self.presets.get(name.lower())

    def get_active(self):
        """GetActive, FramePresetStore.cs:54."""
        return self.presets[self.active_name.lower()]

    def list(self):
        return list(self.presets.values())


def _find_legacy_piling_preset_names(root):
    """FindLegacyPilingPresetNames, FramePresetStore.cs:213-238 (JObject, so member
    lookups are exact-case and a present-but-null member still counts as present)."""
    names = set()
    if not isinstance(root, _JsonObject):
        return names
    presets = None
    for key, value in root:
        if key == "Presets":
            presets = value
    if not isinstance(presets, list) or isinstance(presets, _JsonObject):
        return names
    for token in presets:
        if not isinstance(token, _JsonObject):
            continue
        keys = [k for k, _ in token]
        if "PileTemplateName" in keys or "Piling" not in keys:
            continue
        name = None
        for key, value in token:
            if key == "Name":
                name = value
        if isinstance(name, (list, _JsonObject)):
            raise GroundFramesError("preset_file_unreadable",
                                    "a legacy preset Name is not a scalar (InvalidCastException in the plugin)")
        name = _to_string(name)
        if name is not None and name.strip():
            names.add(name.lower())
    return names


def load_frame_preset_store(text, *, replicate_list_reuse=True):
    """FramePresetStore.Load, FramePresetStore.cs:162-196, from the file's text.

    None (no file) or corrupt JSON yields the Default-only store, exactly like the
    plugin; `corrupt` records the second case. The Default preset is added last when
    the file lacks one (BuildDefault, FramePresetStore.cs:240-254)."""
    presets = {}
    active = "Default"
    corrupt = False
    legacy_names = []
    legacy_default = None
    if text is not None:
        try:
            root = _parse_json(text, "frame_presets")
        except json.JSONDecodeError:
            root, corrupt = None, True
        if not corrupt:
            legacy = _find_legacy_piling_preset_names(root)
            try:
                loaded, active_from_file = _deserialize_preset_dto(root, replicate_list_reuse)
            except _Corrupt:
                loaded, active_from_file, corrupt = [], None, True
            for p in loaded:
                if p.name.lower() in legacy:
                    p.pile_template_name = "Default"
                    legacy_names.append(p.name)
                    legacy_default = deepcopy(p.piling) if p.piling is not None else PilingConfig()
                presets[p.name.lower()] = p
            if active_from_file is not None and active_from_file.strip():
                active = active_from_file
    if "default" not in presets:
        presets["default"] = default_frame_preset()
    if active.lower() not in presets:
        active = "Default"
    return FramePresetStore(presets, active, corrupt, legacy_names, legacy_default)


def _deserialize_preset_dto(root, reuse):
    """StoreDto { SchemaVersion, ActiveName, Presets }, FramePresetStore.cs:256-261."""
    if root is None:
        return [], None
    if not isinstance(root, _JsonObject):
        raise _Corrupt("root")
    table = _frame_preset_table(reuse)
    active = None
    presets = None
    for key, value in root:
        low = key.lower()
        if low == "schemaversion":
            _to_int(value)
        elif low == "activename":
            active = _to_string(value)
        elif low == "presets":
            if value is None:
                presets = None
                continue
            if not isinstance(value, list) or isinstance(value, _JsonObject):
                raise _Corrupt("Presets")
            if len(value) + len(presets or []) > MAX_JSON_LIST_ITEMS:
                raise GroundFramesError("json_list_too_long", "Presets exceeds the item cap")
            presets = presets if (reuse and presets is not None) else []
            for item in value:
                if item is None:
                    presets.append(None)
                    continue
                if not isinstance(item, _JsonObject):
                    raise _Corrupt("FramePreset")
                preset = FramePreset()
                _populate(preset, item, table)
                presets.append(preset)
    kept = [p for p in (presets or []) if p is not None and p.name is not None and p.name.strip()]
    return kept, active


# ======================================================= pile template store ==

@dataclass
class PileTemplateStore:
    """Loaded state of PileTemplateStore.cs, keyed by lowered name."""
    templates: dict
    active_template: str
    corrupt: bool = False

    def get(self, name):
        """Get, PileTemplateStore.cs:46-50."""
        if name is None or not name.strip():
            return None
        return self.templates.get(name.lower())

    def get_active(self):
        """GetActive, PileTemplateStore.cs:54-61."""
        hit = self.templates.get(self.active_template.lower())
        if hit is not None:
            return hit
        hit = self.templates.get("full")
        return hit if hit is not None else next(iter(self.templates.values()), None)

    def resolve(self, preset):
        """Resolve, PileTemplateStore.cs:115-124."""
        if preset is not None and preset.pile_template_name and preset.pile_template_name.strip():
            named = self.get(preset.pile_template_name)
            if named is not None:
                return named
        return self.get_active() or default_full_pile_template()


def _normalize_template(t):
    """Normalize, PileTemplateStore.cs:229-268 (stable sort by OffsetM)."""
    t.name = t.name.strip() if t.name is not None else None
    t.horizontal_pole_count = max(1, t.horizontal_pole_count)
    t.vertical_pole_count = max(1, t.vertical_pole_count)
    if t.horizontal_distances_m is None:
        t.horizontal_distances_m = []
    if t.vertical_distances_m is None:
        t.vertical_distances_m = []
    stations = [s for s in (t.stations or []) if s is not None and math.isfinite(s.offset_m)]
    stations.sort(key=lambda s: s.offset_m)
    for s in stations:
        s.label = s.label if s.label is not None else ""
    t.stations = stations
    if t.placement_mode == "AxisStations" and not t.stations:
        t.placement_mode = "Grid"
    for attr in ("pile_diameter_m", "pile_reveal_m", "pile_embedment_m", "min_pile_length_m", "max_pile_length_m"):
        value = getattr(t, attr)
        setattr(t, attr, value if math.isfinite(value) and value >= 0.0 else 0.0)
    if not t.reveal_bucket_boundaries_m:
        t.reveal_bucket_boundaries_m = default_reveal_bucket_boundaries_m()
    return t


def load_pile_template_store(text, *, legacy_default_piling=None, replicate_list_reuse=True):
    """PileTemplateStore.ReadFromDisk, PileTemplateStore.cs:157-191, from the file's text.

    legacy_default_piling replays FramePresetStore.Load's EnsureLegacyDefault
    (FramePresetStore.cs:175-179, PileTemplateStore.cs:126-132): when the frame preset
    file holds a legacy piling preset, the plugin writes a "Default" template built from
    that preset's PilingConfig before LEAFPILING constructs this store."""
    templates = {}
    active = "Full"
    corrupt = False
    if text is not None:
        try:
            root = _parse_json(text, "pile_templates")
            raw, active_from_file = _deserialize_template_dto(root, replicate_list_reuse)
        except (json.JSONDecodeError, _Corrupt):
            raw, active_from_file, corrupt = [], None, True
        for key, template in raw:
            if template is None:
                continue
            if template.name is None or not template.name.strip():
                template.name = key
            _normalize_template(template)
            templates[template.name.lower()] = template
        if active_from_file is not None and active_from_file.strip():
            active = active_from_file
    if legacy_default_piling is not None:
        templates["default"] = _normalize_template(PileTemplate.from_legacy_config(legacy_default_piling, "Default"))
    if "full" not in templates:
        templates["full"] = default_full_pile_template()
    if active.lower() not in templates:
        active = "Full"
    return PileTemplateStore(templates, active, corrupt)


def _deserialize_template_dto(root, reuse):
    """StoreDto { Templates: Dictionary<string, PileTemplate>, ActiveTemplate }."""
    if root is None:
        return [], None
    if not isinstance(root, _JsonObject):
        raise _Corrupt("root")
    table = _pile_template_table(reuse)
    entries = {}
    active = None
    for key, value in root:
        low = key.lower()
        if low == "activetemplate":
            active = _to_string(value)
        elif low == "templates":
            if value is None:
                entries = {}
                continue
            if not isinstance(value, _JsonObject):
                raise _Corrupt("Templates")
            if len(value) + len(entries) > MAX_JSON_LIST_ITEMS:
                raise GroundFramesError("json_list_too_long", "Templates exceeds the item cap")
            entries = entries if reuse else {}
            for name, item in value:
                if item is None:
                    entries[name] = (name, None)
                    continue
                if not isinstance(item, _JsonObject):
                    raise _Corrupt("PileTemplate")
                template = PileTemplate()
                _populate(template, item, table)
                entries[name] = (name, template)  # indexer set keeps the first slot
    return list(entries.values()), active


# =================================================== PVCasePiling template JSON ==

def _jvalue(obj, key):
    """JToken.Value<T>(key): exact-case member, last duplicate wins."""
    found = None
    for k, v in obj:
        if k == key:
            found = (v,)
    return found[0] if found else None


def _jdouble(v, default):
    if v is None:
        return default
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and _FLOAT_TEXT.match(v):
        return float(v)
    raise GroundFramesError("pvcase_piling_malformed", f"not a number: {v!r}")


def _jint(v, default):
    if v is None:
        return default
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, float):
        if not math.isfinite(v):
            raise GroundFramesError("pvcase_piling_malformed", "non-finite integer member")
        v = round(v)  # Convert.ToInt32(double) rounds half to even
    elif isinstance(v, str):
        if not _INT_TEXT.match(v):
            raise GroundFramesError("pvcase_piling_malformed", f"not an integer: {v!r}")
        v = int(v)
    elif not isinstance(v, int):
        raise GroundFramesError("pvcase_piling_malformed", f"not an integer: {v!r}")
    if not INT32_MIN <= v <= INT32_MAX:
        raise GroundFramesError("pvcase_piling_malformed", "integer member overflows Int32")
    return v


def _jbool(v, default):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    raise GroundFramesError("pvcase_piling_malformed", f"not a boolean: {v!r}")


def _jenum(v, names, default):
    """ReadEnum, PileTemplate.cs:238-260 (never throws; unknown falls back)."""
    if isinstance(v, int) and not isinstance(v, bool):
        if 0 <= v < len(names):
            return names[v]
    if isinstance(v, str) and v.strip():
        text = v.strip()
        for n in names:
            if n.lower() == text.lower():
                return n
        if _INT_TEXT.match(text) and 0 <= int(text) < len(names):
            return names[int(text)]
    return default


def _jdouble_list(v):
    """ReadDoubleList, PileTemplate.cs:283-294."""
    out = []
    if not isinstance(v, list) or isinstance(v, _JsonObject):
        return out
    for item in v:
        if item is None:
            continue
        value = _jdouble(item, None)
        if value is not None and math.isfinite(value):
            out.append(value)
    return out


def _template_from_jobject(obj, fallback_name):
    """FromJObject, PileTemplate.cs:198-236."""
    name = _jvalue(obj, "Name")
    t = PileTemplate(
        name=name if isinstance(name, str) else (fallback_name or "Full"),
        are_equal_margins=_jbool(_jvalue(obj, "AreEqualMargins"), False),
        should_place_piles_at_joints=_jbool(_jvalue(obj, "ShouldPlacePilesAtJoints"), False),
        is_mirror_from_middle=_jbool(_jvalue(obj, "IsMirrorFromMiddle"), False),
        distribution_type=_jint(_jvalue(obj, "DistributionType"), 2),
        middle_distribution=_jdouble(_jvalue(obj, "MiddleDistribution"), 0.0),
        selected_middle_pole=_jbool(_jvalue(obj, "SelectedMiddlePole"), False),
        horizontal_pole_count=max(1, _jint(_jvalue(obj, "HorizontalPoleCount"), 2)),
        vertical_pole_count=max(1, _jint(_jvalue(obj, "VerticalPoleCount"), 1)),
    )
    t.placement_mode = _jenum(_jvalue(obj, "PlacementMode"), PLACEMENT_MODES, "Grid")
    t.station_axis = _jenum(_jvalue(obj, "StationAxis"), STATION_AXES, "LocalX")
    t.reverse_station_start = _jbool(_jvalue(obj, "ReverseStationStart"), False)
    t.pile_diameter_m = _jdouble(_jvalue(obj, "PileDiameterM"), 0.0)
    t.pile_reveal_m = _jdouble(_jvalue(obj, "PileRevealM"), 0.0)
    t.pile_embedment_m = _jdouble(_jvalue(obj, "PileEmbedmentM"), 0.0)
    t.min_pile_length_m = _jdouble(_jvalue(obj, "MinPileLengthM"), 0.0)
    t.max_pile_length_m = _jdouble(_jvalue(obj, "MaxPileLengthM"), 0.0)
    h = _jvalue(obj, "HorizontalDistancesM")
    t.horizontal_distances_m = _jdouble_list(h if h is not None else _jvalue(obj, "HorizontalDistances"))
    v = _jvalue(obj, "VerticalDistancesM")
    t.vertical_distances_m = _jdouble_list(v if v is not None else _jvalue(obj, "VerticalDistances"))
    stations = []
    raw_stations = _jvalue(obj, "Stations")
    if isinstance(raw_stations, list) and not isinstance(raw_stations, _JsonObject):
        for item in raw_stations:
            if not isinstance(item, _JsonObject):
                continue
            label = _jvalue(item, "Label")
            st = PileStation(offset_m=_jdouble(_jvalue(item, "OffsetM"), 0.0),
                             kind=_jenum(_jvalue(item, "Kind"), STATION_KINDS, "Bearing"),
                             label=label if isinstance(label, str) else "",
                             cross_axis_offset_m=_jdouble(_jvalue(item, "CrossAxisOffsetM"), 0.0))
            if math.isfinite(st.offset_m):
                _bounded_append(stations, st, MAX_STATIONS, "stations")
    t.stations = stations
    reveal = _jdouble_list(_jvalue(obj, "RevealBucketBoundariesM"))
    if reveal:
        t.reveal_bucket_boundaries_m = reveal
    return t


def parse_all_pvcase_piling_json(text):
    """ParseAllPvcasePilingJson, PileTemplate.cs:124-141."""
    if text is None or not str(text).strip():
        raise GroundFramesError("pvcase_piling_empty", "PVCasePiling JSON is required.")
    try:
        root = _parse_json(text, "pvcase_piling")
    except json.JSONDecodeError as exc:
        raise GroundFramesError("pvcase_piling_malformed", str(exc)) from None
    if not isinstance(root, _JsonObject):
        raise GroundFramesError("pvcase_piling_malformed", "root is not an object")
    templates = _jvalue(root, "Templates")
    if not isinstance(templates, _JsonObject):
        name = _jvalue(root, "Name")
        return [_template_from_jobject(root, name if isinstance(name, str) else "Full")]
    merged = {}
    for key, value in templates:
        merged[key] = value  # JObject duplicate members replace in place
    return [_template_from_jobject(obj, key) for key, obj in merged.items() if isinstance(obj, _JsonObject)]


def parse_pvcase_piling_json(text):
    """ParsePvcasePilingJson, PileTemplate.cs:113-122: prefer "Full", else the first."""
    templates = parse_all_pvcase_piling_json(text)
    if not templates:
        raise GroundFramesError("pvcase_piling_empty", "PVCasePiling JSON did not contain any templates.")
    for t in templates:
        if (t.name or "").lower() == "full":
            return t
    return templates[0]


# ================================================================ geometry ==

def _bounded_append(items, value, cap, what):
    if len(items) >= cap:
        raise GroundFramesError(f"{what}_over_cap", f"more than {cap} {what}")
    items.append(value)


def _finite(value, code, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise GroundFramesError(code, f"{what} must be a finite number")
    return float(value)


def _points(value, prefix, what, cap=MAX_POLYGON_VERTICES):
    """Validate a vertex list: 2+ element sequences of finite numbers (Z ignored).
    Refusals: <prefix>_malformed, <prefix>_too_many_vertices."""
    code = f"{prefix}_malformed"
    if isinstance(value, (str, bytes, dict)) or not hasattr(value, "__len__"):
        raise GroundFramesError(code, f"{what} must be a list of points")
    if len(value) > cap:
        raise GroundFramesError(f"{prefix}_too_many_vertices", f"{what} has more than {cap} vertices")
    pts = []
    for p in value:
        if isinstance(p, (str, bytes, dict)) or not hasattr(p, "__len__") or len(p) < 2:
            raise GroundFramesError(code, f"{what} holds a malformed point")
        pts.append((_finite(p[0], code, what), _finite(p[1], code, what)))
    return pts


def _bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def point_in_polygon(px, py, poly):
    """PointInPolygon, FramePacker.cs:249-283: ray cast, then accept on-edge points."""
    n = len(poly)
    inside = False
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[i - 1 if i else n - 1]
        if (yi > py) != (yj > py):
            x_cross = (xj - xi) * (py - yi) / (yj - yi) + xi
            if px < x_cross:
                inside = not inside
    if inside:
        return True
    eps = 1e-9
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[i - 1 if i else n - 1]
        dx = xj - xi
        dy = yj - yi
        len2 = dx * dx + dy * dy
        if len2 < eps:
            continue
        t = ((px - xi) * dx + (py - yi) * dy) / len2
        if t < -eps or t > 1 + eps:
            continue
        qx = xi + t * dx
        qy = yi + t * dy
        if abs(qx - px) < eps and abs(qy - py) < eps:
            return True
    return False


def _cross(ax, ay, bx, by):
    return ax * by - ay * bx


def _segments_intersect(p1, p2, p3, p4):
    """SegmentsIntersect, FramePacker.cs:209-220 (proper crossings only)."""
    d1 = _cross(p4[0] - p3[0], p4[1] - p3[1], p1[0] - p3[0], p1[1] - p3[1])
    d2 = _cross(p4[0] - p3[0], p4[1] - p3[1], p2[0] - p3[0], p2[1] - p3[1])
    d3 = _cross(p2[0] - p1[0], p2[1] - p1[1], p3[0] - p1[0], p3[1] - p1[1])
    d4 = _cross(p2[0] - p1[0], p2[1] - p1[1], p4[0] - p1[0], p4[1] - p1[1])
    return (((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0))
            and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)))


def _polygons_overlap(a, b):
    """PolygonsOverlap, FramePacker.cs:193-207."""
    for p in a:
        if point_in_polygon(p[0], p[1], b):
            return True
    for p in b:
        if point_in_polygon(p[0], p[1], a):
            return True
    na, nb = len(a), len(b)
    for i in range(na):
        a1, a2 = a[i - 1 if i else na - 1], a[i]
        for k in range(nb):
            if _segments_intersect(a1, a2, b[k - 1 if k else nb - 1], b[k]):
                return True
    return False


def circle_to_polygon(cx, cy, radius, sides=32):
    """CircleToPolygon, FramePacker.cs:227-238. math.cos/sin may differ from .NET's
    Math.Cos/Sin in the last ulp."""
    sides = max(3, int(sides))
    if sides > MAX_POLYGON_VERTICES:
        raise GroundFramesError("circle_sides_over_cap", f"more than {MAX_POLYGON_VERTICES} sides")
    return [(cx + radius * math.cos(2.0 * math.pi * i / sides), cy + radius * math.sin(2.0 * math.pi * i / sides))
            for i in range(sides)]


# ============================================================== FramePacker ==

def frame_footprint(preset):
    """FrameFootprint, FramePacker.cs:43-71. Returns (width_m, height_m)."""
    if preset is None:
        raise GroundFramesError("preset_missing", "a frame preset is required")
    if preset.rows < 1 or preset.columns < 1:
        raise GroundFramesError("preset_rows_columns", "Preset Rows and Columns must be >= 1.")
    m_len = preset.module_length_m
    m_wid = preset.module_width_m
    pack = preset.get_tracker_pack()
    tracker_len = pack.tracker_length_m(m_wid) if pack.has_intra_tracker_segments() else 0.0
    use_pack = tracker_len > 0.0
    if preset.orientation == "Landscape":
        width = preset.rows * m_len + max(0, preset.rows - 1) * preset.horizontal_gap_m
        height = tracker_len if use_pack else preset.columns * m_wid + max(0, preset.columns - 1) * preset.vertical_gap_m
        return width, height
    width = preset.rows * m_wid + max(0, preset.rows - 1) * preset.horizontal_gap_m
    height = tracker_len if use_pack else preset.columns * m_len + max(0, preset.columns - 1) * preset.vertical_gap_m
    return width, height


def _prepare_exclusions(exclusion_polys):
    if exclusion_polys is None:
        return []
    if isinstance(exclusion_polys, (str, bytes, dict)) or not hasattr(exclusion_polys, "__len__"):
        raise GroundFramesError("exclusions_malformed", "exclusion_polys must be a list of polygons")
    if len(exclusion_polys) > MAX_EXCLUSIONS:
        raise GroundFramesError("exclusions_over_cap", f"more than {MAX_EXCLUSIONS} exclusion polygons")
    prepared = []
    for poly in exclusion_polys:
        if poly is None:
            continue
        pts = _points(poly, "exclusion", "exclusion polygon")
        if len(pts) < 3:
            continue  # FramePacker.cs:187
        prepared.append((pts, _bbox(pts)))
    return prepared


def pack_frames(boundary, preset, meters_per_unit=1.0, exclusion_polys=None, pitch_m=0.0,
                slope_at_point_percent=None):
    """FramePacker.Pack, FramePacker.cs:87-151.

    Returns frames as dicts {row, col, vertices} in the plugin's order: rows from the
    boundary bbox minimum Y upward, columns from minimum X rightward, with the plugin's
    accumulating float steps. A bbox prefilter skips exclusion tests that cannot hit
    (margin 1e-6 against the 1e-9 edge tolerance), which leaves the result unchanged."""
    pts = _points(boundary, "boundary", "boundary")
    if len(pts) < 3:
        raise GroundFramesError("boundary_too_few_vertices", "Boundary must have at least 3 vertices.")
    if preset is None:
        raise GroundFramesError("preset_missing", "a frame preset is required")
    mpu = _finite(meters_per_unit, "meters_per_unit_invalid", "meters_per_unit")
    if mpu <= 0:
        raise GroundFramesError("meters_per_unit_invalid", "metersPerUnit must be > 0.")
    pitch = _finite(pitch_m, "pitch_invalid", "pitch_m")
    width_m, height_m = frame_footprint(preset)
    fw = width_m / mpu
    fh = height_m / mpu
    if not (math.isfinite(fw) and math.isfinite(fh)) or fw <= 0.0 or fh <= 0.0:
        raise GroundFramesError("frame_footprint_degenerate",
                                f"frame footprint {width_m} x {height_m} m cannot be packed (the plugin would not terminate)")
    row_step = max(fh, pitch / mpu) if pitch > 0.0 else fh
    exclusions = _prepare_exclusions(exclusion_polys)
    max_slope = preset.max_slope_percent
    x_min, y_min, x_max, y_max = _bbox(pts)

    rows_est = 0 if y_max + 1e-9 - y_min - fh < 0 else math.floor((y_max + 1e-9 - y_min - fh) / row_step) + 2
    cols_est = 0 if x_max + 1e-9 - x_min - fw < 0 else math.floor((x_max + 1e-9 - x_min - fw) / fw) + 2
    if rows_est * cols_est > MAX_CANDIDATE_CELLS:
        raise GroundFramesError("candidate_cells_over_cap",
                                f"about {rows_est * cols_est} candidate frame cells exceed {MAX_CANDIDATE_CELLS}")

    frames = []
    row_idx = 0
    y = y_min
    while y + fh <= y_max + 1e-9:
        col_idx = 0
        x = x_min
        while x + fw <= x_max + 1e-9:
            verts = [(x, y), (x + fw, y), (x + fw, y + fh), (x, y + fh)]
            if (all(point_in_polygon(vx, vy, pts) for vx, vy in verts)
                    and not _intersects_any_exclusion(verts, exclusions)
                    and not _slope_exceeds(verts, slope_at_point_percent, max_slope)):
                _bounded_append(frames, {"row": row_idx, "col": col_idx, "vertices": verts}, MAX_FRAMES, "frames")
            col_idx += 1
            x += fw
        row_idx += 1
        y += row_step
    return frames


def _intersects_any_exclusion(verts, exclusions):
    """IntersectsAnyExclusion, FramePacker.cs:180-191."""
    if not exclusions:
        return False
    fx0, fy0, fx1, fy1 = _bbox(verts)
    margin = 1e-6
    for poly, (ex0, ey0, ex1, ey1) in exclusions:
        if fx1 < ex0 - margin or ex1 < fx0 - margin or fy1 < ey0 - margin or ey1 < fy0 - margin:
            continue
        if _polygons_overlap(verts, poly):
            return True
    return False


def _slope_exceeds(verts, slope_fn, max_slope_percent):
    """SlopeExceeds, FramePacker.cs:153-173: centroid slope, NaN never rejects."""
    if slope_fn is None or max_slope_percent <= 0.0:
        return False
    cx = 0.0
    cy = 0.0
    for x, y in verts:
        cx += x
        cy += y
    cx /= len(verts)
    cy /= len(verts)
    slope = slope_fn(cx, cy)
    if slope is None:
        return False
    slope = float(slope)
    return not math.isnan(slope) and slope > max_slope_percent


def _sample(terrain_z_m, x, y):
    z = terrain_z_m(x, y)
    if z is None:
        return None
    return _finite(z, "terrain_sample_invalid", "terrain sample")


def slope_filter(terrain_z_m, preset, meters_per_unit):
    """CreateSlopeFilter + SlopeAtPointPercent, LeafGenerateCommand.cs:356-386.
    Forward differences one drawing unit east and north; NaN when any sample is off
    the grid. None when there is no terrain or the preset disables the filter."""
    if terrain_z_m is None or preset is None or preset.max_slope_percent <= 0.0:
        return None

    def slope(x, y):
        z0 = _sample(terrain_z_m, x, y)
        zx = _sample(terrain_z_m, x + 1.0, y)
        zy = _sample(terrain_z_m, x, y + 1.0)
        if z0 is None or zx is None or zy is None:
            return math.nan
        horizontal_m = 1.0 * meters_per_unit
        if horizontal_m <= 1e-12:
            return math.nan
        gx = (zx - z0) / horizontal_m
        gy = (zy - z0) / horizontal_m
        return math.sqrt(gx * gx + gy * gy) * 100.0
    return slope


# ============================================================== setbacks ==

def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _point_on_segment(p, a, b, eps=1e-9):
    """PointOnSegment, SetbackZones.cs:499-508."""
    if abs(_orient(a, b, p)) > eps:
        return False
    return (min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps
            and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps)


def _point_in_or_on_polygon(p, poly):
    """PointInOrOnPolygon, SetbackZones.cs:217-241."""
    n = len(poly)
    if n < 3:
        return False
    for i in range(n):
        if _point_on_segment(p, poly[i - 1 if i else n - 1], poly[i]):
            return True
    inside = False
    for i in range(n):
        a = poly[i]
        b = poly[i - 1 if i else n - 1]
        if (a[1] > p[1]) != (b[1] > p[1]):
            x_cross = (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0]
            if p[0] < x_cross:
                inside = not inside
    return inside


def _properly_crosses(pts, closed, poly, eps=1e-9):
    """PolylineProperlyCrossesPolygon + SegmentsProperlyIntersect, SetbackZones.cs:460-492."""
    count = len(pts) if closed else len(pts) - 1
    for i in range(max(0, count)):
        a1, a2 = pts[i], pts[(i + 1) % len(pts)]
        for j in range(len(poly)):
            b1, b2 = poly[j], poly[(j + 1) % len(poly)]
            if (_orient(a1, a2, b1) * _orient(a1, a2, b2) < -eps
                    and _orient(b1, b2, a1) * _orient(b1, b2, a2) < -eps):
                return True
    return False


def filter_frames_inside_setbacks(frames, setback_polys):
    """FilterFramesInsideSetbacks, SetbackZones.cs:127-153 with
    IsFootprintInsideAllSetbacks, SetbackZones.cs:181-204. Returns (kept, rejected)."""
    setbacks = []
    for poly in setback_polys or []:
        pts = _points(poly, "setback", "setback polygon")
        if len(pts) >= 3:
            setbacks.append(pts)
    if not setbacks:
        return list(frames), 0
    kept = []
    rejected = 0
    for frame in frames:
        verts = frame["vertices"]
        ok = bool(verts)
        for setback in (setbacks if ok else ()):
            if not all(_point_in_or_on_polygon(p, setback) for p in verts) or _properly_crosses(verts, True, setback):
                ok = False
                break
        if ok:
            kept.append(frame)
        else:
            rejected += 1
    return kept, rejected


# ================================================================ entities ==

def _entities(entities):
    if isinstance(entities, (str, bytes, dict)) or not hasattr(entities, "__len__"):
        raise GroundFramesError("entities_malformed", "entities must be a list of entity dicts")
    if len(entities) > MAX_ENTITIES:
        raise GroundFramesError("entities_over_cap", f"more than {MAX_ENTITIES} entities")
    for ent in entities:
        if not isinstance(ent, dict):
            raise GroundFramesError("entities_malformed", "every entity must be a dict")
    return entities


def _layer_is(ent, layer):
    value = ent.get("layer")
    return isinstance(value, str) and value.upper() == layer.upper()


def _is_lwpolyline(ent):
    return str(ent.get("type", "")).upper() == "LWPOLYLINE"


def _handle(ent):
    h = ent.get("handle")
    return h.upper() if isinstance(h, str) and h.strip() else None


def xdata_for_app(xdata, app):
    """ResultBuffer of GetXDataForApplication: the (1001, app) item and the items that
    follow it up to the next 1001. None when the app is absent."""
    if not xdata:
        return None
    out = None
    for item in xdata:
        if isinstance(item, (str, bytes)) or not hasattr(item, "__len__") or len(item) != 2:
            raise GroundFramesError("xdata_malformed", "xdata items must be (code, value) pairs")
        code, value = item
        if code == DXF_REGAPP:
            if out is not None:
                break
            if isinstance(value, str) and value.upper() == app.upper():
                out = [(code, value)]
        elif out is not None:
            out.append((code, value))
    return out


def _read_frame_cell(ent):
    """TryReadLeafFrameCell, LeafPilingCommand.cs:1115-1138 (same loop as
    LeafCollisionCommand.cs:110-125): the first two 1070 values are row and col."""
    block = xdata_for_app(ent.get("xdata"), XDATA_FRAMECELL)
    if block is None:
        return None
    ints = []
    for code, value in block:
        if code == DXF_INT16 and len(ints) < 2:
            if isinstance(value, bool) or not isinstance(value, int):
                raise GroundFramesError("xdata_malformed", "a 1070 value must be an integer")
            ints.append(value)
    return (ints[0], ints[1]) if len(ints) >= 2 else None


def read_tracker_frames(entities):
    """ReadTrackerFrames, LeafCollisionCommand.cs:94-143, and TryBuildNativeFrameSource,
    LeafPilingCommand.cs:977-1013: closed LWPOLYLINE on LEAF-TRACKERS with exactly four
    vertices and LEAFFRAMECELL row/col. Entity order is kept."""
    frames = []
    for index, ent in enumerate(_entities(entities)):
        if not _is_lwpolyline(ent) or not ent.get("closed"):
            continue
        if not _layer_is(ent, LAYER_TRACKERS):
            continue
        verts = ent.get("vertices") or []
        if len(verts) != 4:
            continue
        cell = _read_frame_cell(ent)
        if cell is None:
            continue
        pts = _points(verts, "tracker_frame", "tracker frame")
        _bounded_append(frames, {"row": cell[0], "col": cell[1], "vertices": pts,
                                 "handle": _handle(ent), "entity_index": index}, MAX_FRAMES, "frames")
    return frames


def restriction_polygons(entities, sides=32):
    """LoadRestrictionPolygons, LeafGenerateCommand.cs:468-504: circles as 32-gons and
    closed polylines on LEAF-PVCASE-SHADING-RESTRICTION, in entity order."""
    out = []
    for ent in _entities(entities):
        if not _layer_is(ent, LAYER_SHADING_RESTRICTION):
            continue
        kind = str(ent.get("type", "")).upper()
        if kind == "CIRCLE":
            center = _points([ent.get("center")], "restriction", "circle center")[0]
            radius = _finite(ent.get("radius"), "restriction_malformed", "circle radius")
            out.append(circle_to_polygon(center[0], center[1], radius, sides))
        elif kind == "LWPOLYLINE" and ent.get("closed") and len(ent.get("vertices") or []) >= 3:
            out.append(_points(ent["vertices"], "restriction", "restriction polygon"))
    return out


# ====================================================== LEAFGENERATE picking ==

def find_boundary_candidates(entities):
    """FindBoundaryCandidates, LeafGenerateCommand.cs:506-525: closed LWPOLYLINEs with at
    least three vertices on any layer that does not start with LEAF-."""
    out = []
    for index, ent in enumerate(_entities(entities)):
        if not _is_lwpolyline(ent) or not ent.get("closed"):
            continue
        if len(ent.get("vertices") or []) < 3:
            continue
        layer = ent.get("layer")
        if isinstance(layer, str) and layer.upper().startswith("LEAF-"):
            continue
        out.append(index)
    return out


def _extract_vertices(entities, indexes):
    """ExtractVertices, LeafGenerateCommand.cs:527-547."""
    out = []
    for i in indexes:
        ent = entities[i]
        if not _is_lwpolyline(ent) or not ent.get("closed") or len(ent.get("vertices") or []) < 3:
            continue
        _bounded_append(out, _points(ent["vertices"], "boundary", "boundary"), MAX_BOUNDARIES, "boundaries")
    return out


def pick_boundaries(entities, multi, selection=None):
    """PickBoundaries, LeafGenerateCommand.cs:420-459.

    Auto-selects when there are candidates and either multi is set or exactly one
    exists. Otherwise the plugin prompts; `selection` stands in for the user's pick as
    entity indexes (one for LEAFGENERATE, any number for LEAFGENERATEMULTI). Returns
    {"mode": "auto"|"prompt", "candidates": [...], "boundaries": list|None}."""
    ents = _entities(entities)
    auto = find_boundary_candidates(ents)
    if auto and (multi or len(auto) == 1):
        return {"mode": "auto", "candidates": auto, "boundaries": _extract_vertices(ents, auto)}
    if selection is None:
        return {"mode": "prompt", "candidates": auto, "boundaries": None}
    picked = list(selection)
    if not multi and len(picked) != 1:
        raise GroundFramesError("selection_invalid", "LEAFGENERATE picks exactly one entity")
    for i in picked:
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(ents):
            raise GroundFramesError("selection_invalid", f"selection index {i!r} is out of range")
    return {"mode": "prompt", "candidates": auto, "boundaries": _extract_vertices(ents, picked)}


# ====================================================== LEAFGENERATE drawing ==

def frame_color(color_index):
    """DrawFrames colour rule, LeafGenerateCommand.cs:563-566: ACI 1..255 explicit, else ByLayer."""
    if isinstance(color_index, int) and not isinstance(color_index, bool) and 1 <= color_index <= 255:
        return {"method": "ByAci", "index": color_index}
    return dict(BY_LAYER)


def frame_xdata(row, col, site_revision=None):
    """LEAFFRAMECELL XData, LeafGenerateCommand.cs:605-614: app name, row and col as
    Int16 clamped to 32767, then the site revision string when one is set."""
    items = [(DXF_REGAPP, XDATA_FRAMECELL), (DXF_INT16, min(row, SHORT_MAX)), (DXF_INT16, min(col, SHORT_MAX))]
    if site_revision is not None and str(site_revision).strip():
        items.append((DXF_ASCII, str(site_revision)))
    return items


def frame_entities(frames, color_index, meters_per_unit, terrain_z_m=None, site_revision=None):
    """DrawFrames, LeafGenerateCommand.cs:554-624: one closed 4-vertex LWPOLYLINE per
    frame on LEAF-TRACKERS, in frame order. Elevation is the terrain Z at the vertex
    centroid in drawing units, 0.0 without terrain or off the grid."""
    if not frames:
        return []
    mpu = meters_per_unit if meters_per_unit > 0.0 else 1.0
    color = frame_color(color_index)
    out = []
    for f in frames:
        elevation = 0.0
        if terrain_z_m is not None:
            cx = 0.0
            cy = 0.0
            for vx, vy in f["vertices"]:
                cx += vx
                cy += vy
            cx /= len(f["vertices"])
            cy /= len(f["vertices"])
            z = _sample(terrain_z_m, cx, cy)
            if z is not None:
                elevation = z / mpu
        out.append({"type": "LWPOLYLINE", "layer": LAYER_TRACKERS, "closed": True,
                    "vertices": [tuple(v) for v in f["vertices"]], "color": dict(color),
                    "elevation": elevation, "xdata": frame_xdata(f["row"], f["col"], site_revision)})
    return out


def _generate_one(boundary, preset, mpu, exclusions, pitch_m, slope_fn, array_setbacks, terrain_z_m, site_revision):
    packed = pack_frames(boundary, preset, mpu, exclusions, pitch_m, slope_fn)
    rejected = 0
    if array_setbacks:
        packed, rejected = filter_frames_inside_setbacks(packed, array_setbacks)
    return packed, rejected, frame_entities(packed, preset.color_index, mpu, terrain_z_m, site_revision)


def _generation_result(frames, entities, rejected, extra):
    result = {"frames": frames, "entities": entities, "frame_count": len(frames), "skipped_by_setback": rejected,
              "layers_created": [{"name": LAYER_TRACKERS, "aci": TRACKERS_LAYER_ACI}] if entities else [],
              "regapps": [XDATA_FRAMECELL] if entities else []}
    result.update(extra)
    return result


def generate_frames(boundaries, preset, meters_per_unit, *, exclusion_polys=None, array_setbacks=None,
                    terrain_z_m=None, site_revision=None):
    """RunGenerate, LeafGenerateCommand.cs:54-174 (LEAFGENERATE and LEAFGENERATEMULTI
    once boundaries are picked). exclusion_polys carries shading restrictions and road
    buffers (LeafGenerateCommand.cs:88-99); array_setbacks the LEAF setback polygons.
    Row and column indexes restart at 0 for every boundary, as in the plugin."""
    if boundaries is None or len(boundaries) == 0:
        return _generation_result([], [], 0, {"status": "no_boundary", "boundary_count": 0})
    if len(boundaries) > MAX_BOUNDARIES:
        raise GroundFramesError("boundaries_over_cap", f"more than {MAX_BOUNDARIES} boundaries")
    exclusions = _prepare_exclusions(exclusion_polys)
    exclusion_lists = [poly for poly, _ in exclusions]
    slope_fn = slope_filter(terrain_z_m, preset, meters_per_unit)
    frames, entities, rejected = [], [], 0
    for b_index, boundary in enumerate(boundaries):
        packed, skipped, ents = _generate_one(boundary, preset, meters_per_unit, exclusion_lists, 0.0, slope_fn,
                                              array_setbacks, terrain_z_m, site_revision)
        rejected += skipped
        for f in packed:
            f["boundary_index"] = b_index
        frames.extend(packed)
        entities.extend(ents)
        if len(frames) > MAX_FRAMES:
            raise GroundFramesError("frames_over_cap", f"more than {MAX_FRAMES} frames")
    return _generation_result(frames, entities, rejected,
                              {"status": "ok", "boundary_count": len(boundaries), "preset_name": preset.name})


def generate_frames_for_areas(areas, store, meters_per_unit, *, exclusion_polys=None, array_setbacks=None,
                              terrain_z_m=None, site_revision=None):
    """RunGenerateAreas, LeafGenerateCommand.cs:237-354 (LEAFGENERATE when the drawing
    holds PV areas). Each area dict: name, boundary, frame_preset_name,
    exclusion_polys, pitch_override_m. None entries and boundaries under three
    vertices are skipped and counted."""
    if len(areas) > MAX_BOUNDARIES:
        raise GroundFramesError("boundaries_over_cap", f"more than {MAX_BOUNDARIES} areas")
    active = store.get_active()
    shared = [poly for poly, _ in _prepare_exclusions(exclusion_polys)]
    frames, entities, rejected, generated, skipped = [], [], 0, 0, []
    for a_index, area in enumerate(areas):
        if area is None:
            skipped.append(a_index)
            continue
        if not isinstance(area, dict):
            raise GroundFramesError("area_malformed", "every area must be a dict or None")
        boundary = area.get("boundary") or []
        if len(boundary) < 3:
            skipped.append(a_index)
            continue
        name = area.get("frame_preset_name")
        preset = store.get(name) if isinstance(name, str) and name.strip() else None
        if preset is None:
            preset = active
        slope_fn = slope_filter(terrain_z_m, preset, meters_per_unit)
        area_excl = shared + [poly for poly, _ in _prepare_exclusions(area.get("exclusion_polys"))]
        pitch = area.get("pitch_override_m") or 0.0
        pitch = _finite(pitch, "pitch_invalid", "pitch_override_m")
        pitch = pitch if pitch > 0.0 else 0.0
        packed, skipped_setback, ents = _generate_one(boundary, preset, meters_per_unit, area_excl, pitch, slope_fn,
                                                      array_setbacks, terrain_z_m, site_revision)
        rejected += skipped_setback
        for f in packed:
            f["area_index"] = a_index
            f["preset_name"] = preset.name
        frames.extend(packed)
        entities.extend(ents)
        generated += 1
        if len(frames) > MAX_FRAMES:
            raise GroundFramesError("frames_over_cap", f"more than {MAX_FRAMES} frames")
    return _generation_result(frames, entities, rejected,
                              {"status": "ok", "areas_generated": generated, "areas_skipped": skipped})


# =========================================================== LEAFCOLLISION ==

def _frame_bounds(frame):
    """Bounds, CollisionDetector.cs:84-99."""
    verts = frame.get("vertices") if isinstance(frame, dict) else None
    if not verts:
        raise GroundFramesError("frame_without_vertices", "PackedFrame has no vertices.")
    return _bbox(verts)


def detect_overlaps(frames):
    """DetectOverlaps, CollisionDetector.cs:44-78: every pair i < j whose bounding boxes
    overlap with positive area, ordered by (i, j). A sweep over X replaces the
    all-pairs scan; the pairs and their intersection boxes are identical."""
    if frames is None:
        raise GroundFramesError("frames_missing", "frames are required")
    if len(frames) > MAX_FRAMES:
        raise GroundFramesError("frames_over_cap", f"more than {MAX_FRAMES} frames")
    boxes = [_frame_bounds(f) for f in frames]
    order = sorted(range(len(frames)), key=lambda k: (boxes[k][0], k))
    active = []
    hits = []
    for k in order:
        x0 = boxes[k][0]
        active = [a for a in active if boxes[a][2] > x0]
        for a in active:
            i, j = (a, k) if a < k else (k, a)
            ax0, ay0, ax1, ay1 = boxes[i]
            bx0, by0, bx1, by1 = boxes[j]
            ix0, iy0 = max(ax0, bx0), max(ay0, by0)
            ix1, iy1 = min(ax1, bx1), min(ay1, by1)
            if ix1 > ix0 and iy1 > iy0:
                _bounded_append(hits, (i, j, ix0, iy0, ix1, iy1), MAX_COLLISIONS, "collisions")
        active.append(k)
    hits.sort(key=lambda h: (h[0], h[1]))
    return [{"frame_a_index": i, "frame_b_index": j,
             "frame_a_row": frames[i].get("row", 0), "frame_a_col": frames[i].get("col", 0),
             "frame_b_row": frames[j].get("row", 0), "frame_b_col": frames[j].get("col", 0),
             "min_x": ix0, "min_y": iy0, "max_x": ix1, "max_y": iy1}
            for i, j, ix0, iy0, ix1, iy1 in hits]


def _rectangle_marker(min_x, min_y, max_x, max_y):
    """Marker polyline, LeafCollisionCommand.cs:156-165 and LeafCollisionRangeCommand.cs:204-213."""
    return {"type": "LWPOLYLINE", "layer": LAYER_COLLISION, "closed": True, "color": dict(BY_LAYER),
            "elevation": 0.0, "vertices": [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)],
            "xdata": []}


def run_collision(entities):
    """LEAFCOLLISION, LeafCollisionCommand.cs:32-92. Markers land on LEAF-COLLISION
    (created red, ACI 1, when absent) only when there is at least one overlap."""
    frames = read_tracker_frames(entities)
    if not frames:
        return {"status": "no_frames", "frame_count": 0, "collisions": [], "markers": [], "layers_created": []}
    hits = detect_overlaps(frames)
    markers = [_rectangle_marker(h["min_x"], h["min_y"], h["max_x"], h["max_y"]) for h in hits]
    return {"status": "overlaps" if hits else "no_overlaps", "frame_count": len(frames), "collisions": hits,
            "markers": markers,
            "layers_created": [{"name": LAYER_COLLISION, "aci": COLLISION_LAYER_ACI}] if hits else []}


# ============================================================== PilePlacer ==

def _new_pile(frame, **values):
    pile = {"frame_row": frame.get("row", 0), "frame_col": frame.get("col", 0), "pile_h": 0, "pile_v": 0,
            "is_joint_pile": False, "joint_kind": "Modules", "joint_offset_m": 0.0, "placement_mode": "Grid",
            "station_kind": "Bearing", "station_label": "", "template_name": "", "mapping_signature": "",
            "pile_diameter_m": 0.0, "pile_reveal_m": 0.0, "pile_embedment_m": 0.0,
            "x": 0.0, "y": 0.0, "z": 0.0, "source_tracker_handle": None}
    pile.update(values)
    return pile


def _build_axis_positions(lo, hi, count, distances, selected_middle_pole):
    """BuildAxisPositions + Deduplicate, PilePlacer.cs:299-360 (no extra offsets)."""
    length = hi - lo
    positions = []
    if distances is not None and len(distances) == count + 1:
        total = 0.0
        for d in distances:
            if d > 0.0:
                total += d
        if total > 0.0:
            cursor = 0.0
            for i in range(count):
                cursor += max(0.0, distances[i])
                positions.append(lo + length * cursor / total)
    if not positions:
        for i in range(count):
            positions.append(lo + length * (i + 0.5) / count)
    if selected_middle_pole:
        positions.append((lo + hi) * 0.5)
    positions.sort()
    out = []
    for value in positions:
        if not out or abs(out[-1] - value) > 1e-7:
            out.append(value)
    return out


def _choose_axis_is_x(markers, x0, y0, x1, y1):
    """ChooseAxisIsX, PilePlacer.cs:262-281."""
    max_offset = 0.0
    for m in markers:
        if m is not None and m.offset_m > max_offset:
            max_offset = m.offset_m
    width = abs(x1 - x0)
    height = abs(y1 - y0)
    fits_x = max_offset <= width + 1e-9
    fits_y = max_offset <= height + 1e-9
    if fits_x and not fits_y:
        return True
    if fits_y and not fits_x:
        return False
    return width >= height


def _add_joint_piles(frame, markers, sample_z, x0, y0, x1, y1, piles):
    """AddJointPiles + HasGridPileNearAxisOffset, PilePlacer.cs:134-183, 283-297."""
    if not markers:
        return
    axis_is_x = _choose_axis_is_x(markers, x0, y0, x1, y1)
    axis_start = min(x0, x1) if axis_is_x else min(y0, y1)
    axis_end = max(x0, x1) if axis_is_x else max(y0, y1)
    cross_center = (y0 + y1) * 0.5 if axis_is_x else (x0 + x1) * 0.5
    joint_index = 0
    for m in markers:
        if m is None:
            continue
        axis_coord = axis_start + m.offset_m
        if axis_coord < axis_start - 1e-9 or axis_coord > axis_end + 1e-9:
            continue
        tolerance = max(m.gap_width_m * 0.5, 1e-6)
        if any(not p["is_joint_pile"] and abs((p["x"] if axis_is_x else p["y"]) - axis_coord) < tolerance
               for p in piles):
            continue
        x = axis_coord if axis_is_x else cross_center
        y = cross_center if axis_is_x else axis_coord
        z = sample_z(x, y) if sample_z is not None else None
        piles.append(_new_pile(frame, pile_h=-1, pile_v=joint_index, is_joint_pile=True, joint_kind=m.kind,
                               joint_offset_m=m.offset_m, station_kind="Joint", x=x, y=y,
                               z=z if z is not None else 0.0))
        joint_index += 1


def _place_axis_stations(frame, template, sample_z, meters_per_unit):
    """PlaceAxisStations, PilePlacer.cs:185-242."""
    verts = frame["vertices"]
    min_x, min_y, max_x, max_y = _bbox(verts)
    mpu = meters_per_unit if meters_per_unit > 1e-12 else 1.0
    axis_is_x = template.station_axis == "LocalX"
    axis_min = min_x if axis_is_x else min_y
    axis_max = max_x if axis_is_x else max_y
    cross_center = (min_y + max_y) * 0.5 if axis_is_x else (min_x + max_x) * 0.5
    length = abs(axis_max - axis_min)
    start = axis_max if template.reverse_station_start else axis_min
    direction = -1.0 if template.reverse_station_start else 1.0
    piles = []
    index = 0
    for st in template.stations:
        if st is None:
            continue
        offset = st.offset_m / mpu
        if offset < -1e-9 or offset > length + 1e-9:
            continue
        axis_coord = start + direction * offset
        cross = cross_center + st.cross_axis_offset_m / mpu
        x = axis_coord if axis_is_x else cross
        y = cross if axis_is_x else axis_coord
        joint_kind = "Motor" if st.kind == "Drive" else "JointGap" if st.kind == "Joint" else "Modules"
        z = sample_z(x, y) if sample_z is not None else None
        piles.append(_new_pile(frame, pile_h=index, pile_v=0, is_joint_pile=st.kind == "Joint",
                               joint_kind=joint_kind, joint_offset_m=st.offset_m, placement_mode="AxisStations",
                               station_kind=st.kind, station_label=st.label or "", x=x, y=y,
                               z=z if z is not None else 0.0))
        index += 1
    return piles


def place_piles(frame, template, sample_z=None, joint_markers=None, meters_per_unit=1.0):
    """PilePlacer.Place, PilePlacer.cs:38-114. `template` may be a PilingConfig (the
    legacy overload, converted by FromLegacyConfig) or a PileTemplate. Grid order is
    vertical index outer, horizontal inner; joint piles follow."""
    if frame is None:
        raise GroundFramesError("frame_missing", "a frame is required")
    if template is None:
        raise GroundFramesError("template_missing", "a pile template is required")
    if isinstance(template, PilingConfig):
        if template.horizontal_poles_per_frame < 1 or template.vertical_poles_per_group < 1:
            raise GroundFramesError("piling_counts", "PilingConfig pole counts must be >= 1.")
        template = PileTemplate.from_legacy_config(template)
    if template.horizontal_pole_count < 1 or template.vertical_pole_count < 1:
        raise GroundFramesError("template_counts", "PileTemplate pole counts must be >= 1.")
    if template.horizontal_pole_count > MAX_POLES_PER_AXIS or template.vertical_pole_count > MAX_POLES_PER_AXIS:
        raise GroundFramesError("template_counts_over_cap", f"more than {MAX_POLES_PER_AXIS} poles on an axis")
    if template.stations is not None and len(template.stations) > MAX_STATIONS:
        raise GroundFramesError("stations_over_cap", f"more than {MAX_STATIONS} stations")
    verts = frame.get("vertices") or []
    if len(verts) < 3:
        raise GroundFramesError("frame_malformed", "a frame needs at least three vertices")
    if template.placement_mode == "AxisStations" and template.stations:
        return _place_axis_stations(frame, template, sample_z, meters_per_unit)
    x0, y0 = verts[0]
    x1, y1 = verts[2]
    xs = _build_axis_positions(x0, x1, template.horizontal_pole_count, template.horizontal_distances_m,
                               template.selected_middle_pole)
    ys = _build_axis_positions(y0, y1, template.vertical_pole_count, template.vertical_distances_m, False)
    piles = []
    for v, y in enumerate(ys):
        for h, x in enumerate(xs):
            z = sample_z(x, y) if sample_z is not None else None
            piles.append(_new_pile(frame, pile_h=h, pile_v=v, x=x, y=y, z=z if z is not None else 0.0))
    if template.should_place_piles_at_joints:
        _add_joint_piles(frame, joint_markers, sample_z, x0, y0, x1, y1, piles)
    return piles


def place_all_piles(frames, template, sample_z=None):
    """PlaceAll, PilePlacer.cs:116-132."""
    if frames is None:
        raise GroundFramesError("frames_missing", "frames are required")
    out = []
    for f in frames:
        out.extend(place_piles(f, template, sample_z))
        if len(out) > MAX_PILES:
            raise GroundFramesError("piles_over_cap", f"more than {MAX_PILES} piles")
    return out


# ============================================================== LEAFPILING ==

def format_xdata_double(value):
    """FormatXDataDouble, LeafPilingCommand.cs:1413-1416: "0.########" with .NET's
    15 significant digit custom-format precision and half-away-from-zero rounding."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    q = Decimal(format(value, ".15g")).quantize(Decimal("1e-8"), rounding=ROUND_HALF_UP, context=_WIDE)
    text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def pile_xdata(pile, diameter, reveal, embed, depth_m):
    """LEAFPILING XData, LeafPilingCommand.cs:1358-1392, in order."""
    if pile["placement_mode"] == "AxisStations":
        kind, sub = "Station", str(pile["station_kind"])
    elif pile["is_joint_pile"]:
        kind, sub = "Joint", str(pile["joint_kind"])
    else:
        kind, sub = "Grid", ""

    def tagged(prefix, value):
        return "" if value is None or not str(value).strip() else prefix + str(value)

    return [(DXF_REGAPP, XDATA_PILING), (DXF_ASCII, kind), (DXF_ASCII, sub),
            (DXF_ASCII, tagged("SourceTracker=", pile["source_tracker_handle"])),
            (DXF_ASCII, tagged("Template=", pile["template_name"])),
            (DXF_ASCII, tagged("Mapping=", pile["mapping_signature"])),
            (DXF_ASCII, tagged("StationLabel=", pile["station_label"])),
            (DXF_ASCII, "DiameterM=" + format_xdata_double(diameter)),
            (DXF_ASCII, "RevealM=" + format_xdata_double(reveal)),
            (DXF_ASCII, "EmbedmentM=" + format_xdata_double(embed)),
            (DXF_ASCII, "DepthM=" + format_xdata_double(depth_m))]


def pile_entities(piles, cfg, meters_per_unit):
    """DrawPiles, LeafPilingCommand.cs:1290-1411. Each pile is a CIRCLE with thickness
    (the plugin's comment at 1353-1354: it avoids ACIS solids), centre at the pile's
    X/Y and Z minus embedment, normal +Z, extruded up by reveal plus embedment. Piles
    whose radius or depth rounds under 1e-6 drawing units are skipped."""
    mpu = meters_per_unit if meters_per_unit > 1e-12 else 1.0
    out = []
    for p in piles:
        diameter = p["pile_diameter_m"] if p["pile_diameter_m"] > 0.0 else cfg.pile_diameter_m
        radius = diameter * 0.5 / mpu
        reveal = p["pile_reveal_m"] if p["pile_reveal_m"] > 0.0 else cfg.pile_reveal_m
        embed = p["pile_embedment_m"] if p["pile_embedment_m"] > 0.0 else cfg.pile_embedment_m
        depth_m = reveal + embed
        embed_du = embed / mpu
        depth_du = depth_m / mpu
        if radius < 1e-6 or depth_du < 1e-6:
            continue
        bottom = p["z"] - embed_du
        out.append({"type": "CIRCLE", "layer": LAYER_PILING, "color": dict(BY_LAYER),
                    "center": (p["x"], p["y"], bottom), "normal": (0.0, 0.0, 1.0), "radius": radius,
                    "thickness": depth_du, "xdata": pile_xdata(p, diameter, reveal, embed, depth_m),
                    "cylinder": {"center_x": p["x"], "center_y": p["y"], "diameter_du": radius * 2.0,
                                 "bottom_z": bottom, "top_z": bottom + depth_du}})
    return out


def run_piling(entities, preset, pile_store, meters_per_unit, *, terrain_z_m=None, in_road_buffer=None,
               native_mapping=None):
    """LEAFPILING for native LEAF-TRACKERS frames, LeafPilingCommand.cs:57-441.

    preset: the active FramePreset. pile_store: a PileTemplateStore (its resolve()
    picks the native template). native_mapping: optional {"template", "signature"} for
    the saved "NATIVE|LEAF-TRACKERS|ACTIVE-PRESET" block mapping
    (ApplyNativeMapping, LeafPilingCommand.cs:1015-1026). in_road_buffer(x, y) stands
    in for CivilRoadGeometry.PointInAnyRoadBuffer; None means no recognised roads.
    Every existing entity on LEAF-PILING is erased before drawing (erase_indexes)."""
    ents = _entities(entities)
    sources = read_tracker_frames(ents)
    if not sources:
        return {"status": "no_sources", "native_frame_count": 0, "piles": [], "entities": [],
                "erase_indexes": [], "expected_total": 0}
    if preset is None or pile_store is None:
        raise GroundFramesError("preset_missing", "the active preset and the pile template store are required")
    cfg = preset.piling if preset.piling is not None else PilingConfig()
    native_template = pile_store.resolve(preset)
    mpu = _finite(meters_per_unit, "meters_per_unit_invalid", "meters_per_unit")
    if mpu <= 0.0:
        raise GroundFramesError("meters_per_unit_invalid", "metersPerUnit must be > 0.")
    sample_z = None
    if terrain_z_m is not None:
        def sample_z(x, y):
            z = _sample(terrain_z_m, x, y)
            return None if z is None else z / mpu

    pack = preset.get_tracker_pack()
    at_joints = pack is not None and pack.place_piles_at_joints and pack.has_intra_tracker_segments()
    if at_joints and not native_template.should_place_piles_at_joints:
        native_template = native_template.clone()
        native_template.should_place_piles_at_joints = True
    native_markers = pack.joint_markers_along_axis_m(preset.module_width_m) if at_joints else None

    mapped = None
    if native_mapping is not None and native_mapping.get("template") is not None:
        mapped = (native_mapping["template"].clone(), str(native_mapping.get("signature") or NATIVE_MAPPING_SIGNATURE))

    worst = native_template if mapped is None else mapped[0]
    per_frame = worst.piles_per_frame + (len(native_markers) if native_markers else 0)
    if len(sources) * max(1, per_frame) > MAX_PILES:
        raise GroundFramesError("piles_over_cap", f"about {len(sources) * per_frame} piles exceed {MAX_PILES}")

    piles = []
    expected = {}
    for src in sources:
        template = mapped[0] if mapped else native_template
        markers = None if mapped else native_markers
        placed = place_piles(src, template, None, markers, mpu)
        for p in placed:
            z = sample_z(p["x"], p["y"]) if sample_z is not None else None
            p["z"] = z if z is not None else 0.0
            p["source_tracker_handle"] = src["handle"]
            p["template_name"] = template.name or ""
            p["mapping_signature"] = mapped[1] if mapped else ""
            if template.pile_diameter_m > 0.0:
                p["pile_diameter_m"] = template.pile_diameter_m
            if template.pile_reveal_m > 0.0:
                p["pile_reveal_m"] = template.pile_reveal_m
            if template.pile_embedment_m > 0.0:
                p["pile_embedment_m"] = template.pile_embedment_m
            piles.append(p)
        if src["handle"]:
            expected[src["handle"]] = len(placed)

    road_skipped = 0
    if in_road_buffer is not None:
        eligible = {s["handle"] for s in sources if s["handle"]}
        kept = []
        for p in piles:
            clip_ok = bool(eligible) and (not p["source_tracker_handle"] or p["source_tracker_handle"] in eligible)
            if clip_ok and in_road_buffer(p["x"], p["y"]):
                continue
            kept.append(p)
        road_skipped = len(piles) - len(kept)
        piles = kept

    actual = {}
    for p in piles:
        if p["source_tracker_handle"]:
            actual[p["source_tracker_handle"]] = actual.get(p["source_tracker_handle"], 0) + 1
    short = sorted(({"handle": h, "expected": n, "actual": actual.get(h, 0),
                     "missing": max(0, n - actual.get(h, 0))}
                    for h, n in expected.items() if actual.get(h, 0) < n), key=lambda r: r["handle"].upper())
    station = sum(1 for p in piles if p["placement_mode"] == "AxisStations")
    joint = sum(1 for p in piles if p["placement_mode"] != "AxisStations" and p["is_joint_pile"])
    drawn = pile_entities(piles, cfg, mpu)
    erase = [i for i, ent in enumerate(ents) if _layer_is(ent, LAYER_PILING)]
    return {"status": "ok" if drawn else "no_piles_drawn", "native_frame_count": len(sources),
            "template_name": (mapped[0] if mapped else native_template).name, "piles": piles, "entities": drawn,
            "erase_indexes": erase, "expected_total": sum(expected.values()), "grid_piles": len(piles) - station - joint,
            "joint_piles": joint, "station_piles": station, "road_skipped": road_skipped, "short_trackers": short,
            "terrain_draped": terrain_z_m is not None,
            "layers_created": [{"name": LAYER_PILING, "aci": PILING_LAYER_ACI}], "regapps": [XDATA_PILING]}


# ====================================================== LEAFCOLLISIONRANGE ==

def _parse_depth_text(text):
    """double.TryParse(NumberStyles.Float, InvariantCulture) on the DepthM= value."""
    raw = text.strip()
    if raw in ("Infinity", "-Infinity", "NaN"):
        return float(raw.replace("Infinity", "inf").replace("NaN", "nan"))
    if _FLOAT_TEXT.match(text):
        return float(text)
    return None


def read_pile_depth_m(ent):
    """TryReadPileDepthM, LeafCollisionRangeCommand.cs:166-191: the first string item
    starting DepthM= (case-insensitive) decides; unparseable or <= 0 means no value."""
    block = xdata_for_app(ent.get("xdata"), XDATA_PILING)
    if block is None:
        return None
    for _code, value in block:
        if not isinstance(value, str) or not value.strip():
            continue
        if not value[:7].upper() == "DEPTHM=":
            continue
        depth = _parse_depth_text(value[7:])
        return depth if depth is not None and depth > 0.0 else None
    return None


def entity_extents(ent):
    """GeometricExtents stand-in: explicit `extents`, else a CIRCLE's extruded box
    (normal +Z) or an LWPOLYLINE's vertex box at its elevation. None when unknown,
    which the plugin's catch treats as "skip the entity"."""
    ext = ent.get("extents")
    if ext is not None:
        lo = [_finite(v, "extents_malformed", "extents") for v in ext[0]]
        hi = [_finite(v, "extents_malformed", "extents") for v in ext[1]]
        if len(lo) != 3 or len(hi) != 3:
            raise GroundFramesError("extents_malformed", "extents must be two 3D points")
        return tuple(lo), tuple(hi)
    kind = str(ent.get("type", "")).upper()
    if kind == "CIRCLE":
        c = ent.get("center")
        if c is None or len(c) < 2:
            raise GroundFramesError("circle_malformed", "a circle needs a center")
        cx = _finite(c[0], "circle_malformed", "center")
        cy = _finite(c[1], "circle_malformed", "center")
        cz = _finite(c[2], "circle_malformed", "center") if len(c) > 2 else 0.0
        r = _finite(ent.get("radius"), "circle_malformed", "radius")
        t = _finite(ent.get("thickness", 0.0), "circle_malformed", "thickness")
        return (cx - r, cy - r, min(cz, cz + t)), (cx + r, cy + r, max(cz, cz + t))
    if kind == "LWPOLYLINE" and ent.get("vertices"):
        pts = _points(ent["vertices"], "polyline", "polyline")
        x0, y0, x1, y1 = _bbox(pts)
        z = _finite(ent.get("elevation", 0.0), "polyline_malformed", "elevation")
        return (x0, y0, z), (x1, y1, z)
    return None


def run_pile_range_check(entities, preset, meters_per_unit):
    """LEAFCOLLISIONRANGE, LeafCollisionRangeCommand.cs:32-164. The window is the
    active preset's PilingConfig Min/MaxPileLengthM (not the pile template's). A pile's
    length is its LEAFPILING DepthM, else its Z extent times meters_per_unit."""
    ents = _entities(entities)
    cfg = preset.piling if preset is not None and preset.piling is not None else PilingConfig()
    lo, hi = cfg.min_pile_length_m, cfg.max_pile_length_m
    if hi <= 0 or hi < lo:
        return {"status": "invalid_range", "min_m": lo, "max_m": hi, "total_piles": 0, "hits": [], "markers": []}
    mpu = meters_per_unit if isinstance(meters_per_unit, (int, float)) and meters_per_unit > 1e-12 else 1.0
    hits = []
    total = 0
    for index, ent in enumerate(ents):
        if not _layer_is(ent, LAYER_PILING):
            continue
        ext = entity_extents(ent)
        if ext is None:
            continue
        (x0, y0, z0), (x1, y1, z1) = ext
        depth = read_pile_depth_m(ent)
        length = depth if depth is not None else (z1 - z0) * mpu
        total += 1
        if lo <= length <= hi:
            continue
        hits.append({"entity_index": index, "center_x": (x0 + x1) * 0.5, "center_y": (y0 + y1) * 0.5,
                     "length_m": length, "below_min": length < lo,
                     "min_x": x0, "min_y": y0, "max_x": x1, "max_y": y1})
    if total == 0:
        return {"status": "no_piles", "min_m": lo, "max_m": hi, "total_piles": 0, "hits": [], "markers": []}
    markers = [_rectangle_marker(h["min_x"], h["min_y"], h["max_x"], h["max_y"]) for h in hits]
    return {"status": "out_of_range" if hits else "all_within_range", "min_m": lo, "max_m": hi,
            "total_piles": total, "hits": hits, "markers": markers,
            "layers_created": [{"name": LAYER_COLLISION, "aci": COLLISION_LAYER_ACI}] if hits else []}
