"""Studio's guardrails: a literal port of the plugin's design guardrail engine (contract G36).

Sources (Branch2025, cited file:line): the snapshot the engine validates is built as
Guardrails/UI/DesignSnapshotCollector.cs builds it (:22-253), the inverter type from global settings
as InverterTypeConfigFactory.FromGlobalSettings does, the 14 rules under
LeafSolarDesign.Core/Guardrails/Rules, registered and run in GuardrailEngine.RegisterAllRules order
(GuardrailEngine.cs:32-60), ordered for display by OrderForDisplay (:157-199), and summarised the way
the palette's health banner prints it (HealthBanner.cs:80-125).

Two list modes. The historical plugin capture validates only its L1 inverter list. Document activation rebuilds the lists from
the drawing (DocumentEventHandler.GetAllInverters, :233-276), and AddInverter files every L2 device in a separate
collector list (:305-313) that the snapshot collector never reads (DesignSnapshotCollector.cs:166-189):
  plugin   the L1 list as the plugin builds it. This intake version carries it only when it is empty (every device
           is L2), which reproduces the capture and pins the port.
  drawing  every inverter the drawing's string assignments name, L1 or L2, in number order. Studio validates the
           design it actually holds, so this is the mode its evidence uses; the difference is the declared
           divergence of G36. When the intake records device numbers (`device_numbers_recorded`, R34), it is
           instead one summary per device in intake order, linked to strings by number as the plugin does
           (DesignSnapshotCollector.BuildInverterSummaries and GetDistinctInverters; Inverter.FindConnectedCables,
           Inverter.cs:361), so InverterCount is the device count.

After Branch2025 #311 the collector reads both levels. The clean-host R31b capture
still reports the empty-design verdicts; this historical `plugin` mode is not a
model of that new collector. Drawing verdicts remain backed by the intake's strings
and catalog. See docs/parity/divergences/guardrails-monitoring/clean-host-empty-snapshot.md
for the branch trace and the unresolved runtime collection cause.

Pure and bounded: every function reads its inputs, allocates at most O(strings), and raises
GuardrailError on a malformed intake. Nothing here reads a database; the catalog row arrives in the
intake.
"""
from __future__ import annotations

import math

PASS, INFO, WARNING, ERROR, CRITICAL = 0, 1, 2, 3, 4          # GuardrailSeverity.cs order
SEVERITY_NAMES = ("pass", "info", "warning", "error", "critical")
DISPLAY_CATEGORY_ORDER = ("Electrical", "Hardware", "Design", "Code Safety")   # GuardrailEngine.cs:142-143
SECTION_NAMES = {"Electrical": "electrical", "Hardware": "hardware", "Design": "design",
                 "Code Safety": "code-safety"}
MAX_STRINGS = 200_000
MAX_DEVICES = 100_000
STANDARD_OCPD_RATINGS = (15, 20, 25, 30, 35, 40, 45, 50, 60)   # OvercurrentProtectionRule.cs
NEC_CONTINUOUS_FACTOR = 1.25                                   # CurrentSafetyFactorRule.cs
DC_AC_MIN, DC_AC_MAX = 1.0, 1.55                               # DcAcRatioRule.cs
MPPT_IMBALANCE_PCT = 10.0                                      # MpptBalanceRule.cs
DEFAULT_MIN_TEMP_C, DEFAULT_MAX_TEMP_C = -40.0, 65.0           # DesignTemperatureLookup.cs:45-46


class GuardrailError(ValueError):
    """A malformed guardrails intake; validation refuses rather than guessing."""


def _require(condition, message):
    if not condition:
        raise GuardrailError(message)


