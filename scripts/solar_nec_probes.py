#!/usr/bin/env python3
"""Studio's ports of the six licensed NEC DEMO scenario lists, and their probe files.

Each LEAF*DEMO command in the Branch2025 plugin runs a FIXED scenario list
through one NEC engine and writes a JSON probe file to %TEMP%. This module holds
Studio's own copy of those six lists and runs them through server/solar_nec.py,
emitting files in the plugin's exact JSON shape: same keys, same key order, same
value types, same order of probes.

Scenario lists ported from, read 2026-09-22 at C:/tmp/solar-parity/wt-b25-s17:

  * Terrain/VdropAcDemoCommand.cs:85-192      -> leafvdropac_probes.json
  * Terrain/AmpCorrDemoCommand.cs:92-182      -> leafampcorr_probes.json
  * Terrain/ConduitFillDemoCommand.cs:98-218  -> leafconduitfill_probes.json
  * Terrain/MaxFillDemoCommand.cs:84-159      -> leafmaxfill_probes.json
  * Terrain/FeederOcpdDemoCommand.cs:69-129   -> leaffeederocpd_probes.json
  * Terrain/OneLinerDemoCommand.cs:93-206     -> leafoneliner_probes.json

Contract: the scenario list is the FIXTURE (joint identity contract v5 rule E1),
so it is written here as literal inputs and NEVER read from the plugin's output.
Every number in a probe record is COMPUTED by server/solar_nec.py; nothing is
copied from the licensed capture. Value types are pinned explicitly (float() on
a fraction the engine may return as an int sentinel) because a probe file that
writes 0 where the plugin writes 0.0 is a parity failure, not a formatting
detail.

No network, no dependencies outside the standard library.

Usage:
    python scripts/solar_nec_probes.py --out-dir <dir> --all
    python scripts/solar_nec_probes.py --out-dir <dir> --demo leafmaxfill
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def _load_solar_nec():
    """Load server/solar_nec.py by path so the import works from any cwd."""
    path = ROOT / "server" / "solar_nec.py"
    spec = importlib.util.spec_from_file_location("solar_nec", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nec = _load_solar_nec()


# --------------------------------------------------------------------------- #
# LEAFVDROPACDEMO -- NecCitation.VoltageDropAc
# (name, I, L_ft, R, X, powerFactor, phase, V_source)
# --------------------------------------------------------------------------- #
VDROPAC_SCENARIOS = (
    ("single_phase_unity_pf", 10.0, 100.0, 0.5, 0.1, 1.0, 1, 240.0),
    ("three_phase_unity_pf", 50.0, 200.0, 0.3, 0.1, 1.0, 3, 480.0),
    ("single_phase_pf_95", 30.0, 150.0, 0.4, 0.08, 0.95, 1, 208.0),
    ("three_phase_pf_90", 100.0, 300.0, 0.2, 0.05, 0.9, 3, 480.0),
    ("zero_current", 0.0, 100.0, 0.5, 0.1, 1.0, 1, 240.0),
    ("zero_reactance", 20.0, 100.0, 0.5, 0.0, 0.95, 1, 240.0),
    ("pf_over_one_clamped", 10.0, 100.0, 0.5, 0.1, 1.2, 1, 240.0),
    ("pf_negative_clamped", 10.0, 100.0, 0.5, 0.1, -0.5, 1, 240.0),
    ("zero_source_voltage", 50.0, 200.0, 0.3, 0.1, 1.0, 3, 0.0),
    ("long_run", 75.0, 1000.0, 0.25, 0.07, 0.9, 3, 600.0),
    ("low_voltage_residential", 25.0, 50.0, 0.6, 0.12, 0.95, 1, 120.0),
)

# --------------------------------------------------------------------------- #
# LEAFAMPCORRDEMO -- NecCitation.AmpacityCorrection_310_15B16
# (name, baseA, tempFactor, conduitFactor)
# --------------------------------------------------------------------------- #
AMPCORR_SCENARIOS = (
    ("all_unity", 1.0, 1.0, 1.0),
    ("temp_only", 100.0, 0.82, 1.0),
    ("conduit_only", 100.0, 1.0, 0.7),
    ("both", 100.0, 0.82, 0.7),
    ("zero_base", 0.0, 0.82, 0.7),
    ("zero_temp", 100.0, 0.0, 0.7),
    ("zero_conduit", 100.0, 0.82, 0.0),
    ("high_base", 600.0, 0.88, 0.8),
    ("small_factors", 50.0, 0.5, 0.5),
    ("over_unity", 40.0, 1.05, 1.0),
    ("fractional_base", 12.5, 0.75, 0.8),
)

# --------------------------------------------------------------------------- #
# LEAFMAXFILLDEMO -- NecConduitFillEngine.MaxFillFraction. (name, n)
# --------------------------------------------------------------------------- #
MAXFILL_SCENARIOS = (
    ("neg_minus_5", -5),
    ("neg_one", -1),
    ("zero", 0),
    ("one", 1),
    ("two", 2),
    ("three", 3),
    ("four", 4),
    ("ten", 10),
    ("hundred", 100),
    ("thousand", 1000),
    ("intmax", 2147483647),  # int.MaxValue, the int-range boundary
)

# --------------------------------------------------------------------------- #
# LEAFFEEDEROCPDDEMO -- NecOcpdSizing.SizeFeederOcpd
# (name, continuousA, isContinuous, egcMaterial)
# --------------------------------------------------------------------------- #
FEEDEROCPD_SCENARIOS = (
    ("feeder_cont_cu_20a", 20.0, True, nec.COPPER),
    ("feeder_cont_al_20a", 20.0, True, nec.ALUMINUM),
    ("feeder_noncont_cu_20a", 20.0, False, nec.COPPER),
    ("feeder_cont_cu_100a", 100.0, True, nec.COPPER),
    ("feeder_cont_small_10a", 10.0, True, nec.COPPER),
    ("feeder_cont_at_boundary_12a", 12.0, True, nec.COPPER),
    ("feeder_cont_at_table_max_4800a", 4800.0, True, nec.COPPER),
    ("feeder_cont_above_table_7000a", 7000.0, True, nec.COPPER),
    ("feeder_noncont_cu_100a", 100.0, False, nec.COPPER),
    ("feeder_noncont_al_100a", 100.0, False, nec.ALUMINUM),
    ("feeder_cont_fractional_12p5a", 12.5, True, nec.COPPER),
)

# --------------------------------------------------------------------------- #
# LEAFONELINERDEMO -- NecCitation.ToOneLiner
# (name, article, shortDesc, formula, result, units, hasSynthetic, synthNames)
# --------------------------------------------------------------------------- #
ONELINER_SCENARIOS = (
    ("minimal", "NEC 690.8(A)", "Continuous current", "I*1.25", 1.0, None, False, None),
    ("with_units", "NEC 690.8(A)", "Continuous current", "I*1.25", 1.0, "A", False, None),
    ("decimal_res", "NEC 690.7(A)(3)", "Cold Voc", "Voc*(1+b*dT)", 60.5, "V", False, None),
    ("zero_res", "A", "d", "f", 0.0, "%", False, None),
    ("neg_res", "A", "d", "f", -3.14, "C", False, None),
    ("six_digits", "A", "d", "f", 123.456, "ft", False, None),
    ("round_six", "A", "d", "f", 123.4567, "ft", False, None),
    ("empty_units", "A", "d", "f", 1.0, "", False, None),
    ("synth_one", "A", "d", "f", 1.0, None, True, ("bvoc",)),
    ("synth_multi", "A", "d", "f", 1.0, None, True, ("bvoc", "tmax")),
    ("synth_units", "A", "d", "f", 1.0, "V", True, ("isc",)),
)

# --------------------------------------------------------------------------- #
# LEAFCONDUITFILLDEMO -- four batteries over NecConduitFillEngine.
# --------------------------------------------------------------------------- #
CONDUITFILL_MAXFILL = (
    ("I1_count_1", 1),
    ("I2_count_2", 2),
    ("I3_count_3", 3),
    ("I3_count_100", 100),
    ("I4_count_0", 0),
    ("I4_count_neg", -5),
)

CONDUITFILL_CONDUCTOR_AREA = (
    ("I5_14_THWN2", "14", "AWG", nec.THWN2),
    ("I5_12_THWN2", "12", "AWG", nec.THWN2),
    ("I5_10_THWN2", "10", "AWG", nec.THWN2),
    ("I5_10_XHHW2", "10", "AWG", nec.XHHW2),
    ("I5_10_USE2", "10", "AWG", nec.USE2),
    ("I5_10_Bare", "10", "AWG", nec.BARE),
    ("I5_8_THWN2", "8", "AWG", nec.THWN2),
    ("I5_6_THWN2", "6", "AWG", nec.THWN2),
    ("I5_4_THWN2", "4", "AWG", nec.THWN2),
    ("I5_2_THWN2", "2", "AWG", nec.THWN2),
    ("I5_1_0_XHHW2", "1/0", "AWG", nec.XHHW2),
    ("I5_4_0_THWN2", "4/0", "AWG", nec.THWN2),
    ("I5_250_THWN2", "250", "kcmil", nec.THWN2),
    ("I5_500_XHHW2", "500", "kcmil", nec.XHHW2),
    ("I5_1000_THWN2", "1000", "kcmil", nec.THWN2),
    ("I5_unknown_gauge", "99", "AWG", nec.THWN2),
    ("I6_bare_250", "250", "kcmil", nec.BARE),
    ("I6_bare_500", "500", "kcmil", nec.BARE),
    ("I6_bare_1000", "1000", "kcmil", nec.BARE),
)

CONDUITFILL_CONDUIT_AREA = (
    ("I7_half_EMT", "1/2", nec.EMT),
    ("I7_half_PVC40", "1/2", nec.PVC_SCH40),
    ("I7_half_PVC80", "1/2", nec.PVC_SCH80),
    ("I7_half_RMC", "1/2", nec.RMC),
    ("I7_half_LFNC", "1/2", nec.LFNC_B),
    ("I7_1_EMT", "1", nec.EMT),
    ("I7_1q_EMT", "1-1/4", nec.EMT),
    ("I7_1h_EMT", "1-1/2", nec.EMT),
    ("I7_2_EMT", "2", nec.EMT),
    ("I7_2_LFNC", "2", nec.LFNC_B),
    ("I7_4_EMT", "4", nec.EMT),
    ("I7_unknown_trade", "9999", nec.EMT),
    ("I8_LFNC_2h", "2-1/2", nec.LFNC_B),
    ("I8_LFNC_3", "3", nec.LFNC_B),
    ("I8_LFNC_3h", "3-1/2", nec.LFNC_B),
    ("I8_LFNC_4", "4", nec.LFNC_B),
)

_TEN_THWN2 = ("10", "AWG", nec.THWN2)
_SIX_THWN2 = ("6", "AWG", nec.THWN2)

CONDUITFILL_SIZE_CONDUIT = (
    ("I9_one_10awg_emt", nec.EMT, (_TEN_THWN2,)),
    ("I9_two_10awg_emt", nec.EMT, (_TEN_THWN2, _TEN_THWN2)),
    ("I9_three_10awg_emt", nec.EMT, (_TEN_THWN2, _TEN_THWN2, _TEN_THWN2)),
    ("I9_mixed_pvc40", nec.PVC_SCH40, (_SIX_THWN2, _SIX_THWN2, ("10", "AWG", nec.BARE))),
    ("I9_feeder_pvc80", nec.PVC_SCH80,
     (("4/0", "AWG", nec.XHHW2), ("4/0", "AWG", nec.XHHW2), ("4/0", "AWG", nec.XHHW2),
      ("4", "AWG", nec.BARE))),
    ("I12_empty", nec.EMT, ()),
) + tuple(("I13_mono_%d" % n, nec.EMT, (_TEN_THWN2,) * n) for n in range(3, 11))


# --------------------------------------------------------------------------- #
# Probe builders. Each returns the exact JSON-ready document the plugin writes.
# --------------------------------------------------------------------------- #
def build_vdropac():
    probes = []
    for name, current, length, r, x, pf, phase, volts in VDROPAC_SCENARIOS:
        citation = nec.voltage_drop_ac(current, length, r, x, pf, phase, volts)
        probes.append({
            "Name": name,
            "Tag": name,
            "I_in": float(current),
            "L_in": float(length),
            "R_in": float(r),
            "X_in": float(x),
            "PF_in": float(pf),
            "Phase_in": int(phase),
            "V_in": float(volts),
            "Article": citation.article,
            "ShortDescription": citation.short_description,
            "Formula": citation.formula,
            "Result": float(citation.result),
            "Units": citation.units,
        })
    return probes


def build_ampcorr():
    probes = []
    for name, base_a, temp_factor, conduit_factor in AMPCORR_SCENARIOS:
        citation = nec.ampacity_correction_310_15b16(base_a, temp_factor, conduit_factor)
        rejected = citation.rejected_alternatives[0].would_have_resulted_in \
            if citation.rejected_alternatives else 0.0
        probes.append({
            "Name": name,
            "Tag": name,
            "BaseA": float(base_a),
            "TempFactor": float(temp_factor),
            "ConduitFactor": float(conduit_factor),
            "Article": citation.article,
            "Units": citation.units,
            "Formula": citation.formula,
            "Result": float(citation.result),
            "RejectedResult": float(rejected),
        })
    return probes


def build_maxfill():
    return [{"Name": name, "Tag": name, "N": int(n),
             "Fill": float(nec.max_fill_fraction(n))}
            for name, n in MAXFILL_SCENARIOS]


def build_feederocpd():
    probes = []
    for name, current, is_continuous, material in FEEDEROCPD_SCENARIOS:
        result = nec.size_feeder_ocpd(current, is_continuous, material)
        probes.append({
            "Name": name,
            "Tag": name,
            "ContinuousCurrentA": float(current),
            "IsContinuous": bool(is_continuous),
            "EgcMaterial": material,
            "MinOcpdA": float(result.min_ocpd_a),
            "OcpdRatingA": float(result.ocpd_rating_a),
            "EgcGauge": result.egc_gauge,
            "Note": result.note,
        })
    return probes


def build_oneliner():
    probes = []
    for name, article, short_desc, formula, result, units, synthetic, names in ONELINER_SCENARIOS:
        synth_names = list(names) if names else []
        citation = nec.NecCitation(
            article=article, short_description=short_desc, formula=formula,
            result=result, units=units, has_synthetic_inputs=synthetic,
            synthetic_input_names=synth_names)
        probes.append({
            "Name": name,
            "Tag": name,
            "Article": article,
            "ShortDescription": short_desc,
            "Formula": formula,
            "Result": float(result),
            "Units": units,
            "HasSyntheticInputs": bool(synthetic),
            "SyntheticInputNames": list(synth_names),
            "OneLiner": citation.to_one_liner(),
        })
    return probes


def build_conduitfill():
    payload = {"maxFill": [], "conductorArea": [], "conduitArea": [], "sizeConduit": []}

    for label, count in CONDUITFILL_MAXFILL:
        payload["maxFill"].append({
            "label": label,
            "conductorCount": int(count),
            "csharpFraction": float(nec.max_fill_fraction(count)),
        })

    for label, gauge, unit, insulation in CONDUITFILL_CONDUCTOR_AREA:
        payload["conductorArea"].append({
            "label": label,
            "gauge": gauge,
            "unit": unit,
            "insulation": insulation,
            "csharpArea": float(nec.lookup_conductor_area(gauge, unit, insulation)),
        })

    for label, trade_size, conduit_type in CONDUITFILL_CONDUIT_AREA:
        payload["conduitArea"].append({
            "label": label,
            "tradeSize": trade_size,
            "conduitType": conduit_type,
            "csharpArea": float(nec.lookup_conduit_area(trade_size, conduit_type)),
        })

    for label, conduit_type, conductors in CONDUITFILL_SIZE_CONDUIT:
        built = [nec.ConduitConductor(gauge=g, gauge_unit=u, insulation=i)
                 for g, u, i in conductors]
        result = nec.size_conduit(conduit_type, built)
        payload["sizeConduit"].append({
            "label": label,
            "conduitType": conduit_type,
            "conductors": [{"gauge": g, "unit": u, "insulation": i} for g, u, i in conductors],
            "success": bool(result.success),
            "tradeSize": result.trade_size,
            "conductorAreaSqIn": float(result.conductor_area_sq_in),
            "conduitAreaSqIn": float(result.conduit_area_sq_in),
            "fillPct": float(result.fill_pct),
            "maxFillPct": float(result.max_fill_pct),
            "totalConductors": int(result.total_conductors),
            "failureReason": result.failure_reason,
        })

    return payload


DEMOS = {
    "leafvdropac": build_vdropac,
    "leafampcorr": build_ampcorr,
    "leafconduitfill": build_conduitfill,
    "leafmaxfill": build_maxfill,
    "leaffeederocpd": build_feederocpd,
    "leafoneliner": build_oneliner,
}
# The capability each DEMO proves, for the probe-evidence normalizer's caller.
DEMO_CAPABILITIES = {
    "leafvdropac": "nec-ac-voltage-drop",
    "leafampcorr": "nec-ampacity-correction",
    "leafconduitfill": "nec-conduit-fill",
    "leafmaxfill": "nec-conduit-fill",
    "leaffeederocpd": "nec-feeder-ocpd-sizing",
    "leafoneliner": "nec-citation-one-line-render",
}
# The probe-type key solar_probe_evidence.py reads to project a file into rows.
DEMO_PROBE_TYPES = {
    "leafvdropac": "nec-ac-voltage-drop",
    "leafampcorr": "nec-ampacity-correction",
    "leafconduitfill": "nec-conduit-fill-tables",
    "leafmaxfill": "nec-max-fill-fraction",
    "leaffeederocpd": "nec-feeder-ocpd-sizing",
    "leafoneliner": "nec-citation-one-line-render",
}


def build(demo):
    """Run one DEMO's scenario list and return its JSON-ready probe document."""
    if demo not in DEMOS:
        raise ValueError("unknown demo: " + str(demo))
    return DEMOS[demo]()


def file_name(demo):
    return demo + "_probes.json"


def write(demo, out_dir):
    """Write one probe file the way Newtonsoft writes it: 2-space indent, no
    trailing newline, non-ASCII characters left as themselves."""
    path = Path(out_dir) / file_name(demo)
    payload = json.dumps(build(demo), indent=2, ensure_ascii=False, allow_nan=False)
    path.write_text(payload, encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--demo", action="append", choices=sorted(DEMOS), default=None)
    parser.add_argument("--all", action="store_true", help="run every demo")
    args = parser.parse_args(argv)
    demos = sorted(DEMOS) if args.all else (args.demo or [])
    if not demos:
        print("solar-nec-probes: name --demo at least once, or pass --all", file=sys.stderr)
        return 2
    try:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for demo in demos:
            print(write(demo, args.out_dir))
    except (OSError, ValueError, TypeError) as exc:
        print("solar-nec-probes: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
