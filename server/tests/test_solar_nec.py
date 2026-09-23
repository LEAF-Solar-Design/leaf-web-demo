"""Studio's NEC port against the licensed plugin, computed rather than looked up.

Three layers, all hermetic and none of them skipping:

  1. The C# number formatters (`G6`, `F<n>`, default) this port depends on, with
     the away-from-zero tie rule that differs from Python's own rounding.
  2. The plugin's OWN unit tests, ported case for case from
     Tests/Tests/CableSizing/NecConduitFillEngineTests.cs and
     Tests/Tests/CableSizing/NecOcpdSizingTests.cs (read 2026-09-22 at
     C:/tmp/solar-parity/wt-b25-s17).
  3. Every probe in the six licensed DEMO capture files, reproduced by RUNNING
     Studio's scenario lists through server/solar_nec.py. The expected values
     below are the licensed AutoCAD 2025 capture of 2026-09-23
     (~/.claude/plans/ref/solar-parity-20260917/receipts/w3-demo-probes-20260923),
     transcribed so this suite is hermetic on a runner that does not carry the
     capture; when the capture IS present, test_licensed_capture_reproduced
     re-reads all six files and requires the generated documents to equal them
     exactly, so the transcription cannot drift away from the artifact.

Nothing here feeds a lookup table back into the port: solar_nec.py computes
every number and renders every string, and these are the assertions it must meet.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nec = _load("solar_nec", ROOT / "server" / "solar_nec.py")
probes = _load("solar_nec_probes", ROOT / "scripts" / "solar_nec_probes.py")

# The licensed capture is committed with the receipts, so every runner carries it.
DEFAULT_REFERENCE_DIR = (Path(__file__).resolve().parents[2]
                         / "docs/parity/evidence/nec/demo-probes-20260923")
REFERENCE_DIR = Path(os.environ.get("LEAF_SOLAR_W3_PROBE_REF", str(DEFAULT_REFERENCE_DIR)))

VDROP_PREFIX = "VD% = ({0} \u00d7 I \u00d7 (R\u00b7cos\u03c6 + X\u00b7sin\u03c6) \u00d7 L / 1000) / V \u00d7 100 = "
AMPCORR_PREFIX = "I_corrected = I_base \u00d7 temp_factor \u00d7 conduit_factor = "


# --------------------------------------------------------------------------- #
# Licensed capture, transcribed. Keys are probe names; see the module docstring.
# --------------------------------------------------------------------------- #
VDROPAC_EXPECTED = {
    # name: (glyph, formula tail, Result)
    "single_phase_unity_pf": ("2", "(2 \u00d7 10 \u00d7 (0.5\u00b71 + 0.1\u00b70.000) \u00d7 100 / 1000) / 240 \u00d7 100", 0.4166666666666667),
    "three_phase_unity_pf": ("\u221a3", "(\u221a3 \u00d7 50 \u00d7 (0.3\u00b71 + 0.1\u00b70.000) \u00d7 200 / 1000) / 480 \u00d7 100", 1.0825317547305482),
    "single_phase_pf_95": ("2", "(2 \u00d7 30 \u00d7 (0.4\u00b70.95 + 0.08\u00b70.312) \u00d7 150 / 1000) / 208 \u00d7 100", 1.7523172730492027),
    "three_phase_pf_90": ("\u221a3", "(\u221a3 \u00d7 100 \u00d7 (0.2\u00b70.9 + 0.05\u00b70.436) \u00d7 300 / 1000) / 480 \u00d7 100", 2.184489484617198),
    "zero_current": ("2", "(2 \u00d7 0 \u00d7 (0.5\u00b71 + 0.1\u00b70.000) \u00d7 100 / 1000) / 240 \u00d7 100", 0.0),
    "zero_reactance": ("2", "(2 \u00d7 20 \u00d7 (0.5\u00b70.95 + 0\u00b70.312) \u00d7 100 / 1000) / 240 \u00d7 100", 0.7916666666666665),
    "pf_over_one_clamped": ("2", "(2 \u00d7 10 \u00d7 (0.5\u00b71 + 0.1\u00b70.000) \u00d7 100 / 1000) / 240 \u00d7 100", 0.4166666666666667),
    "pf_negative_clamped": ("2", "(2 \u00d7 10 \u00d7 (0.5\u00b70 + 0.1\u00b71.000) \u00d7 100 / 1000) / 240 \u00d7 100", 0.08333333333333334),
    "zero_source_voltage": ("\u221a3", "(\u221a3 \u00d7 50 \u00d7 (0.3\u00b71 + 0.1\u00b70.000) \u00d7 200 / 1000) / 0 \u00d7 100", 0.0),
    "long_run": ("\u221a3", "(\u221a3 \u00d7 75 \u00d7 (0.25\u00b70.9 + 0.07\u00b70.436) \u00d7 1000 / 1000) / 600 \u00d7 100", 5.532003409373658),
    "low_voltage_residential": ("2", "(2 \u00d7 25 \u00d7 (0.6\u00b70.95 + 0.12\u00b70.312) \u00d7 50 / 1000) / 120 \u00d7 100", 1.2655624749799799),
}

AMPCORR_EXPECTED = {
    # name: (formula tail, Result, RejectedResult)
    "all_unity": ("1 \u00d7 1 \u00d7 1", 1.0, 1.0),
    "temp_only": ("100 \u00d7 0.82 \u00d7 1", 82.0, 100.0),
    "conduit_only": ("100 \u00d7 1 \u00d7 0.7", 70.0, 70.0),
    "both": ("100 \u00d7 0.82 \u00d7 0.7", 57.4, 70.0),
    "zero_base": ("0 \u00d7 0.82 \u00d7 0.7", 0.0, 0.0),
    "zero_temp": ("100 \u00d7 0 \u00d7 0.7", 0.0, 70.0),
    "zero_conduit": ("100 \u00d7 0.82 \u00d7 0", 0.0, 0.0),
    "high_base": ("600 \u00d7 0.88 \u00d7 0.8", 422.40000000000003, 480.0),
    "small_factors": ("50 \u00d7 0.5 \u00d7 0.5", 12.5, 25.0),
    "over_unity": ("40 \u00d7 1.05 \u00d7 1", 42.0, 40.0),
    "fractional_base": ("12.5 \u00d7 0.75 \u00d7 0.8", 7.5, 10.0),
}

MAXFILL_EXPECTED = {
    "neg_minus_5": 0.0, "neg_one": 0.0, "zero": 0.0, "one": 0.53, "two": 0.31,
    "three": 0.4, "four": 0.4, "ten": 0.4, "hundred": 0.4, "thousand": 0.4,
    "intmax": 0.4,
}

FEEDEROCPD_EXPECTED = {
    # name: (MinOcpdA, OcpdRatingA, EgcGauge, Note)
    "feeder_cont_cu_20a": (25.0, 25.0, "10 AWG",
        "NEC 215.3 \u2014 20.0A \u00d7 1.25 = 25.0A \u2192 25A OCPD; EGC: 10 AWG per NEC 250.122"),
    "feeder_cont_al_20a": (25.0, 25.0, "8 AWG",
        "NEC 215.3 \u2014 20.0A \u00d7 1.25 = 25.0A \u2192 25A OCPD; EGC: 8 AWG per NEC 250.122"),
    "feeder_noncont_cu_20a": (20.0, 20.0, "12 AWG",
        "NEC 210.20(A) \u2014 20.0A \u2192 20A OCPD; EGC: 12 AWG per NEC 250.122"),
    "feeder_cont_cu_100a": (125.0, 125.0, "6 AWG",
        "NEC 215.3 \u2014 100.0A \u00d7 1.25 = 125.0A \u2192 125A OCPD; EGC: 6 AWG per NEC 250.122"),
    "feeder_cont_small_10a": (12.5, 15.0, "14 AWG",
        "NEC 215.3 \u2014 10.0A \u00d7 1.25 = 12.5A \u2192 15A OCPD; EGC: 14 AWG per NEC 250.122"),
    "feeder_cont_at_boundary_12a": (15.0, 15.0, "14 AWG",
        "NEC 215.3 \u2014 12.0A \u00d7 1.25 = 15.0A \u2192 15A OCPD; EGC: 14 AWG per NEC 250.122"),
    "feeder_cont_at_table_max_4800a": (6000.0, 6000.0, "750 kcmil",
        "NEC 215.3 \u2014 4800.0A \u00d7 1.25 = 6000.0A \u2192 6000A OCPD; EGC: 750 kcmil per NEC 250.122"),
    "feeder_cont_above_table_7000a": (8750.0, 8750.0, None,
        "NEC 215.3 \u2014 7000.0A \u00d7 1.25 = 8750.0A \u2192 8750A OCPD; EGC: N/A per NEC 250.122"),
    "feeder_noncont_cu_100a": (100.0, 100.0, "8 AWG",
        "NEC 210.20(A) \u2014 100.0A \u2192 100A OCPD; EGC: 8 AWG per NEC 250.122"),
    "feeder_noncont_al_100a": (100.0, 100.0, "6 AWG",
        "NEC 210.20(A) \u2014 100.0A \u2192 100A OCPD; EGC: 6 AWG per NEC 250.122"),
    "feeder_cont_fractional_12p5a": (15.625, 20.0, "12 AWG",
        "NEC 215.3 \u2014 12.5A \u00d7 1.25 = 15.6A \u2192 20A OCPD; EGC: 12 AWG per NEC 250.122"),
}

ONELINER_EXPECTED = {
    "minimal": "NEC 690.8(A) \u2014 Continuous current: I*1.25 = 1",
    "with_units": "NEC 690.8(A) \u2014 Continuous current: I*1.25 = 1 A",
    "decimal_res": "NEC 690.7(A)(3) \u2014 Cold Voc: Voc*(1+b*dT) = 60.5 V",
    "zero_res": "A \u2014 d: f = 0 %",
    "neg_res": "A \u2014 d: f = -3.14 C",
    "six_digits": "A \u2014 d: f = 123.456 ft",
    "round_six": "A \u2014 d: f = 123.457 ft",
    "empty_units": "A \u2014 d: f = 1",
    "synth_one": "A \u2014 d: f = 1 [SYNTHETIC: bvoc]",
    "synth_multi": "A \u2014 d: f = 1 [SYNTHETIC: bvoc, tmax]",
    "synth_units": "A \u2014 d: f = 1 V [SYNTHETIC: isc]",
}

CONDUITFILL_MAXFILL_EXPECTED = {
    "I1_count_1": 0.53, "I2_count_2": 0.31, "I3_count_3": 0.4,
    "I3_count_100": 0.4, "I4_count_0": 0.0, "I4_count_neg": 0.0,
}

CONDUCTOR_AREA_EXPECTED = {
    "I5_14_THWN2": 0.0097, "I5_12_THWN2": 0.0133, "I5_10_THWN2": 0.0211,
    "I5_10_XHHW2": 0.0176, "I5_10_USE2": 0.0211, "I5_10_Bare": 0.0106,
    "I5_8_THWN2": 0.0366, "I5_6_THWN2": 0.0507, "I5_4_THWN2": 0.0824,
    "I5_2_THWN2": 0.1158, "I5_1_0_XHHW2": 0.1676, "I5_4_0_THWN2": 0.3237,
    "I5_250_THWN2": 0.397, "I5_500_XHHW2": 0.6619, "I5_1000_THWN2": 1.3478,
    "I5_unknown_gauge": 0.0, "I6_bare_250": 0.0, "I6_bare_500": 0.0,
    "I6_bare_1000": 0.0,
}

CONDUIT_AREA_EXPECTED = {
    "I7_half_EMT": 0.304, "I7_half_PVC40": 0.285, "I7_half_PVC80": 0.217,
    "I7_half_RMC": 0.314, "I7_half_LFNC": 0.275, "I7_1_EMT": 0.864,
    "I7_1q_EMT": 1.496, "I7_1h_EMT": 2.036, "I7_2_EMT": 3.356,
    "I7_2_LFNC": 3.195, "I7_4_EMT": 15.901, "I7_unknown_trade": 0.0,
    "I8_LFNC_2h": 0.0, "I8_LFNC_3": 0.0, "I8_LFNC_3h": 0.0, "I8_LFNC_4": 0.0,
}

SIZE_CONDUIT_EXPECTED = {
    # label: (success, tradeSize, conductorArea, conduitArea, fillPct, maxFillPct,
    #         totalConductors, failureReason)
    "I9_one_10awg_emt": (True, "1/2", 0.0211, 0.304, 6.9407894736842115, 53.0, 1, None),
    "I9_two_10awg_emt": (True, "1/2", 0.0422, 0.304, 13.881578947368423, 31.0, 2, None),
    "I9_three_10awg_emt": (True, "1/2", 0.0633, 0.304, 20.82236842105263, 40.0, 3, None),
    "I9_mixed_pvc40": (True, "1/2", 0.112, 0.285, 39.298245614035096, 40.0, 3, None),
    "I9_feeder_pvc80": (True, "2", 0.9410999999999999, 2.874, 32.74530271398747, 40.0, 4, None),
    "I12_empty": (False, None, 0.0, 0.0, 0.0, 0.0, 0, "No conductors specified."),
    "I13_mono_3": (True, "1/2", 0.0633, 0.304, 20.82236842105263, 40.0, 3, None),
    "I13_mono_4": (True, "1/2", 0.0844, 0.304, 27.763157894736846, 40.0, 4, None),
    "I13_mono_5": (True, "1/2", 0.10550000000000001, 0.304, 34.703947368421055, 40.0, 5, None),
    "I13_mono_6": (True, "3/4", 0.12660000000000002, 0.533, 23.752345215759853, 40.0, 6, None),
    "I13_mono_7": (True, "3/4", 0.14770000000000003, 0.533, 27.711069418386497, 40.0, 7, None),
    "I13_mono_8": (True, "3/4", 0.16880000000000003, 0.533, 31.669793621013138, 40.0, 8, None),
    "I13_mono_9": (True, "3/4", 0.18990000000000004, 0.533, 35.62851782363978, 40.0, 9, None),
    "I13_mono_10": (True, "3/4", 0.21100000000000005, 0.533, 39.58724202626642, 40.0, 10, None),
}


def by_name(records, key="Name"):
    return {record[key]: record for record in records}


# --------------------------------------------------------------------------- #
# 1. C# number formatting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expected", [
    (0.0, "0"), (1.0, "1"), (60.5, "60.5"), (-3.14, "-3.14"),
    (123.456, "123.456"), (123.4567, "123.457"), (0.82, "0.82"),
    (0.08, "0.08"), (1.05, "1.05"), (2147483647.0, "2.14748E+09"),
    (0.0001, "0.0001"), (0.00001, "1E-05"), (1000000.0, "1E+06"),
    (999999.0, "999999"),
])
def test_format_g6_matches_csharp(value, expected):
    assert nec.format_g6(value) == expected


@pytest.mark.parametrize("value,decimals,expected", [
    (0.0, 3, "0.000"), (1.0, 3, "1.000"), (0.31224989991991997, 3, "0.312"),
    (0.43588989435406733, 3, "0.436"), (20.0, 1, "20.0"), (15.625, 1, "15.6"),
    (4800.0, 1, "4800.0"), (12.5, 1, "12.5"), (53.0, 0, "53"), (40.0, 0, "40"),
    # Ties go AWAY FROM ZERO, which is C#'s rule; Python's own round() would
    # give "0.12" and "2" here.
    (0.125, 2, "0.13"), (2.5, 0, "3"), (-2.5, 0, "-3"),
])
def test_format_fixed_matches_csharp(value, decimals, expected):
    assert nec.format_fixed(value, decimals) == expected


@pytest.mark.parametrize("value,expected", [
    (25.0, "25"), (20.0, "20"), (125.0, "125"), (6000.0, "6000"),
    (8750.0, "8750"), (12.5, "12.5"), (0.0, "0"),
])
def test_format_double_matches_csharp(value, expected):
    assert nec.format_double(value) == expected


# --------------------------------------------------------------------------- #
# 2. The plugin's own unit tests, ported
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("count,expected", [
    (1, 0.53), (2, 0.31), (3, 0.40), (4, 0.40), (10, 0.40), (0, 0.0),
])
def test_max_fill_fraction_returns_correct_limit(count, expected):
    assert nec.max_fill_fraction(count) == pytest.approx(expected, abs=0.001)


@pytest.mark.parametrize("gauge,unit,insulation,expected", [
    ("10", "AWG", nec.THWN2, 0.0211),
    ("10", "AWG", nec.BARE, 0.0106),
    ("4/0", "AWG", nec.THWN2, 0.3237),
    ("250", "kcmil", nec.XHHW2, 0.3700),
    ("99", "AWG", nec.THWN2, 0),
    ("250", "kcmil", nec.BARE, 0),
])
def test_lookup_conductor_area(gauge, unit, insulation, expected):
    assert nec.lookup_conductor_area(gauge, unit, insulation) == pytest.approx(expected, abs=0.0001)


@pytest.mark.parametrize("trade,conduit,expected", [
    ("3/4", nec.EMT, 0.533), ("1", nec.PVC_SCH40, 0.832), ("2", nec.RMC, 3.408),
    ("5", nec.EMT, 0), ("2-1/2", nec.LFNC_B, 0),
])
def test_lookup_conduit_area(trade, conduit, expected):
    assert nec.lookup_conduit_area(trade, conduit) == pytest.approx(expected, abs=0.001)


def test_size_for_circuit_typical_string_homerun():
    result = nec.size_for_circuit("10 AWG", 2, "10 AWG", nec.PVC_SCH40)
    assert result.success is True
    assert result.trade_size == "1/2"
    assert result.total_conductors == 3
    assert result.fill_pct < 40.0
    assert result.max_fill_pct == pytest.approx(40.0, abs=0.01)
    assert "NEC Ch9" in result.note


def test_size_for_circuit_large_trunk_picks_one_and_a_half_inch():
    result = nec.size_for_circuit("4/0 AWG", 2, "6 AWG", nec.PVC_SCH40)
    assert result.success is True
    assert result.trade_size == "1-1/2"
    assert result.fill_pct < 40.0


def test_size_for_circuit_no_egc_uses_thirty_one_percent():
    result = nec.size_for_circuit("10 AWG", 2, None, nec.EMT)
    assert result.success is True
    assert result.total_conductors == 2
    assert result.max_fill_pct == pytest.approx(31.0, abs=0.01)


def test_size_for_circuit_single_conductor_uses_fifty_three_percent():
    result = nec.size_for_circuit("6 AWG", 1, None, nec.EMT)
    assert result.success is True
    assert result.total_conductors == 1
    assert result.max_fill_pct == pytest.approx(53.0, abs=0.01)


def test_size_for_circuit_invalid_gauge_fails_closed():
    result = nec.size_for_circuit("99 AWG", 2, "10 AWG", nec.PVC_SCH40)
    assert result.success is False
    assert "Unknown conductor" in result.failure_reason


def test_size_for_circuit_null_gauge_fails_closed():
    result = nec.size_for_circuit(None, 2, "10 AWG")
    assert result.success is False
    assert "Invalid" in result.failure_reason


@pytest.mark.parametrize("conductors", [[], None])
def test_size_conduit_empty_or_null_list_fails(conductors):
    result = nec.size_conduit(nec.EMT, conductors)
    assert result.success is False
    assert result.failure_reason == "No conductors specified."


def test_size_conduit_three_phase_ac_four_conductors_plus_egc():
    conductors = [nec.ConduitConductor.parse("8 AWG", nec.THWN2) for _ in range(4)]
    conductors.append(nec.ConduitConductor.parse("10 AWG", nec.BARE))
    result = nec.size_conduit(nec.EMT, conductors)
    assert result.success is True
    assert result.total_conductors == 5
    assert result.max_fill_pct == pytest.approx(40.0, abs=0.01)
    assert result.conductor_area_sq_in == pytest.approx(0.1570, abs=0.001)


def test_pvc_sch80_requires_same_or_larger_trade_size_than_emt():
    conductors = [nec.ConduitConductor.parse("6 AWG", nec.THWN2),
                  nec.ConduitConductor.parse("6 AWG", nec.THWN2),
                  nec.ConduitConductor.parse("10 AWG", nec.BARE)]
    emt = nec.size_conduit(nec.EMT, conductors)
    pvc80 = nec.size_conduit(nec.PVC_SCH80, conductors)
    assert emt.success is True and pvc80.success is True
    assert nec.find_trade_index(pvc80.trade_size) >= nec.find_trade_index(emt.trade_size)


def test_lfnc_b_oversize_fails_gracefully():
    conductors = [nec.ConduitConductor.parse("4/0 AWG", nec.THWN2) for _ in range(4)]
    conductors.append(nec.ConduitConductor.parse("6 AWG", nec.BARE))
    result = nec.size_conduit(nec.LFNC_B, conductors)
    assert result.success is False
    assert "LFNC-B" in result.failure_reason


@pytest.mark.parametrize("label,gauge,unit,insulation", [
    ("10 AWG", "10", "AWG", nec.THWN2),
    ("250 kcmil", "250", "kcmil", nec.XHHW2),
])
def test_conduit_conductor_parse_splits(label, gauge, unit, insulation):
    conductor = nec.ConduitConductor.parse(label, insulation)
    assert (conductor.gauge, conductor.gauge_unit, conductor.insulation) == (gauge, unit, insulation)


@pytest.mark.parametrize("label", [None, "", "  "])
def test_conduit_conductor_parse_blank_returns_none(label):
    assert nec.ConduitConductor.parse(label) is None


@pytest.mark.parametrize("conduit,expected", [
    (nec.EMT, "EMT"), (nec.PVC_SCH40, "PVC Sch 40"), (nec.PVC_SCH80, "PVC Sch 80"),
    (nec.RMC, "RMC"), (nec.LFNC_B, "LFNC-B"),
])
def test_conduit_type_label(conduit, expected):
    assert nec.conduit_type_label(conduit) == expected


def test_fill_pct_matches_manual_calculation():
    conductors = [nec.ConduitConductor.parse("10 AWG") for _ in range(3)]
    result = nec.size_conduit(nec.EMT, conductors)
    assert result.success is True
    assert result.conductor_area_sq_in == pytest.approx(0.0633, abs=0.001)
    assert result.fill_pct == pytest.approx(0.0633 / result.conduit_area_sq_in * 100.0, abs=0.1)


@pytest.mark.parametrize("minimum,expected", [
    (13.0, 15), (15.0, 15), (15.1, 20), (20.0, 20), (31.0, 35),
    (100.0, 100), (101.0, 110), (500.0, 500), (7000.0, 7000),
])
def test_next_standard_ocpd(minimum, expected):
    assert nec.next_standard_ocpd(minimum) == expected


@pytest.mark.parametrize("amps,material,expected", [
    (15, nec.COPPER, "14 AWG"), (20, nec.COPPER, "12 AWG"), (30, nec.COPPER, "10 AWG"),
    (60, nec.COPPER, "10 AWG"), (100, nec.COPPER, "8 AWG"), (200, nec.COPPER, "6 AWG"),
    (400, nec.COPPER, "3 AWG"), (800, nec.COPPER, "1/0 AWG"),
    (15, nec.ALUMINUM, "12 AWG"), (20, nec.ALUMINUM, "10 AWG"),
    (100, nec.ALUMINUM, "6 AWG"), (200, nec.ALUMINUM, "4 AWG"),
    (7000, nec.COPPER, None),
])
def test_size_egc(amps, material, expected):
    assert nec.size_egc(amps, material) == expected


def test_size_pv_source_ocpd_typical_module():
    result = nec.size_pv_source_ocpd(11.0)
    assert result.continuous_current_a == pytest.approx(13.75, abs=0.01)
    assert result.min_ocpd_a == pytest.approx(17.1875, abs=0.01)
    assert result.ocpd_rating_a == 20
    assert result.egc_gauge == "12 AWG"
    assert "NEC 690.9(B)" in result.note
    assert "20A OCPD" in result.note


def test_size_pv_source_ocpd_high_isc_and_material_asymmetry():
    high = nec.size_pv_source_ocpd(18.0)
    assert high.ocpd_rating_a == 30
    assert high.egc_gauge == "10 AWG"
    copper = nec.size_pv_source_ocpd(11.0, nec.COPPER)
    aluminium = nec.size_pv_source_ocpd(11.0, nec.ALUMINUM)
    assert copper.ocpd_rating_a == aluminium.ocpd_rating_a
    assert copper.egc_gauge != aluminium.egc_gauge


@pytest.mark.parametrize("continuous,expected_min,expected_rating,marker", [
    (True, 100.0, 100, "NEC 215.3"),
    (False, 80.0, 80, "NEC 210.20(A)"),
])
def test_size_feeder_ocpd_continuous_factor(continuous, expected_min, expected_rating, marker):
    result = nec.size_feeder_ocpd(80.0, continuous)
    assert result.min_ocpd_a == pytest.approx(expected_min, abs=0.01)
    assert result.ocpd_rating_a == expected_rating
    assert marker in result.note


def test_full_chain_pv_source_ocpd_to_egc_to_conduit():
    ocpd = nec.size_pv_source_ocpd(11.0)
    assert ocpd.ocpd_rating_a == 20
    assert ocpd.egc_gauge == "12 AWG"
    conduit = nec.size_for_circuit("10 AWG", 2, ocpd.egc_gauge, nec.PVC_SCH40)
    assert conduit.success is True
    assert conduit.trade_size == "1/2"
    assert conduit.total_conductors == 3
    assert conduit.fill_pct < 40.0


@pytest.mark.parametrize("phase", [0, 2, -1, 4])
def test_voltage_drop_ac_refuses_unknown_phase(phase):
    with pytest.raises(ValueError):
        nec.voltage_drop_ac(10.0, 100.0, 0.5, 0.1, 1.0, phase, 240.0)


def test_voltage_drop_dc_and_citation_renderers():
    citation = nec.voltage_drop(10.0, 100.0, 1.21, 400.0)
    assert citation.units == "% (3% recommended max)"
    assert citation.result == pytest.approx(2.0 * 10.0 * 100.0 * 1.21 / 1000.0 / 400.0 * 100.0)
    assert "\u03c1" in citation.formula
    assert "Source: " in citation.to_multi_line()


def test_max_string_length_fails_closed_on_sentinel_cold_voc():
    citation = nec.max_string_length_690_7a3(0.0, 1000.0)
    assert citation.result == 0
    assert citation.rejected_alternatives[0].would_have_resulted_in == 0
    good = nec.max_string_length_690_7a3(50.0, 1000.0)
    assert good.result == 20
    assert good.rejected_alternatives[0].would_have_resulted_in == 20


def test_mark_synthetic_is_idempotent():
    citation = nec.continuous_current_690_8a(10.0, ["bvoc", "bvoc", "", None])
    assert citation.synthetic_input_names == ["bvoc"]
    assert citation.has_synthetic_inputs is True
    assert citation.result == pytest.approx(12.5)


# --------------------------------------------------------------------------- #
# 3. The licensed DEMO probes, reproduced by computation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(VDROPAC_EXPECTED))
def test_vdropac_probe_reproduced(name):
    record = by_name(probes.build("leafvdropac"))[name]
    glyph, tail, result = VDROPAC_EXPECTED[name]
    assert record["Formula"] == VDROP_PREFIX.format(glyph) + tail
    assert record["Result"] == result
    assert record["Article"] == "NEC 210.19(A)(4) Informational Note 4 (AC)"
    assert record["Units"] == "% (3% recommended max)"
    assert record["ShortDescription"] == (
        "AC voltage drop (%d-phase, recommended <= 3%% for inverter output)" % record["Phase_in"])
    # S5 of the plugin's own validator: exactly four middle dots per formula.
    assert record["Formula"].count("\u00b7") == 4


@pytest.mark.parametrize("name", sorted(AMPCORR_EXPECTED))
def test_ampcorr_probe_reproduced(name):
    record = by_name(probes.build("leafampcorr"))[name]
    tail, result, rejected = AMPCORR_EXPECTED[name]
    assert record["Formula"] == AMPCORR_PREFIX + tail
    assert record["Result"] == result
    assert record["RejectedResult"] == rejected
    assert record["Article"] == "NEC 310.16"
    assert record["Units"] == "A"
    assert record["Formula"].count("\u00d7") == 4


@pytest.mark.parametrize("name", sorted(MAXFILL_EXPECTED))
def test_maxfill_probe_reproduced(name):
    record = by_name(probes.build("leafmaxfill"))[name]
    assert record["Fill"] == MAXFILL_EXPECTED[name]
    assert isinstance(record["Fill"], float)
    assert isinstance(record["N"], int)


@pytest.mark.parametrize("name", sorted(FEEDEROCPD_EXPECTED))
def test_feederocpd_probe_reproduced(name):
    record = by_name(probes.build("leaffeederocpd"))[name]
    min_ocpd, rating, egc, note = FEEDEROCPD_EXPECTED[name]
    assert record["MinOcpdA"] == min_ocpd
    assert record["OcpdRatingA"] == rating
    assert record["EgcGauge"] == egc
    assert record["Note"] == note


@pytest.mark.parametrize("name", sorted(ONELINER_EXPECTED))
def test_oneliner_probe_reproduced(name):
    record = by_name(probes.build("leafoneliner"))[name]
    assert record["OneLiner"] == ONELINER_EXPECTED[name]
    # I13 of the plugin's validator: the three structural separators.
    for separator in (" \u2014 ", ": ", " = "):
        assert separator in record["OneLiner"]


@pytest.mark.parametrize("label", sorted(CONDUITFILL_MAXFILL_EXPECTED))
def test_conduitfill_maxfill_probe_reproduced(label):
    record = by_name(probes.build("leafconduitfill")["maxFill"], "label")[label]
    assert record["csharpFraction"] == CONDUITFILL_MAXFILL_EXPECTED[label]


@pytest.mark.parametrize("label", sorted(CONDUCTOR_AREA_EXPECTED))
def test_conduitfill_conductor_area_probe_reproduced(label):
    record = by_name(probes.build("leafconduitfill")["conductorArea"], "label")[label]
    assert record["csharpArea"] == CONDUCTOR_AREA_EXPECTED[label]


@pytest.mark.parametrize("label", sorted(CONDUIT_AREA_EXPECTED))
def test_conduitfill_conduit_area_probe_reproduced(label):
    record = by_name(probes.build("leafconduitfill")["conduitArea"], "label")[label]
    assert record["csharpArea"] == CONDUIT_AREA_EXPECTED[label]


@pytest.mark.parametrize("label", sorted(SIZE_CONDUIT_EXPECTED))
def test_conduitfill_size_conduit_probe_reproduced(label):
    record = by_name(probes.build("leafconduitfill")["sizeConduit"], "label")[label]
    expected = SIZE_CONDUIT_EXPECTED[label]
    actual = (record["success"], record["tradeSize"], record["conductorAreaSqIn"],
              record["conduitAreaSqIn"], record["fillPct"], record["maxFillPct"],
              record["totalConductors"], record["failureReason"])
    assert actual == expected


def test_probe_order_is_the_scenario_list_order():
    """Probe ORDER is part of the output; several DEMO validators assert it."""
    assert [r["Name"] for r in probes.build("leafvdropac")] == \
        [s[0] for s in probes.VDROPAC_SCENARIOS]
    assert [r["Name"] for r in probes.build("leafampcorr")] == \
        [s[0] for s in probes.AMPCORR_SCENARIOS]
    assert [r["Name"] for r in probes.build("leafmaxfill")] == \
        [s[0] for s in probes.MAXFILL_SCENARIOS]
    assert [r["Name"] for r in probes.build("leaffeederocpd")] == \
        [s[0] for s in probes.FEEDEROCPD_SCENARIOS]
    assert [r["Name"] for r in probes.build("leafoneliner")] == \
        [s[0] for s in probes.ONELINER_SCENARIOS]
    payload = probes.build("leafconduitfill")
    assert list(payload) == ["maxFill", "conductorArea", "conduitArea", "sizeConduit"]
    assert [r["label"] for r in payload["sizeConduit"]] == \
        [s[0] for s in probes.CONDUITFILL_SIZE_CONDUIT]


def test_probe_files_are_written_and_round_trip(tmp_path):
    for demo in sorted(probes.DEMOS):
        path = probes.write(demo, tmp_path)
        assert path.name == demo + "_probes.json"
        assert json.loads(path.read_text(encoding="utf-8")) == probes.build(demo)


def test_licensed_capture_reproduced():
    """Every probe in the six licensed files, when the capture is on this host.

    The capture is not committed to this repo, so a hermetic runner does not
    carry it. This test never skips: without the capture it asserts the capture
    is absent as a WHOLE (a half-present directory is a broken reference, not a
    hermetic runner) and the transcribed expectations above carry the parity.
    """
    names = {demo: REFERENCE_DIR / probes.file_name(demo) for demo in probes.DEMOS}
    present = [demo for demo, path in names.items() if path.is_file()]
    if len(present) != len(names):
        assert present == [], "licensed capture is partially present: " + repr(sorted(present))
        assert set(VDROPAC_EXPECTED) == {s[0] for s in probes.VDROPAC_SCENARIOS}
        assert set(FEEDEROCPD_EXPECTED) == {s[0] for s in probes.FEEDEROCPD_SCENARIOS}
        assert set(ONELINER_EXPECTED) == {s[0] for s in probes.ONELINER_SCENARIOS}
        return
    for demo, path in sorted(names.items()):
        licensed = json.loads(path.read_text(encoding="utf-8"))
        assert probes.build(demo) == licensed, demo


def _type_map(value, path=""):
    """Every leaf's JSON TYPE, keyed by path. 0 == 0.0 in Python but not in JSON."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out.update(_type_map(item, path + "/" + str(key)))
        return out
    if isinstance(value, list):
        out = {}
        for index, item in enumerate(value):
            out.update(_type_map(item, path + "/" + str(index)))
        return out
    return {path: type(value).__name__}