def _num(value, default=0.0):
    """Settings.Default numeric values: a missing or non-numeric value reads as the type default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return float(value)


def _int(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0
    return int(value)


# ---------------------------------------------------------------------------------------------
# The snapshot (DesignSnapshotCollector.Collect).

def min_temp_c(latitude, longitude):
    """DesignTemperatureLookup.GetMinTempC(lat, lon) (DesignTemperatureLookup.cs:87-123): a coarse band by |lat|."""
    if latitude == 0.0 and longitude == 0.0:
        return None
    if not (-90.0 <= latitude <= 90.0):                      # only the latitude is range-checked (:96-99)
        return None
    lat = abs(latitude)
    for bound, value in ((60.0, -40.0), (50.0, -32.0), (45.0, -25.0), (40.0, -18.0), (35.0, -10.0),
                         (30.0, -5.0), (23.5, 0.0)):
        if lat >= bound:
            return value
    return 5.0


def mppt_letter(circuit):
    """BuildInverterSummaries (DesignSnapshotCollector.cs:217-223): the circuit's last character when it is a..z, else 'a'."""
    if circuit and "a" <= circuit[-1] <= "z":
        return circuit[-1]
    return "a"


def inverter_type_from_settings(host, catalog):
    """InverterTypeConfigFactory.FromGlobalSettings: type 'A' from NumMppt, StringsPerMppt and InverterSelection,
    hydrated from the catalog row when the database has one (then SuggestedCount carries over)."""
    mppts = _int(host.get("NumMppt"))
    per_mppt = _int(host.get("StringsPerMppt"))
    suggested = _int(host.get("SuggestedInverterCount"))
    if catalog:
        config = {"model_name": catalog.get("model_name"), "num_mppt_trackers": _int(catalog.get("num_mppt_trackers")),
                  "total_dc_inputs": _int(catalog.get("total_dc_inputs")),
                  "max_dc_voltage": _num(catalog.get("max_dc_voltage")),
                  "mppt_voltage_range_min": _num(catalog.get("mppt_voltage_range_min")),
                  "mppt_voltage_range_max": _num(catalog.get("mppt_voltage_range_max")),
                  "max_ac_power_kw": _num(catalog.get("max_ac_power_kw")),
                  "is_solar_edge": bool(catalog.get("is_solar_edge")), "suggested_count": suggested}
    else:
        config = {"model_name": host.get("InverterSelection"), "num_mppt_trackers": mppts,
                  "total_dc_inputs": mppts * per_mppt, "max_dc_voltage": 0.0, "mppt_voltage_range_min": 0.0,
                  "mppt_voltage_range_max": 0.0, "max_ac_power_kw": 0.0, "is_solar_edge": False,
                  "suggested_count": suggested}
    return config


def dc_inputs_per_mppt(config):
    """InverterTypeConfig.DcInputsPerMppt: ceil(total / trackers), or total when there are no trackers."""
    trackers, total = config["num_mppt_trackers"], config["total_dc_inputs"]
    return int(math.ceil(total / trackers)) if trackers > 0 else total


