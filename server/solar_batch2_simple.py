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

import re

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
# z2, z3: elevation zone assignment (Commands.cs LEAFZONEASSIGNPANELHEIGHT :3748-3916, LEAFZONEASSIGNSTRINGS
# :3921-4033), both reached from the palette's height zone (ZonesStepPanel.ElevAssignPanels_Click :1355-1374).

MAX_PANELS = 1_000_000
_HANDLE = re.compile(r"[0-9A-F]{1,16}")


def _assign_intake(intake):
    _require(isinstance(intake, dict) and intake.get("format") == "zone-assign-intake-v1",
             "the intake is not a zone assign intake")
    units = intake.get("units")
    _require(units in ("in", "ft", "mm", "cm", "m"), "the zone assign intake has no drawing units")
    zones = intake.get("elevation_zones")
    _require(isinstance(zones, list) and len(zones) <= MAX_ZONES, "the stored zone list is invalid")
    panels = intake.get("panels")
    _require(isinstance(panels, list) and len(panels) <= MAX_PANELS, "the intake panels are invalid")
    for panel in panels:
        _require(isinstance(panel, dict) and set(panel) == {"panel", "size", "colour"}
                 and type(panel["panel"]) is str and _HANDLE.fullmatch(panel["panel"])
                 and (panel["size"] is None or type(panel["size"]) is str) and type(panel["colour"]) is int,
                 "an intake panel is invalid")
    strings = intake.get("strings")
    _require(isinstance(strings, list) and len(strings) <= MAX_PANELS
             and all(type(h) is str and _HANDLE.fullmatch(h) for h in strings), "the intake strings are invalid")
    return units, [_zone(zone, units) for zone in zones], panels, strings


def _zone_named(zones, name):
    """zones.FirstOrDefault(z => z.Name equals name, OrdinalIgnoreCase) (Commands.cs:3772-3773)."""
    for zone in zones:
        if zone["name"].upper() == name.upper():
            return zone
    return None


def _move_into(zones, zone, key, handle):
    """Remove the handle from every zone's list, then add it to the target once (Commands.cs:3866-3875, :3998-4006)."""
    for other in zones:
        while handle in other[key]:
            other[key].remove(handle)
    if handle not in zone[key]:
        zone[key].append(handle)


def zone_assign_panels(intake, zones, height_zone, reference):
    """LEAFZONEASSIGNPANELHEIGHT with the reference panel picked by handle (its layer sets the "*layer*" filter and
    its rectangle size the signature, Commands.cs:3789-3811, EntitySignature, HandleResolver.cs:24-83) and ALL:
    every panel of the intake in the selection's order (descending handle), the signature-matching ones moved into
    the zone and given its colour (:3848-3877). Returns (zones after, {panel: colour} of the recoloured, skipped)."""
    units, _, panels, _ = _assign_intake(intake)
    zones = [_zone(zone, units) for zone in zones]
    zone = _zone_named(zones, height_zone)
    _require(zone is not None, "the pending height zone is not in the drawing")
    by_handle = {panel["panel"]: panel for panel in panels}
    _require(reference in by_handle, "the reference panel is not one of the intake panels")
    signature = by_handle[reference]["size"]
    recoloured, skipped = {}, 0
    for panel in panels:
        if signature is not None and panel["size"] != signature:
            skipped += 1
            continue
        _move_into(zones, zone, "panels", panel["panel"])
        if panel["colour"] != zone["colour"]:
            recoloured[panel["panel"]] = zone["colour"]
    return zones, recoloured, skipped


def zone_assign_strings(intake, zones, height_zone):
    """LEAFZONEASSIGNSTRINGS with ALL: every String-layer polyline with a string record (the intake's strings, the
    selection's descending-handle order) moved into the pending zone (Commands.cs:3979-4007)."""
    units, _, _, strings = _assign_intake(intake)
    zones = [_zone(zone, units) for zone in zones]
    zone = _zone_named(zones, height_zone)
    _require(zone is not None, "the pending height zone is not in the drawing")
    for handle in strings:
        _move_into(zones, zone, "strings", handle)
    return zones