def test_generated_probe_value_types_are_the_plugin_s():
    """A probe that writes 0 where the plugin writes 0.0 is a parity failure.

    Equality alone cannot catch it: Python's 0 == 0.0 and False == 0. This walks
    every leaf and pins its type, against the licensed capture when it is here
    and against the shapes the six DEMO commands declare otherwise.
    """
    names = {demo: REFERENCE_DIR / probes.file_name(demo) for demo in probes.DEMOS}
    if all(path.is_file() for path in names.values()):
        for demo, path in sorted(names.items()):
            licensed = json.loads(path.read_text(encoding="utf-8"))
            assert _type_map(probes.build(demo)) == _type_map(licensed), demo
        return
    fractions = probes.build("leafmaxfill")
    assert {type(r["Fill"]).__name__ for r in fractions} == {"float"}
    assert {type(r["N"]).__name__ for r in fractions} == {"int"}
    feeder = probes.build("leaffeederocpd")
    assert {type(r["IsContinuous"]).__name__ for r in feeder} == {"bool"}
    assert {type(r["MinOcpdA"]).__name__ for r in feeder} == {"float"}
    sizing = probes.build("leafconduitfill")["sizeConduit"]
    assert {type(r["totalConductors"]).__name__ for r in sizing} == {"int"}
    assert {type(r["fillPct"]).__name__ for r in sizing} == {"float"}