def build_snapshot(intake, mode):
    """The DesignSnapshot for `intake` (a g0-intake document) with the inverter lists in `mode`."""
    _require(mode in ("plugin", "drawing"), "mode must be plugin or drawing")
    _require(isinstance(intake, dict), "the guardrails intake is not an object")
    _require(intake.get("host_settings_recorded") is True, "the intake's host settings were not recorded")
    host = intake.get("host_settings")
    drawing = intake.get("drawing")
    _require(isinstance(host, dict) and isinstance(drawing, dict), "the intake lacks host settings or drawing facts")
    strings = intake.get("strings")
    devices = intake.get("devices")
    _require(isinstance(strings, list) and len(strings) <= MAX_STRINGS, "the intake strings are invalid")
    _require(isinstance(devices, list) and len(devices) <= MAX_DEVICES, "the intake devices are invalid")
    snap = {
        "module_voc": _num(host.get("Voc")), "module_bvoc_pct": _num(host.get("BVoc")),
        "module_pmax": _num(host.get("Pmp")), "module_vmp": _num(host.get("Vmp")),
        "module_isc": _num(host.get("Imp")),                   # :36-39, Imp stands in for Isc
        "panels_in_sequence": _int(host.get("NumPanelsInSequence")),
        "num_mppt": _int(host.get("NumMppt")), "dc_inputs_per_mppt": _int(host.get("StringsPerMppt")),
        "optimizer": None, "inverter_types": {}, "strings": [], "summaries": [], "synthetic": set(),
        "cable_dictionary_null": False, "inverter_list_null": False, "tag_dictionary_null": False,
    }
    types = drawing.get("inverter_types") or {}
    _require(isinstance(types, dict), "the drawing inverter types are invalid")
    if types:
        raise GuardrailError("drawing inverter types are not carried by this intake version")
    snap["inverter_types"]["A"] = inverter_type_from_settings(host, intake.get("inverter_catalog"))   # :136-141
    lat, lon = _num(drawing.get("project_latitude")), _num(drawing.get("project_longitude"))
    looked_up = min_temp_c(lat, lon) if (lat != 0.0 or lon != 0.0) else None
    if looked_up is None and drawing.get("project_zip_code_set"):
        raise GuardrailError("a zip-code temperature lookup is not carried by this intake version")
    if looked_up is None:
        snap["min_temp_c"] = DEFAULT_MIN_TEMP_C
        snap["synthetic"].add("DesignMinTempC")
    else:
        snap["min_temp_c"] = looked_up
    snap["max_temp_c"] = DEFAULT_MAX_TEMP_C
    for key, config in snap["inverter_types"].items():                                               # :143-163
        if config["is_solar_edge"]:
            _require(not host.get("OptimizerModel"), "SolarEdge optimizer data is not carried by this intake version")
            break
    numbers_recorded = intake.get("device_numbers_recorded", False)
    _require(isinstance(numbers_recorded, bool), "the intake's device_numbers_recorded flag is invalid")
    if mode == "drawing":
        use_combiner = bool(host.get("UseCombinerBox"))
        by_inverter = {}
        for item in strings:
            _require(isinstance(item, dict), "an intake string is not an object")
            number = item.get("inverter")
            if not isinstance(number, int) or isinstance(number, bool):
                continue                                           # an unassigned string belongs to no inverter
            circuit = item.get("circuit")
            _require(circuit is None or isinstance(circuit, str), "an intake string's circuit is invalid")
            letter = mppt_letter(circuit)
            by_inverter.setdefault(number, []).append((letter, _int(item.get("panel_count")), item.get("circuit")))
        if numbers_recorded:
            # One summary per distinct inverter DEVICE, in intake order (the adapter lists L1 then L2), as
            # DesignSnapshotCollector.BuildInverterSummaries walks GetDistinctInverters. A device gets every string
            # whose inverter number equals its own (Inverter.FindConnectedCables, Inverter.cs:361): a number two
            # devices share gives both the strings, and a string whose number no device has joins no summary.
            numbers = []
            for device in devices:
                _require(isinstance(device, dict), "an intake device is invalid")
                number = device.get("number")
                _require(isinstance(number, int) and not isinstance(number, bool) and number >= 0,
                         "an intake device's number is invalid")
                numbers.append(number)
        else:
            numbers = sorted(by_inverter)
        for number in numbers:
            config = snap["inverter_types"].get("A")
            solar_edge = bool(config and config["is_solar_edge"])
            summary = {"number": number, "type_key": "A", "is_solar_edge": solar_edge,
                       "is_combiner_box": use_combiner and not solar_edge, "strings_by_mppt": {}}
            for letter, count, circuit in by_inverter.get(number, ()):
                summary["strings_by_mppt"].setdefault(letter, []).append(count)
                snap["strings"].append({"panel_count": count, "inverter": number, "mppt": letter,
                                        "type_key": "A", "circuit": circuit})
            snap["summaries"].append(summary)
    if mode == "plugin":
        l1_devices = 0
        for device in devices:
            _require(isinstance(device, dict) and isinstance(device.get("is_l2"), bool), "an intake device is invalid")
            l1_devices += 0 if device["is_l2"] else 1
        runtime = intake.get("runtime_lists")
        _require(isinstance(runtime, dict) and runtime.get("inverter_list_count") == l1_devices,
                 "the intake's runtime inverter list does not match its L1 devices")
        _require(l1_devices == 0, "a non-empty L1 inverter list is not carried by this intake version")
    snap["inverter_count"] = len(snap["summaries"])
    snap["has_design"] = not snap["inverter_list_null"] and snap["inverter_count"] > 0
    return snap