def zone_assign_rows(zones_before, zones_after, recoloured=None):
    """z2 and z3 rows (the plugin adapter's rule): every zone after when the list changed, then one `recoloured`
    row per panel whose colour changed, in selection order."""
    rows = {}
    if zones_before != zones_after:
        rows["elevation-zone"] = [(f"elevation-zone-{n}", zone) for n, zone in enumerate(zones_after, 1)]
    if recoloured:
        rows["recoloured"] = [(f"recoloured-{n}", {"panel": handle, "colour": colour})
                              for n, (handle, colour) in enumerate(recoloured.items(), 1)]
    return rows


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


# ---------------------------------------------------------------------------------------------
# k1: string sizer (StringSizer): Studio's pinned sizing client over the service's response.

SIZER_SETTINGS = ("PanelsInSequence", "VocColdPasses", "VocColdOverrideAccepted", "VocColdSuggestedStringLength",
                  "VocColdPerModule", "VocColdStringVoltage", "VocColdMaxDcVoltage")
MAX_SIZER_BODY = 256 * 1024


def string_sizer_outcome(intake, response, form=None):
    """What Studio commits for one string sizing: the request must be the pinned plugin request (SizingRequest), the
    response body is parsed exactly as solar_sizing_client.size parses it (strict JSON, duplicate keys refused) and
    validated as the plugin's result object. A body that is valid JSON but not an object (the service's
    double-encoded 200) is refused the way the plugin's Newtonsoft conversion refuses it, and nothing is committed.
    Returns (report values, {setting: value})."""
    import json as _json
    import os as _os
    import sys as _sys
    _here = _os.path.dirname(_os.path.abspath(__file__))
    if _here not in _sys.path:          # the evidence producer loads this module by path
        _sys.path.insert(0, _here)
    import solar_sizing_client as sizing

    _require(isinstance(intake, dict) and isinstance(intake.get("request"), dict), "the sizer intake has no request")
    settings = intake.get("settings")
    _require(isinstance(settings, dict) and set(settings) == set(SIZER_SETTINGS), "the sizer intake settings are invalid")
    try:
        sizing.SizingRequest.model_validate(intake["request"])
    except (ValueError, TypeError):
        raise BatchTwoError("the sizer request is not the plugin's request shape") from None
    _require(isinstance(response, dict) and response.get("status") == 200 and isinstance(response.get("body_text"), str),
             "the recorded sizer response is invalid")
    body = response["body_text"]
    _require(len(body.encode("utf-8")) <= MAX_SIZER_BODY, "the recorded sizer response is too large")

    def unique(items):
        result = {}
        for key, value in items:
            _require(key not in result, "the sizer response repeats a key")
            result[key] = value
        return result

    try:
        value = _json.loads(body, object_pairs_hook=unique)
    except ValueError:
        return {"calculation": "failed", "error": "other"}, {}
    if not isinstance(value, dict):
        return {"calculation": "failed", "error": "response-not-an-object"}, {}
    if form is None:
        try:
            sizing.validate_response(value)
        except sizing.CloudError:
            # The result form also accepts the captured response without the client's min_temp.
            if not (isinstance(value.get("simulation_results"), dict) and value["simulation_results"]
                    and all(_is_number(value.get(k)) for k in ("voc", "bvoc"))):
                return {"calculation": "failed", "error": "other"}, {}
        raise BatchTwoError("the result-form port needs explicit form choices")
    simulations = value.get("simulation_results")
    if not isinstance(simulations, dict) or not all(_is_number(value.get(k)) for k in ("voc", "bvoc")):
        return {"calculation": "failed", "error": "other"}, {}
    _require(isinstance(form, dict), "the result form is not an object")
    standard = form.get("design_standard")
    _require(type(standard) is str and standard in simulations, "the result form needs a design standard")
    resolution = form.get("voc_cold_resolution")
    _require(resolution in (None, "pick-shorter", "override"), "the Voc_cold resolution is invalid")
    sim = simulations[standard]
    _require(isinstance(sim, dict), "the selected simulation is not an object")
    n0 = sim.get("string_length")
    _require(_is_number(n0) and n0 > 0 and float(n0).is_integer(), "the string length must be a positive integer")
    vmax = sim.get("string_design_voltage")
    _require(_is_number(vmax) and float(vmax).is_integer(), "the design voltage must be an integer")
    voc, bvoc = float(value["voc"]), float(value["bvoc"])
    # R28: today's response omits mintemp; FunctionResults defaults that field to zero.
    tmin = value.get("mintemp", 0.0)
    _require(_is_number(tmin), "mintemp must be a finite number")
    vmax = float(vmax)

    def evaluate(n):
        if voc <= 0 or vmax <= 0:
            return True, 0, 0.0, 0.0, 0.0, False
        cold = voc * (1.0 + bvoc / 100.0 * (float(tmin) - 25.0))
        voltage = cold * n
        passes = voltage <= vmax
        suggested = max(1, math.floor(vmax / cold)) if not passes and cold > 0 else 0
        return passes, suggested, cold, voltage, vmax, True

    n = int(n0)
    final = evaluate(n)
    override = False
    if not final[0]:
        _require(resolution is not None, "the result form blocks Close until Voc_cold is resolved")
        if resolution == "pick-shorter":
            n = final[1]
            final = evaluate(n)
        else:
            override = True
    passes, suggested, cold, voltage, max_dc, complete = final
    committed = {"PanelsInSequence": n, "VocColdPasses": passes if complete else None,
                 "VocColdOverrideAccepted": override, "VocColdSuggestedStringLength": suggested,
                 "VocColdPerModule": cold, "VocColdStringVoltage": voltage, "VocColdMaxDcVoltage": max_dc}
    return {"calculation": "succeeded"}, {name: v for name, v in committed.items() if settings[name] != v}


