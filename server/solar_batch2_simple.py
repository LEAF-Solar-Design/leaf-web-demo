"""Studio's G36 engines for the simple batch-2 steps: zone height, the shade reports, frame
information and deep search (contract G36, W1-IDENTITY-CONTRACT.md).

Each engine is a literal port of the plugin command it answers for (Branch2025, cited file:line)
and reads only its name-free intake. Every function is pure, bounded and fails closed on a
malformed input (BatchTwoError); nothing here touches a drawing or a store.

  z1  zone-height-settings      ZONEHEIGHT (ZoneHeightForm.cs): Add, the offset edit, Apply, Close
  s2  shadow-curtain            LEAFSHADOWCURTAIN (Pvcase/LeafShadeSimCommand.cs:253-353)
  s3  shade-loss-heatmap        LEAFSHADELOSSMAP (Commands.cs:5421-5482)
  s4  shade-loss-heatmap-clear  LEAFCLEARSHADEHEATMAP (Commands.cs:5498-5551)
  f1  frame-information         LEAFFRAMEINFO (Pvcase/FrameInformationForm.cs:155-193)
  q1  deep-search-status        LEAFDEEPSEARCH (DeepSearchStatusForm.cs:80-182)
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import math


class BatchTwoError(ValueError):
    """A malformed intake or answer; the step refuses rather than guessing."""


MAX_ZONES = 1000                 # bound on the stored zone list an intake may carry
MAX_MARKERS = 1_000_000          # bound on heatmap markers (one per panel sample)
MAX_HANDLES = 1_000_000          # bound on a zone's string or panel handle lists

# ZoneHeightForm.cs:33 the colour cycle; :34 the cursor starts at 0 every time the form opens.
ZONE_COLORS = (1, 3, 5, 4, 6, 2, 30, 210, 140, 200)
# ZoneHeightForm.Designer.cs:224-226: two decimals, 0 to 10000 (NumericUpDown's default minimum 0).
OFFSET_DECIMALS = 2
OFFSET_MIN = Decimal(0)
OFFSET_MAX = Decimal(10000)


def _require(condition, message):
    if not condition:
        raise BatchTwoError(message)


def _is_number(value):
    return type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value)


def _quantity(kind, value, unit):
    return {"kind": kind, "value": float(value), "unit": unit}


def report_rows(values):
    """G23 `report` rows, one per name, in name order: [(id, fields)]."""
    return [(f"report-{name}", {"name": name, "value": values[name]}) for name in sorted(values)]


# ---------------------------------------------------------------------------------------------
# z1: zone height.

def _zone(zone, units):
    _require(isinstance(zone, dict), "an elevation zone is not an object")
    _require(set(zone) == {"name", "offset", "colour", "strings", "panels"}, "an elevation zone has the wrong fields")
    offset = zone["offset"]
    _require(isinstance(offset, dict) and offset.get("kind") == "length" and offset.get("unit") == units
             and _is_number(offset.get("value")), "an elevation zone offset is not a length in drawing units")
    _require(type(zone["name"]) is str and type(zone["colour"]) is int, "an elevation zone name or colour is invalid")
    for key in ("strings", "panels"):
        _require(isinstance(zone[key], list) and len(zone[key]) <= MAX_HANDLES
                 and all(type(h) is str for h in zone[key]), f"an elevation zone {key} list is invalid")
    return {"name": zone["name"], "offset": _quantity("length", offset["value"], units), "colour": zone["colour"],
            "strings": list(zone["strings"]), "panels": list(zone["panels"])}


def zone_height(state, form_values):
    """ZONEHEIGHT with the G36 form actions: `add` new zones (btnAddZone_Click, ZoneHeightForm.cs:141-154: named
    "Zone {count + 1}", offset 0, colour from the cycle), the last one's offset set to `offset_in` through the
    two-decimal NumericUpDown and applied (btnApply_Click: name kept, Offset = nudOffset.Value), then Close saves
    the whole list (SaveAndClose, :356-370). Returns the zones after, in stored order."""
    _require(isinstance(state, dict), "the zone-height state is not an object")
    _require(state.get("installation_design") == "Roof", "ZONEHEIGHT is only available for Rooftop projects")
    units = state.get("units")
    _require(units in ("in", "ft", "mm", "cm", "m"), "the zone-height state has no drawing units")
    zones = state.get("elevation_zones")
    _require(isinstance(zones, list) and len(zones) <= MAX_ZONES, "the stored zone list is invalid")
    zones = [_zone(zone, units) for zone in zones]
    _require(isinstance(form_values, dict) and set(form_values) == {"add", "offset_in"}, "form_values must be add and offset_in")
    add, offset = form_values["add"], form_values["offset_in"]
    _require(type(add) is int and 1 <= add <= MAX_ZONES - len(zones), "add must be a positive zone count")
    _require(_is_number(offset), "offset_in must be a number")
    value = Decimal(repr(float(offset))).quantize(Decimal(1).scaleb(-OFFSET_DECIMALS), rounding=ROUND_HALF_UP)
    _require(OFFSET_MIN <= value <= OFFSET_MAX, "the offset is outside the form's 0 to 10000 range")
    for cursor in range(add):
        zones.append({"name": f"Zone {len(zones) + 1}", "offset": _quantity("length", 0, units),
                      "colour": ZONE_COLORS[cursor % len(ZONE_COLORS)], "strings": [], "panels": []})
    zones[-1]["offset"] = _quantity("length", float(value), units)
    return zones


def zone_rows(state, zones_after):
    """z1 rows: every zone after, in stored order, when the list changed (the plugin adapter's rule)."""
    before = [_zone(zone, state["units"]) for zone in state["elevation_zones"]]
    if before == zones_after:
        return []
    return [(f"elevation-zone-{number}", zone) for number, zone in enumerate(zones_after, 1)]


# ---------------------------------------------------------------------------------------------
# s2 to s4: the shade reports.

def shadow_curtain(markers_intake):
    """LEAFSHADOWCURTAIN's report outcome (LeafShadeSimCommand.cs:320-333): no markers -> no-heatmap-markers;
    markers but none with stored ray interceptions -> no-stored-hits with the marker count. A curtain that would
    draw (some marker has stored hits) is outside G36's report-only receipt and refuses."""
    _require(isinstance(markers_intake, dict), "the markers intake is not an object")
    markers = markers_intake.get("markers")
    _require(isinstance(markers, list) and len(markers) <= MAX_MARKERS, "the markers list is invalid")
    _require(markers_intake.get("marker_count") == len(markers), "the marker count disagrees with the markers")
    for marker in markers:
        _require(isinstance(marker, dict) and type(marker.get("has_stored_hits")) is bool,
                 "a marker has no stored-hits flag")
    if not markers:
        return report_rows({"message": "no-heatmap-markers", "heatmap-markers": 0})
    if any(marker["has_stored_hits"] for marker in markers):
        raise BatchTwoError("a marker carries stored interceptions: the curtain would draw, not report")
    return report_rows({"message": "no-stored-hits", "heatmap-markers": len(markers)})


def _shade_state(state):
    _require(isinstance(state, dict), "the shade state is not an object")
    losses = state.get("shade_loss_per_module")
    _require(isinstance(losses, dict) and all(_is_number(v) for v in losses.values()),
             "the per-module loss map is invalid")
    _require(type(state.get("shade_loss_heatmap_applied")) is bool, "the heatmap-applied flag is invalid")
    return losses


def shade_loss_heatmap(state):
    """LEAFSHADELOSSMAP (Commands.cs:5434-5439): an empty per-module loss map prints the no-data line and returns;
    a populated map would recolour panels, which G36 does not receipt."""
    if _shade_state(state):
        raise BatchTwoError("the per-module loss map is populated: the heatmap would recolour, not report")
    return report_rows({"message": "no-shading-data"})


def shade_loss_heatmap_clear(state):
    """LEAFCLEARSHADEHEATMAP (Commands.cs:5511-5516): the same no-data gate and message class."""
    if _shade_state(state):
        raise BatchTwoError("the per-module loss map is populated: the clear would revert colours, not report")
    return report_rows({"message": "no-shading-data"})


# ---------------------------------------------------------------------------------------------
# f1: frame information.

def net_format(value, decimals):
    """.NET's custom "0.#..." format with `decimals` optional digits (InvariantCulture): round the exact binary
    value half away from zero, drop trailing zeros and a bare point. Returns the text the dialog shows."""
    _require(_is_number(value), "a displayed value is not a number")
    quantum = Decimal(1).scaleb(-decimals)
    text = format(Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _shown_number(text):
    return float(text)


def _active_preset(presets):
    _require(isinstance(presets, dict) and isinstance(presets.get("Presets"), list), "the frame preset store is invalid")
    name = presets.get("ActiveName")
    matches = [p for p in presets["Presets"] if isinstance(p, dict) and p.get("Name") == name]
    # FramePresetStore.GetActive: the named preset, else the first one.
    if matches:
        return matches[0]
    _require(presets["Presets"] and isinstance(presets["Presets"][0], dict), "the frame preset store has no presets")
    return presets["Presets"][0]


def frame_information(intake):
    """LEAFFRAMEINFO's dialog fields in display order (FrameInformationForm.LoadFields, :155-193), each as the text
    the form writes, typed the way the plugin adapter types it: [(group, field, value)]."""
    _require(isinstance(intake, dict), "the frame intake is not an object")
    preset = _active_preset(intake.get("frame_presets"))
    park = intake.get("park_params")
    totals = intake.get("drawing_totals")
    _require(isinstance(park, dict) and isinstance(totals, dict), "the frame intake lacks park params or totals")
    for key in ("ModuleLengthM", "ModuleWidthM", "ModuleThicknessM", "ModulePowerWp", "TiltDegrees",
                "HorizontalGapM", "VerticalGapM"):
        _require(_is_number(preset.get(key)), f"the active preset lacks {key}")
    _require(type(preset.get("Rows")) is int and type(preset.get("Columns")) is int, "the active preset rows or columns")
    for key in ("ShadingLimitAngleDeg", "FlatPitchM", "MinPitchM", "MaxPitchM", "ColumnSpacingM", "AzimuthDeg"):
        _require(_is_number(park.get(key)), f"the park params lack {key}")

    def length(value, decimals):
        return _quantity("length", _shown_number(net_format(value, decimals)), "m")

    def angle(value, decimals):
        return _quantity("angle", _shown_number(net_format(value, decimals)), "deg")

    frame_power = preset["Rows"] * preset["Columns"] * preset["ModulePowerWp"] / 1000.0   # FramePreset.cs:100
    orientation = "Landscape" if str(park.get("FrameOrientation", "")).lower() == "landscape" else "Portrait"  # :182-183
    frame_types = totals.get("frame_types")
    _require(isinstance(frame_types, list) and all(type(t) is str for t in frame_types), "the frame types are invalid")
    _require(type(totals.get("frames")) is int and type(totals.get("piles")) is int and _is_number(totals.get("kwp_total")),
             "the drawing totals are invalid")
    return [
        ("active", "preset_name", str(preset.get("Name", ""))),
        ("module", "module_length", length(preset["ModuleLengthM"], 3)),
        ("module", "module_width", length(preset["ModuleWidthM"], 3)),
        ("module", "module_thickness", length(preset["ModuleThicknessM"], 3)),
        ("module", "module_power_wp", float(preset["ModulePowerWp"])),        # ToString(Invariant): all digits
        ("frame", "framing_type", str(preset.get("FramingType", ""))),
        ("frame", "orientation", str(preset.get("Orientation", ""))),
        ("frame", "rows", preset["Rows"]),
        ("frame", "columns", preset["Columns"]),
        ("frame", "tilt", angle(preset["TiltDegrees"], 2)),
        ("frame", "horizontal_gap", length(preset["HorizontalGapM"], 3)),
        ("frame", "vertical_gap", length(preset["VerticalGapM"], 3)),
        ("frame", "frame_power_kwp", _shown_number(net_format(frame_power, 2))),
        ("park", "shading_limit", angle(park["ShadingLimitAngleDeg"], 2)),
        ("park", "flat_pitch", length(park["FlatPitchM"], 2)),
        ("park", "min_pitch", length(park["MinPitchM"], 2)),
        ("park", "max_pitch", length(park["MaxPitchM"], 2)),
        ("park", "column_spacing", length(park["ColumnSpacingM"], 3)),
        ("park", "azimuth", angle(park["AzimuthDeg"], 2)),
        ("park", "frame_orientation", orientation),
        ("drawing", "frames", totals["frames"]),
        ("drawing", "kwp_total", _shown_number(net_format(totals["kwp_total"], 2))),
        ("drawing", "piles", totals["piles"]),
        ("drawing", "frame_types", "-" if not frame_types else "\n".join(frame_types)),   # :285-288
    ]


def frame_rows(intake):
    return [(f"frame-info-{number}", {"group": group, "field": field, "value": value})
            for number, (group, field, value) in enumerate(frame_information(intake), 1)]



# ---------------------------------------------------------------------------------------------
# f2: frame and park settings (LEAFFRAME, LeafFrameCommand.cs:15-31, FrameParkSettingsForm.cs).

FRAME_FORM_KEYS = {"ok", "cancel"}
MAX_PACK_SEGMENTS = 10_000


def tracker_pack_modules(preset):
    """TrackerPack.ModuleCountTotal (TrackerPack.cs:57-59) of the preset as stored: the Modules segments' counts.
    A preset without a pack takes DefaultUniform(Rows x Columns) (FramePreset.cs:76-89, TrackerPack.cs:219-228)."""
    target = frame_target(preset)
    pack = preset.get("TrackerPack")
    if pack is None:
        return target
    _require(isinstance(pack, dict), "a tracker pack is not an object")
    segments = pack.get("Segments")
    if segments is None or segments == []:
        return target
    _require(isinstance(segments, list) and len(segments) <= MAX_PACK_SEGMENTS, "the tracker pack segments are invalid")
    total = 0
    for segment in segments:
        if segment is None:
            continue
        _require(isinstance(segment, dict), "a tracker pack segment is not an object")
        count = segment.get("Count", 0)
        _require(type(count) is int, "a tracker pack segment count is not an integer")
        if segment.get("Kind") == "Modules":
            total += max(0, count)
    return total


def frame_target(preset):
    """FramePreset.TargetModuleCount (FramePreset.cs:103-113): Rows x Columns, each floored at zero."""
    rows, columns = preset.get("Rows", 0), preset.get("Columns", 0)
    _require(type(rows) is int and type(columns) is int, "a preset's Rows and Columns are not integers")
    return min(max(0, rows) * max(0, columns), 2**31 - 1)


def frame_park_settings(intake, form_values):
    """LEAFFRAME with the G36 actions: OK on the active preset, then Cancel if OK was refused. OnOkClicked
    (FrameParkSettingsForm.cs:605-622): a fixed-tilt preset skips the tracker field check; the pack check
    (ValidateTrackerPack :762-772) compares the pack's module total with Rows x Columns; on a pass the working
    preset is saved and made active (unchanged here, so the stores do not change). Studio reads the preset store as
    stored. Returns the `report` rows in the plugin adapter's shape."""
    _require(isinstance(intake, dict), "the frame intake is not an object")
    _require(isinstance(form_values, dict) and set(form_values) == FRAME_FORM_KEYS, "form_values must be ok and cancel")
    preset = _active_preset(intake.get("frame_presets"))
    _require(preset.get("FramingType") != "SingleAxisTracker",
             "a tracker preset's field validation is not carried by this intake version")
    modules, target = tracker_pack_modules(preset), frame_target(preset)
    values = {"store-changed": False}
    if modules == target:
        values.update({"pack-check": "passed", "committed": True})
    else:
        values.update({"pack-check": "failed", "pack-modules": modules, "pack-target": target, "committed": False})
    values["active-preset"] = preset.get("Name")
    return report_rows(values)


# ---------------------------------------------------------------------------------------------
# q1: deep search status.

def deep_search_status(sessions):
    """LEAFDEEPSEARCH's dialog (DeepSearchStatusForm.cs:150-182): with no sessions it shows the empty-state
    message. Studio keeps no deep-search sessions for a drawing that has run no solve in Studio, so `sessions`
    is empty here; a non-empty list is outside G36's captured state and refuses."""
    _require(isinstance(sessions, list), "the deep search sessions are not a list")
    if sessions:
        raise BatchTwoError("sessions are listed; G36 receipts only the empty state")
    return report_rows({"message": "no-sessions", "sessions": 0})