# ---------------------------------------------------------------------------------------------
# The rules, in GuardrailEngine.RegisterAllRules order. Each returns [severity, ...].

def cold_voc(s):
    """ColdVocRule (Rules/Electrical/ColdVocRule.cs)."""
    if s["module_voc"] <= 0 or s["panels_in_sequence"] <= 0:
        return [PASS]
    bvoc = s["module_bvoc_pct"] / 100.0
    voc_cold = s["module_voc"] * (1.0 + bvoc * (s["min_temp_c"] - 25.0))
    string_voc = voc_cold * s["panels_in_sequence"]
    out, checked, skipped = [], False, 0
    for config in s["inverter_types"].values():
        if config is None or config["max_dc_voltage"] <= 0:
            continue
        if config["is_solar_edge"]:
            skipped += 1
            continue
        checked = True
        limit = config["max_dc_voltage"]
        if string_voc > limit:
            out.append(CRITICAL)
        else:
            margin = (limit - string_voc) / limit * 100.0
            out.append(CRITICAL if margin < 1.0 else WARNING if margin < 5.0 else PASS)
    if skipped:
        out.append(INFO)
    if not checked and skipped == 0:
        out.append(INFO)
    return out


def current_safety_factor(s):
    """CurrentSafetyFactorRule (Rules/Electrical/CurrentSafetyFactorRule.cs)."""
    opt = s["optimizer"]
    if opt is None:
        return [PASS]
    if s["module_isc"] <= 0 or opt["max_input_isc"] <= 0:
        return [WARNING]
    return [ERROR if s["module_isc"] * NEC_CONTINUOUS_FACTOR > opt["max_input_isc"] else PASS]


def mppt_window(s):
    """MpptVoltageWindowRule (Rules/Electrical/MpptVoltageWindowRule.cs)."""
    if s["module_vmp"] <= 0 or s["module_voc"] <= 0 or s["panels_in_sequence"] <= 0:
        return [PASS]
    bvoc = s["module_bvoc_pct"] / 100.0
    vmp_hot = s["module_vmp"] * (1.0 + bvoc * (s["max_temp_c"] - 25.0)) * s["panels_in_sequence"]
    voc_cold = s["module_voc"] * (1.0 + bvoc * (s["min_temp_c"] - 25.0)) * s["panels_in_sequence"]
    out, skipped = [], 0
    for config in s["inverter_types"].values():
        if config is None:
            continue
        if config["is_solar_edge"]:
            skipped += 1
            continue
        low, high = config["mppt_voltage_range_min"], config["mppt_voltage_range_max"]
        if low > 0:
            if vmp_hot < low:
                out.append(ERROR)
            else:
                out.append(WARNING if (vmp_hot - low) / low * 100.0 < 5.0 else PASS)
        if high > 0:
            out.append(ERROR if voc_cold > high else PASS)
    if skipped:
        out.append(INFO)
    if not s["inverter_types"]:
        out.append(INFO)
    return out