def string_sizer_rows(intake, response, form=None):
    """k1 rows in the plugin adapter's shape: the calculation report and one `setting` row per changed setting."""
    report, changed = string_sizer_outcome(intake, response, form)
    rows = {"report": report_rows(report)}
    if changed:
        rows["setting"] = [(f"setting-{name}", {"name": name, "value": changed[name]}) for name in sorted(changed)]
    return rows


# ---------------------------------------------------------------------------------------------
# m1: string midpoint connection (LEAFSTRINGMID, Commands.cs:4642-4865, Core/StringMidpointPlacer.cs).

MAX_PANELS = 200_000
MID_LABEL = "MID"                    # Commands.cs:4830
MID_MIN_TRAVERSAL = 3                # StringMidpointPlacer.MinimumTraversalLength
MID_BAND_DIAGONALS = 6.0             # the crossing band pad (Commands.cs:4731-4735)
MID_ADJACENCY_DIAGONALS = 1.5        # the adjacency threshold (:4722-4724)
MID_LABEL_HEIGHT_DIAGONALS = 0.4     # the label height (:4826)
MID_DECIMALS = 6


def _mid_round(value):
    return round(float(value), MID_DECIMALS)


def _mid_panels(intake):
    """{handle: (x, y)}, (hx, hy), diagonal from a panel intake; fails closed on anything malformed."""
    _require(isinstance(intake, dict), "the panel intake is not an object")
    panels = intake.get("panels")
    _require(isinstance(panels, list) and 0 < len(panels) <= MAX_PANELS, "the panel list is invalid")
    half = intake.get("half_extents")
    _require(isinstance(half, list) and len(half) == 2 and all(_is_number(v) and v > 0 for v in half),
             "the panel half extents are invalid")
    diagonal = intake.get("diagonal")
    _require(_is_number(diagonal) and diagonal > 0, "the panel diagonal is invalid")
    out = {}
    for panel in panels:
        _require(isinstance(panel, dict) and isinstance(panel.get("handle"), str) and panel["handle"],
                 "a panel has no handle")
        _require(_is_number(panel.get("x")) and _is_number(panel.get("y")), "a panel has no centroid")
        _require(panel["handle"].upper() not in out, "a panel handle repeats")
        out[panel["handle"].upper()] = (float(panel["x"]), float(panel["y"]))
    return out, (float(half[0]), float(half[1])), float(diagonal)


def _is_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def string_midpoint_path(intake, start, end):
    """The panels LEAFSTRINGMID strings between two picked panels: the crossing-window band around the picks (padded
    six diagonals; a panel is a candidate when its extents touch the band), proximity adjacency (centroids within 1.5
    diagonals, inclusive), and a breadth-first shortest path. The plugin takes neighbours in AutoCAD selection
    order, which a drawing does not record, so a tie between equal shortest paths is broken by handle order here;
    the capture's endpoints have one shortest path. Returns (path, label index); refuses what the command refuses."""
    panels, (hx, hy), diagonal = _mid_panels(intake)
    start, end = str(start).upper(), str(end).upper()
    _require(start in panels and end in panels, "an endpoint is not a panel")
    _require(start != end, "the endpoints are the same panel")
    (ax, ay), (bx, by) = panels[start], panels[end]
    pad = diagonal * MID_BAND_DIAGONALS
    min_x, max_x = min(ax, bx) - pad, max(ax, bx) + pad
    min_y, max_y = min(ay, by) - pad, max(ay, by) + pad
    band = sorted(h for h, (x, y) in panels.items()
                  if x + hx >= min_x and x - hx <= max_x and y + hy >= min_y and y - hy <= max_y)
    threshold_sq = (diagonal * MID_ADJACENCY_DIAGONALS) ** 2
    cell = diagonal * MID_ADJACENCY_DIAGONALS
    buckets = {}
    for handle in band:
        x, y = panels[handle]
        buckets.setdefault((math.floor(x / cell), math.floor(y / cell)), []).append(handle)

    def neighbours(handle):
        x, y = panels[handle]
        gx, gy = math.floor(x / cell), math.floor(y / cell)
        found = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in buckets.get((gx + dx, gy + dy), ()):
                    ox, oy = panels[other]
                    if other != handle and (x - ox) ** 2 + (y - oy) ** 2 <= threshold_sq:
                        found.append(other)
        return sorted(found)

    previous, seen, queue, head = {}, {start}, [start], 0
    while head < len(queue):
        current = queue[head]
        head += 1
        if current == end:
            break
        for other in neighbours(current):
            if other not in seen:
                seen.add(other)
                previous[other] = current
                queue.append(other)
    _require(end in seen, "no adjacency path between the endpoints")
    path = [end]
    while path[-1] in previous:
        path.append(previous[path[-1]])
    path.reverse()
    _require(len(path) >= MID_MIN_TRAVERSAL, "the path is too short for a midpoint string")
    return path, len(path) // 2


def string_midpoint_rows(intake, start, end):
    """LEAFSTRINGMID's one `mid-string` row in the plugin adapter's shape: the path's panels, the polyline through
    their centroids, the label index, text, position (the label panel's centroid) and height (0.4 diagonals), both
    on the drawing's string layer."""
    panels, _, diagonal = _mid_panels(intake)
    path, label_index = string_midpoint_path(intake, start, end)
    vertices = [[_mid_round(panels[h][0]), _mid_round(panels[h][1])] for h in path]
    fields = {"panels": path, "vertices": vertices, "label_index": label_index, "label_text": MID_LABEL,
              "label_position": list(vertices[label_index]),
              "label_height": _mid_round(diagonal * MID_LABEL_HEIGHT_DIAGONALS), "on_string_layer": True}
    return [("mid-string-1", fields)]


# ---------------------------------------------------------------------------------------------
# o1: open a customer drawing (LEAFOPENCUSTOMERDWG, Commands.cs:2677-2745, CustomerDwgWelcomeService.cs,
# CustomerDwgStartupAnalyzer.cs).

CUSTOMER_SCAN_KEYS = frozenset({"pvcase_area_entities", "pvcase_tracker_blocks", "pvcase_xdata_blocks",
                                "branch_tracker_polylines"})
CUSTOMER_SETTING_KEYS = ("ProjectName", "ProjectZipCode", "InstallationDesign", "LeafProjectCanceled",
                         "CustomerWelcomeDismissed")
GROUND, ROOF = "Ground", "Roof"      # AppConstants.Ground, AppConstants.Roof