def overcurrent(s):
    """OvercurrentProtectionRule (Rules/Electrical/OvercurrentProtectionRule.cs)."""
    types, summaries = s["inverter_types"], s["summaries"]

    def solar_edge_summary(summary):
        config = types.get(summary["type_key"])
        return summary["is_solar_edge"] or bool(config and config["is_solar_edge"])

    has_solar_edge = any(c and c["is_solar_edge"] for c in types.values()) or any(map(solar_edge_summary, summaries))
    has_combiner = any(summary["is_combiner_box"] for summary in summaries)
    if summaries:
        central = any(not solar_edge_summary(x) and not x["is_combiner_box"] for x in summaries)
    elif any(c is not None for c in types.values()):
        central = any(c is not None and not c["is_solar_edge"] for c in types.values())
    else:
        central = True
    out = []
    if has_solar_edge:
        out.append(WARNING)
    if has_combiner:
        out.append(WARNING)
    if not central:
        return out
    if s["module_isc"] <= 0:
        return out + [PASS]
    required = s["module_isc"] * 1.25 * 1.25                   # NecCitation.OvercurrentDevice_690_9B
    return out + [PASS if any(rating >= required for rating in STANDARD_OCPD_RATINGS) else WARNING]


def optimizer_cold_voc(s):
    """OptimizerColdVocRule (Rules/Electrical/OptimizerColdVocRule.cs)."""
    opt = s["optimizer"]
    if opt is None or s["module_voc"] <= 0:
        return [PASS]
    if opt["max_input_voltage"] <= 0:
        return [WARNING]
    voc_cold = s["module_voc"] * (1.0 + s["module_bvoc_pct"] / 100.0 * (s["min_temp_c"] - 25.0))
    limit = opt["max_input_voltage"]
    if voc_cold > limit:
        return [CRITICAL]
    return [WARNING if (limit - voc_cold) / limit * 100.0 < 5.0 else PASS]


def optimizer_compatibility(s):
    """OptimizerCompatibilityRule (Rules/Hardware/OptimizerCompatibilityRule.cs)."""
    opt = s["optimizer"]
    if opt is None:
        return [PASS]
    if s["module_pmax"] <= 0:
        return [INFO]
    power_ok = opt["max_input_power"] <= 0 or s["module_pmax"] * opt["modules_per_optimizer"] <= opt["max_input_power"]
    isc_ok = opt["max_input_isc"] <= 0 or s["module_isc"] <= opt["max_input_isc"]
    out = ([] if power_ok else [ERROR]) + ([] if isc_ok else [ERROR])
    return out or [PASS]


def string_length_delta(s):
    """StringLengthDeltaRule (Rules/Hardware/StringLengthDeltaRule.cs)."""
    opt = s["optimizer"]
    if opt is None or opt["max_string_length_delta"] <= 0:
        return [PASS]
    out = []
    for summary in s["summaries"]:
        lengths = [n for values in summary["strings_by_mppt"].values() for n in values]
        if len(lengths) >= 2 and max(lengths) - min(lengths) > opt["max_string_length_delta"]:
            out.append(WARNING)
    return out or [PASS]


def optimizer_family(s):
    """OptimizerFamilyMatchRule (Rules/Hardware/OptimizerFamilyMatchRule.cs). The optimizer path refuses earlier
    (build_snapshot), so only the no-optimizer outcome is reachable here."""
    return [PASS] if s["optimizer"] is None else [INFO]


def mppt_balance(s):
    """MpptBalanceRule (Rules/Design/MpptBalanceRule.cs)."""
    summaries = s["summaries"]
    if not summaries:
        return [PASS]
    out, imbalance, single, multi, solar_edge, combiner = [], False, 0, 0, 0, 0
    for summary in summaries:
        if summary["is_solar_edge"]:
            solar_edge += 1
            continue
        if summary["is_combiner_box"]:
            combiner += 1
            continue
        if len(summary["strings_by_mppt"]) < 2:
            single += 1
            continue
        multi += 1
        totals = [sum(values) for values in summary["strings_by_mppt"].values()]
        high, low = max(totals), min(totals)
        if high == 0:
            continue
        if (high - low) / high * 100.0 > MPPT_IMBALANCE_PCT:
            imbalance = True
            out.append(WARNING)
    if solar_edge:
        out.append(INFO)
    if combiner:
        out.append(INFO)
    if not imbalance:
        if multi:
            out.append(PASS)
        elif single:
            out.append(INFO)
        elif solar_edge == 0 and combiner == 0:
            out.append(PASS)
    return out


def dc_ac_ratio(s):
    """DcAcRatioRule (Rules/Design/DcAcRatioRule.cs)."""
    if s["module_pmax"] <= 0 or s["panels_in_sequence"] <= 0:
        return [PASS]
    panels = sum(item["panel_count"] for item in s["strings"])
    if panels == 0:
        if s["inverter_count"] > 0 and s["dc_inputs_per_mppt"] > 0 and s["num_mppt"] > 0:
            panels = s["inverter_count"] * s["num_mppt"] * s["dc_inputs_per_mppt"] * s["panels_in_sequence"]
        else:
            return [INFO]
    dc_kw = panels * s["module_pmax"] / 1000.0
    ac_kw = 0.0
    types = s["inverter_types"]
    for key, config in types.items():
        if config is None:
            continue
        count = sum(1 for summary in s["summaries"] if summary["type_key"] == key)
        if count == 0:
            count = config["suggested_count"] if config["suggested_count"] > 0 else (
                s["inverter_count"] if len(types) == 1 and s["inverter_count"] > 0 else 1)
        ac_kw += config["max_ac_power_kw"] * count
    if ac_kw <= 0:
        return [INFO]
    ratio = dc_kw / ac_kw
    return [WARNING if ratio < DC_AC_MIN or ratio > DC_AC_MAX else PASS]


def strings_per_mppt(s):
    """StringsPerMpptRule (Rules/Design/StringsPerMpptRule.cs)."""
    if not s["summaries"]:
        return [INFO]
    out, overflow, unknown, evaluated = [], False, 0, 0
    for summary in s["summaries"]:
        config = s["inverter_types"].get(summary["type_key"])
        capacity = dc_inputs_per_mppt(config) if config is not None and dc_inputs_per_mppt(config) > 0 else (
            s["dc_inputs_per_mppt"] if s["dc_inputs_per_mppt"] > 0 else 0)
        if capacity <= 0:
            unknown += 1
            continue
        evaluated += 1
        for values in summary["strings_by_mppt"].values():
            if len(values) > capacity:
                overflow = True
                out.append(ERROR)
    if not overflow:
        if evaluated:
            out.append(PASS)
        elif unknown:
            out.append(INFO)
    return out


def global_state(s):
    """GlobalStateNullRule (Rules/CodeSafety/GlobalStateNullRule.cs)."""
    if not s["has_design"]:
        return [PASS]
    nulls = s["cable_dictionary_null"] or s["inverter_list_null"] or s["tag_dictionary_null"]
    return [WARNING if nulls else PASS]


def division_guard(s):
    """DivisionByZeroGuardRule (Rules/CodeSafety/DivisionByZeroGuardRule.cs)."""
    opt = s["optimizer"]
    if opt is None:
        return [PASS]
    return [ERROR if s["module_pmax"] * opt["modules_per_optimizer"] <= 0 else PASS]


def database_values(s):
    """DatabaseValueIntegrityRule (Rules/CodeSafety/DatabaseValueIntegrityRule.cs), inverter-type checks (the
    optimizer checks are unreachable: build_snapshot refuses optimizer designs)."""
    issues = 0
    for config in s["inverter_types"].values():
        if config is None:
            continue
        low, high, max_v = config["mppt_voltage_range_min"], config["mppt_voltage_range_max"], config["max_dc_voltage"]
        if low > 0 and high > 0 and low >= high:
            issues += 1
        if max_v > 0 and high > 0 and high > max_v:
            issues += 1
        if config["num_mppt_trackers"] <= 0 and s["summaries"]:
            issues += 1
    return [WARNING if issues else PASS]