def suggest_project_name(file_name):
    """SuggestProjectName (:94-104): the file name without its extension, underscores and dashes as spaces, each
    word lower-cased then title-cased; "Customer DWG" when nothing is left."""
    stem = str(file_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    words = stem.replace("_", " ").replace("-", " ").split()
    return " ".join(word.lower()[:1].upper() + word.lower()[1:] for word in words) or "Customer DWG"


def infer_flow(file_name, scan):
    """InferFlow (:398-427): PVcase evidence, then Branch tracker evidence or a ground-mount-physical file name, then
    the ground-mount and SolarEdge file names, else rooftop."""
    name = str(file_name or "").replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if scan["pvcase_area_entities"] or scan["pvcase_tracker_blocks"] or scan["pvcase_xdata_blocks"]:
        return "PvcaseParity"
    if scan["branch_tracker_polylines"] or "groundmountphysical" in name:
        return "GroundMountPhysical"
    if "groundmount" in name:
        return "GroundMount"
    if "solaredge" in name:
        return "SolarEdge"
    return "Rooftop"


def open_customer_dwg(intake):
    """The settings LEAFOPENCUSTOMERDWG writes to the drawing it opens when the welcome shows
    (ShowForDocumentIfNeeded, ApplyInferredProjectDefaults with canceled and dismissed false): a blank project name
    becomes the suggested one, a ground flow sets the installation design to ground and a rooftop flow fills a blank
    one with roof, and the project is marked not canceled. Nothing is written when the drawing was canceled or
    dismissed before, or when the file sits under a tutorial folder. Returns {name: value} of the changed settings."""
    _require(isinstance(intake, dict), "the customer intake is not an object")
    file_name = intake.get("file_name")
    _require(isinstance(file_name, str) and file_name.strip(), "the customer intake has no file name")
    _require(intake.get("opened_via_command") is True, "only the command's own open is carried")
    settings = intake.get("settings")
    _require(isinstance(settings, dict) and set(settings) == set(CUSTOMER_SETTING_KEYS),
             "the customer intake settings are invalid")
    scan = intake.get("scan")
    _require(isinstance(scan, dict) and set(scan) == CUSTOMER_SCAN_KEYS
             and all(type(v) is int and v >= 0 for v in scan.values()), "the customer intake scan is invalid")
    if settings["LeafProjectCanceled"] is True or settings["CustomerWelcomeDismissed"] is True:
        return {}
    if "\\res\\drawings\\" in file_name.replace("/", "\\").casefold():
        return {}
    after = dict(settings)
    if not str(settings["ProjectName"] or "").strip():
        after["ProjectName"] = suggest_project_name(file_name)
    flow = infer_flow(file_name, scan)
    if flow in ("GroundMount", "GroundMountPhysical", "PvcaseParity"):
        after["InstallationDesign"] = GROUND
    elif flow == "Rooftop" and not str(settings["InstallationDesign"] or "").strip():
        after["InstallationDesign"] = ROOF
    after["LeafProjectCanceled"] = False
    return {name: after[name] for name in CUSTOMER_SETTING_KEYS if after[name] != settings[name]}


def open_customer_rows(intake):
    """One `setting` row per changed setting, by name, in the plugin adapter's shape."""
    changed = open_customer_dwg(intake)
    return [(f"setting-{name}", {"name": name, "value": changed[name]}) for name in sorted(changed)]


# ---------------------------------------------------------------------------------------------
# p1: the Leaf Platform palette (LEAFPLATFORM, WebBridge/LeafPlatformWebViewHost.cs:48-57).

PLATFORM_INTAKE_KEYS = frozenset({"origin_overridden", "bound"})


def platform_webview_rows(intake):
    """What Studio commits when the hosted authoring surface is opened for a drawing: nothing. Studio is that surface,
    so there is no host palette to open, and which project and drawing a session works on is Studio-side state, never
    a record written into the drawing. Fails closed on an intake that is not the two facts the plugin adapter records
    (whether the platform origin was overridden, whether the drawing already carries a platform binding)."""
    _require(isinstance(intake, dict) and set(intake) == PLATFORM_INTAKE_KEYS
             and all(type(value) is bool for value in intake.values()), "the platform intake is invalid")
    return {}