# (rule id, title, category, evaluate), GuardrailEngine.RegisterAllRules order (GuardrailEngine.cs:35-59).
RULES = (
    ("NEC-690.7-VOC-COLD", "Cold-temperature Voc limit", "Electrical", cold_voc),
    ("NEC-690.8A-ISC", "Current 1.25x safety factor", "Electrical", current_safety_factor),
    ("MPPT-WINDOW", "MPPT voltage window", "Electrical", mppt_window),
    ("NEC-690.9-OCPD", "Overcurrent protection", "Electrical", overcurrent),
    ("NEC-690.7-VOC-COLD-OPTIMIZER", "Cold Voc per optimizer input", "Electrical", optimizer_cold_voc),
    ("HW-OPTI-COMPAT", "Optimizer-module compatibility", "Hardware", optimizer_compatibility),
    ("HW-STRING-DELTA", "String length delta", "Hardware", string_length_delta),
    ("HW-OPTI-FAMILY", "Optimizer-inverter family match", "Hardware", optimizer_family),
    ("DESIGN-MPPT-BAL", "MPPT balance", "Design", mppt_balance),
    ("DESIGN-DC-AC", "DC/AC ratio", "Design", dc_ac_ratio),
    ("DESIGN-STRINGS-MPPT", "Strings per MPPT capacity", "Design", strings_per_mppt),
    ("SAFE-NULL-STATE", "Global state integrity", "Code Safety", global_state),
    ("SAFE-DIV-ZERO", "Division by zero guard", "Code Safety", division_guard),
    ("SAFE-DB-VALUES", "Database value integrity", "Code Safety", database_values),
)


def evaluate(snapshot):
    """GuardrailEngine.EvaluateAll then OrderForDisplay: [(section, title, rule id, severity)] in palette order."""
    results = [(category, title, rule_id, severity)
               for rule_id, title, category, rule in RULES for severity in rule(snapshot)]
    ordered = []
    for category in DISPLAY_CATEGORY_ORDER:
        group = [r for r in results if r[0] == category]
        ordered.extend(sorted(group, key=lambda r: -r[3]))     # stable, worst first
    return [(SECTION_NAMES[category], title, rule_id, severity) for category, title, rule_id, severity in ordered]


def banner(ordered):
    """HealthBanner.Update: the status word and the count parts it prints, as {name: count}."""
    counts = [0] * 5
    for *_, severity in ordered:
        counts[severity] += 1
    worst = max((severity for *_, severity in ordered), default=PASS)
    status = "CRITICAL" if worst == CRITICAL else "ERRORS DETECTED" if worst == ERROR else \
        "WARNINGS" if worst == WARNING else "HEALTHY"
    parts = {}
    for severity in (CRITICAL, ERROR, WARNING, INFO):
        if counts[severity] > 0:
            parts[SEVERITY_NAMES[severity]] = counts[severity]
    parts["pass"] = counts[PASS]
    return status, parts


def guardrail_rows(intake, mode="drawing"):
    """{kind: [(id, fields)]}: `verdict` rows in display order and `report` rows (status, <class>-count) in the plugin
    adapter's shape."""
    ordered = evaluate(build_snapshot(intake, mode))
    status, parts = banner(ordered)
    verdicts = [(f"verdict-{number}", {"section": section, "rule": title, "rule_id": rule_id,
                                        "status": SEVERITY_NAMES[severity]})
                for number, (section, title, rule_id, severity) in enumerate(ordered, 1)]
    values = {"status": status, **{f"{name}-count": count for name, count in parts.items()}}
    reports = [(f"report-{name}", {"name": name, "value": values[name]}) for name in sorted(values)]
    return {"verdict": verdicts, "report": reports}
