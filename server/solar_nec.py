#!/usr/bin/env python3
"""Studio's literal port of the licensed plugin's four NEC calculation engines.

Ported, function for function and string for string, from the Branch2025 plugin
source read at C:/tmp/solar-parity/wt-b25-s17 on 2026-09-22:

  * LeafSolarDesign.Core/Engineering/NecCitation.cs:62-68  (Fmt, "G6")
  * LeafSolarDesign.Core/Engineering/NecCitation.cs:75-97  (ToOneLiner)
  * LeafSolarDesign.Core/Engineering/NecCitation.cs:102-132 (ToMultiLine)
  * LeafSolarDesign.Core/Engineering/NecCitation.cs:140-338 (the 690.x / 310.16
    / DC voltage-drop citation factories)
  * LeafSolarDesign.Core/Engineering/NecCitation.cs:353-391 (VoltageDropAc)
  * LeafSolarDesign.Core/CableSizing/NecConduitFillEngine.cs:120-358
    (MaxFillFraction, Chapter 9 Tables 1/4/5, SizeConduit, SizeForCircuit,
    the lookups and ConduitTypeLabel)
  * LeafSolarDesign.Core/CableSizing/NecOcpdSizing.cs:40-171
    (NEC 240.6(A) standard sizes, the 250.122 EGC table, NextStandardOcpd,
    SizeEgc, SizePvSourceOcpd, SizeFeederOcpd)
  * LeafSolarDesign.Core/NecCableSizingEngine.cs:103-115 (WireLabels/WireUnits)

Contract of this module, so a later reader and a later completion condition on
it rather than guessing:

  * Every user-facing string is BYTE-IDENTICAL to the licensed plugin's output,
    including U+00D7 MULTIPLICATION SIGN, U+00B7 MIDDLE DOT, U+03C6 PHI,
    U+221A SQUARE ROOT, U+2014 EM DASH and U+2192 RIGHTWARDS ARROW.
  * Arithmetic is IEEE-754 double in the plugin's own evaluation order, so the
    results are bit-identical, not merely close. Never reassociate a product.
  * Number rendering goes through format_g6 / format_fixed / format_double,
    which reproduce C#'s "G6", "F<n>" and default double formatting under
    InvariantCulture, INCLUDING the away-from-zero tie rule that differs from
    Python's own round-half-to-even.
  * Invalid input fails exactly where the plugin fails: voltage_drop_ac raises
    on a phase outside {1, 3} (the plugin's ArgumentException), every lookup
    returns the plugin's 0 sentinel, and size_conduit returns an unsuccessful
    result carrying the plugin's exact FailureReason text.

TWO STRINGS DIFFER FROM THAT CHECKOUT ON PURPOSE. The licensed AutoCAD 2025
build captured on 2026-09-23 writes U+2014 EM DASH where wt-b25-s17's source
shows an ASCII hyphen, in NecCitation.ToOneLiner and in SizeFeederOcpd's Note.
The licensed capture is the reference this parity is measured against
(receipts/w3-demo-probes-20260923/leafoneliner_probes.json line 12 and
leaffeederocpd_probes.json line 11), and OneLinerDemoCommand.cs:19 documents
the contract as "\u2014" as well, so the em dash is the plugin's real output and
the checkout is the drifted copy. size_conduit's Note carries no such evidence:
no probe file records it, so it is ported verbatim from the checkout and is
marked UNVERIFIED at its definition below.

No AutoCAD, no network, no dependencies outside the standard library.
"""

from __future__ import annotations

from decimal import Decimal, localcontext, ROUND_HALF_UP
import math

# Exact binary expansion of a double can need ~1080 significant digits, so every
# rendering below runs in a context wide enough that quantize never overflows.
_DECIMAL_PRECISION = 1200
# C#'s default double.ToString() under .NET Framework is "G15"; .NET Core widens
# it to the shortest round-trippable form. max(shortest, 15) satisfies both, and
# every value this module renders that way (an OCPD rating) is far inside it.
_DEFAULT_MIN_DIGITS = 15

EM_DASH = "\u2014"
ARROW = "\u2192"
TIMES = "\u00d7"
MIDDLE_DOT = "\u00b7"
PHI = "\u03c6"
SQRT = "\u221a"


# --------------------------------------------------------------------------- #
# C# number formatting, InvariantCulture. No allocation-heavy paths; these are
# called a few times per probe, never in a loop over a drawing.
# --------------------------------------------------------------------------- #
def _trim_fraction(text):
    """Drop the trailing zeros C#'s G format never prints."""
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".")


def _non_finite(value):
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return None


def format_fixed(value, decimals):
    """C# double.ToString("F<decimals>", InvariantCulture).

    Ties round AWAY FROM ZERO (ROUND_HALF_UP over the double's exact binary
    value), which is C#'s rule and NOT Python's round-half-to-even. Getting this
    wrong silently changes "15.6" sized notes into "15.5" ones.
    """
    special = _non_finite(value)
    if special is not None:
        return special
    if decimals < 0:
        raise ValueError("decimals must be nonnegative")
    with localcontext() as ctx:
        ctx.prec = _DECIMAL_PRECISION
        quantum = Decimal(1).scaleb(-decimals)
        return format(Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP), "f")


def _format_general(value, precision):
    """C# double.ToString("G<precision>", InvariantCulture)."""
    special = _non_finite(value)
    if special is not None:
        return special
    if precision < 1:
        raise ValueError("precision must be positive")
    if value == 0.0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    with localcontext() as ctx:
        ctx.prec = _DECIMAL_PRECISION
        exact = Decimal(value)
        sign = "-" if exact < 0 else ""
        exact = abs(exact)
        exponent = exact.adjusted()
        quantum = Decimal(1).scaleb(-(precision - 1))
        mantissa = exact.scaleb(-exponent).quantize(quantum, rounding=ROUND_HALF_UP)
        if mantissa >= 10:
            # 9.9999996 at G6 carries into 10.0; renormalize instead of printing it.
            mantissa = mantissa.scaleb(-1).quantize(quantum, rounding=ROUND_HALF_UP)
            exponent += 1
        # C#: fixed-point when the exponent is greater than -5 and less than the
        # precision specifier; scientific otherwise, with an uppercase E and a
        # signed, at-least-two-digit exponent.
        if -5 < exponent < precision:
            return sign + _trim_fraction(format(mantissa.scaleb(exponent), "f"))
        digits = _trim_fraction(format(mantissa, "f"))
        return sign + digits + "E" + ("+" if exponent >= 0 else "-") + "%02d" % abs(exponent)


def format_g6(value):
    """NecCitation.Fmt: v.ToString("G6", InvariantCulture) (NecCitation.cs:62-68)."""
    return _format_general(value, 6)


def _significant_digits(value):
    text = repr(abs(float(value)))
    mantissa = text.split("e")[0].replace(".", "").lstrip("0").rstrip("0")
    return max(len(mantissa), 1)


def format_double(value):
    """C# double.ToString(InvariantCulture) with no format specifier."""
    special = _non_finite(value)
    if special is not None:
        return special
    if value == 0.0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    return _format_general(value, max(_significant_digits(value), _DEFAULT_MIN_DIGITS))


# --------------------------------------------------------------------------- #
# NecCitation (Engineering/NecCitation.cs)
# --------------------------------------------------------------------------- #
SOURCE_URL = "https://www.nfpa.org/codes-and-standards/nfpa-70"


class RejectedAlternative:
    """NecCitation.cs:394-399."""

    __slots__ = ("description", "would_have_resulted_in", "why_rejected")

    def __init__(self, description, would_have_resulted_in, why_rejected):
        self.description = description
        self.would_have_resulted_in = would_have_resulted_in
        self.why_rejected = why_rejected


class NecCitation:
    """One engineering decision in the plugin's defensible citation form.

    Pure data plus the two renderers and the factory methods. Field names are
    the C# property names lowercased; the RENDERED text is what parity measures.
    """

    __slots__ = ("article", "short_description", "formula", "inputs", "result",
                 "units", "source_url", "rejected_alternatives",
                 "has_synthetic_inputs", "synthetic_input_names")

    def __init__(self, article=None, short_description=None, formula=None,
                 inputs=None, result=0.0, units=None, source_url=SOURCE_URL,
                 rejected_alternatives=None, has_synthetic_inputs=False,
                 synthetic_input_names=None):
        self.article = article
        self.short_description = short_description
        self.formula = formula
        self.inputs = dict(inputs) if inputs else {}
        self.result = result
        self.units = units
        self.source_url = source_url
        self.rejected_alternatives = list(rejected_alternatives) if rejected_alternatives else []
        self.has_synthetic_inputs = has_synthetic_inputs
        self.synthetic_input_names = list(synthetic_input_names) if synthetic_input_names else []

    def mark_synthetic(self, names):
        """NecCitation.cs:48-60. Idempotent; duplicate names are de-duplicated."""
        if names is None:
            return self
        for name in names:
            if not name:
                continue
            if name not in self.synthetic_input_names:
                self.synthetic_input_names.append(name)
        if self.synthetic_input_names:
            self.has_synthetic_inputs = True
        return self

    def to_one_liner(self):
        """NecCitation.cs:75-97.

        "{Article} \u2014 {ShortDescription}: {Formula} = {G6(Result)}"
        + " {Units}"                    iff Units is neither None nor ""
        + " [SYNTHETIC: a, b]"          iff HasSyntheticInputs and names present

        The separator is U+2014 EM DASH: that is what the licensed AutoCAD 2025
        build emits (leafoneliner_probes.json) and what OneLinerDemoCommand.cs:19
        states as the contract. See this module's docstring for the drift.
        """
        parts = [self.article, " ", EM_DASH, " ", self.short_description, ": ",
                 self.formula, " = ", format_g6(self.result)]
        if self.units:
            parts.append(" ")
            parts.append(self.units)
        if self.has_synthetic_inputs and self.synthetic_input_names:
            parts.append(" [SYNTHETIC: ")
            parts.append(", ".join(self.synthetic_input_names))
            parts.append("]")
        return "".join(parts)

    def to_multi_line(self):
        """NecCitation.cs:102-132. AppendLine emits "\\r\\n" on Windows, which is
        what the plugin runs on, so this renderer uses CRLF line endings.

        UNVERIFIED against the licensed build: no DEMO probe file records this
        renderer, so its separator stays the checkout's ASCII hyphen. Only
        to_one_liner's em dash is evidenced.
        """
        nl = "\r\n"
        out = [self.article, " - ", self.short_description, nl,
               "Formula: ", self.formula, nl, "Inputs:", nl]
        for key, value in self.inputs.items():
            out += ["  ", key, " = ", format_g6(value), nl]
        out += ["Result: ", format_g6(self.result)]
        if self.units:
            out += [" ", self.units]
        out.append(nl)
        if self.rejected_alternatives:
            out += ["Rejected alternatives:", nl]
            for alt in self.rejected_alternatives:
                out += ["  - ", alt.description, " -> ",
                        format_g6(alt.would_have_resulted_in),
                        " (", alt.why_rejected, ")", nl]
        out += ["Source: ", self.source_url, nl]
        if self.has_synthetic_inputs and self.synthetic_input_names:
            out += ["WARNING - synthetic inputs (harness fallback, not real data): ",
                    ", ".join(self.synthetic_input_names)]
        return "".join(out)


def max_system_voltage_690_7a3(voc, beta_oc, t_min, max_system_v, synthetic_inputs=None):
    """NEC 690.7(A)(3) cold-temperature Voc. NecCitation.cs:140-162."""
    voc_cold = voc * (1.0 + beta_oc * (t_min - 25.0))
    citation = NecCitation(
        article="NEC 690.7(A)(3)",
        short_description="Maximum system voltage at coldest expected temperature",
        formula=("V_oc_cold = V_oc {t} (1 + beta_oc {t} (T_min - 25)) = {0} {t} (1 + ({1}) {t} ({2} - 25))"
                 .format(format_g6(voc), format_g6(beta_oc), format_g6(t_min), t=TIMES)),
        inputs={"V_oc": voc, "beta_oc": beta_oc, "T_min": t_min, "max_system_V": max_system_v},
        result=voc_cold,
        units="V",
    )
    return citation.mark_synthetic(synthetic_inputs)


def max_string_length_690_7a3(voc_cold, max_system_v, synthetic_inputs=None):
    """NEC 690.7(A)(3) integer string length. NecCitation.cs:171-210.

    Fails closed on the sentinel the upstream cold-Voc calc can return: a
    non-positive V_oc_cold yields length 0 rather than a division by zero.
    """
    if voc_cold <= 0:
        length = 0
        ceil_len = 0
    else:
        length = int(math.floor(max_system_v / voc_cold))
        ceil_len = int(math.ceil(max_system_v / voc_cold))
    citation = NecCitation(
        article="NEC 690.7(A)(3)",
        short_description="Maximum modules per string (cold-Voc limited)",
        formula="length = floor(max_system_V / V_oc_cold) = floor({0} / {1})".format(
            format_g6(max_system_v), format_g6(voc_cold)),
        inputs={"V_oc_cold": voc_cold, "max_system_V": max_system_v},
        result=float(length),
        units="modules",
        rejected_alternatives=[RejectedAlternative(
            "ceil(max_system_V / V_oc_cold)", float(ceil_len),
            "Ceil would exceed max system voltage in worst-case temperature.")],
    )
    return citation.mark_synthetic(synthetic_inputs)


def continuous_current_690_8a(isc, synthetic_inputs=None):
    """NEC 690.8(A) continuous current. NecCitation.cs:216-240."""
    citation = NecCitation(
        article="NEC 690.8(A)",
        short_description="Continuous current for PV source circuit",
        formula="I_continuous = I_sc {t} 1.25 = {0} {t} 1.25".format(format_g6(isc), t=TIMES),
        inputs={"I_sc": isc},
        result=isc * 1.25,
        units="A",
        rejected_alternatives=[RejectedAlternative(
            "I_sc with no 1.25 factor", isc,
            "NEC 690.8(A) requires the 125% factor for PV source circuits per the continuous-load definition.")],
    )
    return citation.mark_synthetic(synthetic_inputs)


def overcurrent_device_690_9b(i_continuous):
    """NEC 690.9(B) minimum OCPD rating. NecCitation.cs:246-259."""
    return NecCitation(
        article="NEC 690.9(B)",
        short_description="Minimum overcurrent device rating (cumulative 1.25 {t} 1.25 = 1.5625 vs I_sc)".format(t=TIMES),
        formula="I_ocpd = I_continuous {t} 1.25 = {0} {t} 1.25 (cumulative 1.5625 {t} I_sc)".format(
            format_g6(i_continuous), t=TIMES),
        inputs={"I_continuous": i_continuous},
        result=i_continuous * 1.25,
        units="A",
    )


def ampacity_correction_310_15b16(base_ampacity, temp_factor, conduit_factor):
    """NEC 310.16 ampacity correction. NecCitation.cs:274-302.

    The product is evaluated LEFT TO RIGHT exactly as the C# writes it; float
    multiplication is not associative, so regrouping breaks byte parity.
    """
    corrected = base_ampacity * temp_factor * conduit_factor
    return NecCitation(
        article="NEC 310.16",
        short_description="Conductor ampacity with temperature and conduit-fill correction",
        formula=("I_corrected = I_base {t} temp_factor {t} conduit_factor = {0} {t} {1} {t} {2}"
                 .format(format_g6(base_ampacity), format_g6(temp_factor),
                         format_g6(conduit_factor), t=TIMES)),
        inputs={"I_base": base_ampacity, "temp_factor": temp_factor, "conduit_factor": conduit_factor},
        result=corrected,
        units="A",
        rejected_alternatives=[RejectedAlternative(
            "I_base with no temperature correction", base_ampacity * conduit_factor,
            "Required when ambient exceeds 30\u00b0C.")],
    )


def voltage_drop(current, one_way_length_ft, resistivity_ohm_per_1000ft, source_voltage):
    """DC voltage drop, industry recommendation. NecCitation.cs:318-338."""
    vd_pct = (2.0 * current * one_way_length_ft * resistivity_ohm_per_1000ft / 1000.0 / source_voltage) * 100.0
    return NecCitation(
        article="Industry recommendation (NREL Best Practices)",
        short_description="DC voltage drop (industry recommendation: <= 3%; NEC 690 has no maximum DC VD requirement)",
        formula=("VD% = (2 {t} I {t} L {t} \u03c1 / 1000 / V) {t} 100 = (2 {t} {0} {t} {1} {t} {2} / 1000 / {3}) {t} 100"
                 .format(format_g6(current), format_g6(one_way_length_ft),
                         format_g6(resistivity_ohm_per_1000ft), format_g6(source_voltage), t=TIMES)),
        inputs={"I": current, "L_ft": one_way_length_ft,
                "rho_ohm_per_1000ft": resistivity_ohm_per_1000ft, "V_source": source_voltage},
        result=vd_pct,
        units="% (3% recommended max)",
    )


def voltage_drop_ac(current_a, one_way_length_ft, r_ohm_per_1000ft, x_ohm_per_1000ft,
                    power_factor, phase, source_voltage):
    """AC voltage drop with reactance, power factor and phase factor.

    NecCitation.cs:353-391. Fails closed on a phase outside {1, 3} rather than
    silently coercing it to single-phase (the plugin's R2C-9 decision, which
    raises ArgumentException); the power factor is CLAMPED to [0, 1] because a
    value outside it is physically meaningless.

    Formula glyphs: U+00D7 between factors, U+00B7 between R/X and cos/sin,
    U+03C6 phi, and U+221A only when phase == 3. sin(phi) renders as F3, every
    other number as G6.
    """
    if phase != 1 and phase != 3:
        raise ValueError("phase must be 1 or 3")
    if power_factor < 0:
        power_factor = 0
    if power_factor > 1:
        power_factor = 1
    cos_phi = power_factor
    sin_phi = math.sqrt(max(0.0, 1.0 - cos_phi * cos_phi))
    phase_factor = math.sqrt(3.0) if phase == 3 else 2.0
    v_drop = phase_factor * current_a * (r_ohm_per_1000ft * cos_phi + x_ohm_per_1000ft * sin_phi) \
        * one_way_length_ft / 1000.0
    vd_pct = (v_drop / source_voltage) * 100.0 if source_voltage > 0 else 0.0
    glyph = SQRT + "3" if phase == 3 else "2"
    template = ("VD% = ({0} {t} I {t} (R{d}cos{p} + X{d}sin{p}) {t} L / 1000) / V {t} 100 = "
                "({0} {t} {1} {t} ({2}{d}{3} + {4}{d}{5}) {t} {6} / 1000) / {7} {t} 100")
    return NecCitation(
        article="NEC 210.19(A)(4) Informational Note 4 (AC)",
        short_description="AC voltage drop ({0}-phase, recommended <= 3% for inverter output)".format(phase),
        formula=template.format(glyph, format_g6(current_a), format_g6(r_ohm_per_1000ft),
                                format_g6(cos_phi), format_g6(x_ohm_per_1000ft),
                                format_fixed(sin_phi, 3), format_g6(one_way_length_ft),
                                format_g6(source_voltage), t=TIMES, d=MIDDLE_DOT, p=PHI),
        inputs={"I": current_a, "L_ft": one_way_length_ft, "R_ohm_per_1000ft": r_ohm_per_1000ft,
                "X_ohm_per_1000ft": x_ohm_per_1000ft, "powerFactor": power_factor,
                "phase": float(phase), "V_source": source_voltage},
        result=vd_pct,
        units="% (3% recommended max)",
    )


# --------------------------------------------------------------------------- #
# Wire size catalog (NecCableSizingEngine.cs:103-115)
# --------------------------------------------------------------------------- #
WIRE_LABELS = ("14", "12", "10", "8", "6", "4", "3", "2", "1",
               "1/0", "2/0", "3/0", "4/0",
               "250", "300", "350", "400", "500", "600", "750", "1000")
WIRE_UNITS = ("AWG", "AWG", "AWG", "AWG", "AWG", "AWG", "AWG", "AWG", "AWG",
              "AWG", "AWG", "AWG", "AWG",
              "kcmil", "kcmil", "kcmil", "kcmil", "kcmil", "kcmil", "kcmil", "kcmil")

COPPER = "Copper"
ALUMINUM = "Aluminum"
CONDUCTOR_MATERIALS = (COPPER, ALUMINUM)


# --------------------------------------------------------------------------- #
# NecConduitFillEngine (CableSizing/NecConduitFillEngine.cs)
# --------------------------------------------------------------------------- #
EMT = "EMT"
PVC_SCH40 = "PvcSch40"
PVC_SCH80 = "PvcSch80"
RMC = "RMC"
LFNC_B = "LFNC_B"
# Enum declaration order IS the column order of ConduitArea.
CONDUIT_TYPES = (EMT, PVC_SCH40, PVC_SCH80, RMC, LFNC_B)

THWN2 = "THWN2"
XHHW2 = "XHHW2"
USE2 = "USE2"
BARE = "Bare"
# Enum declaration order IS the column order of ConductorArea.
INSULATION_TYPES = (THWN2, XHHW2, USE2, BARE)

TRADE_SIZES = ("1/2", "3/4", "1", "1-1/4", "1-1/2",
               "2", "2-1/2", "3", "3-1/2", "4")
TRADE_COUNT = 10
NIPPLE_FILL_FRACTION = 0.60

# NEC Chapter 9 Table 4, internal area in square inches. Row = trade size,
# column = conduit type. A 0 means the type does not offer that trade size.
#                    EMT      PvcSch40  PvcSch80  RMC      LFNC-B
CONDUIT_AREA = (
    (0.304, 0.285, 0.217, 0.314, 0.275),      # 1/2
    (0.533, 0.508, 0.409, 0.549, 0.494),      # 3/4
    (0.864, 0.832, 0.688, 0.887, 0.817),      # 1
    (1.496, 1.453, 1.237, 1.526, 1.410),      # 1-1/4
    (2.036, 1.986, 1.711, 2.071, 1.927),      # 1-1/2
    (3.356, 3.291, 2.874, 3.408, 3.195),      # 2
    (5.858, 4.695, 3.647, 4.866, 0),          # 2-1/2
    (8.846, 7.268, 5.541, 7.499, 0),          # 3
    (11.545, 9.737, 7.845, 10.010, 0),        # 3-1/2
    (15.901, 12.554, 10.255, 12.882, 0),      # 4
)

CONDUCTOR_SIZE_COUNT = 21
# NEC Chapter 9 Table 5, conductor area in square inches. Row = WIRE_LABELS
# index, column = insulation type. The Bare column is 0 for every kcmil size.
#                     THWN-2   XHHW-2   USE-2    Bare
CONDUCTOR_AREA = (
    (0.0097, 0.0085, 0.0097, 0.0042),         # 14 AWG
    (0.0133, 0.0117, 0.0133, 0.0066),         # 12 AWG
    (0.0211, 0.0176, 0.0211, 0.0106),         # 10 AWG
    (0.0366, 0.0322, 0.0366, 0.0167),         # 8 AWG
    (0.0507, 0.0459, 0.0507, 0.0266),         # 6 AWG
    (0.0824, 0.0726, 0.0824, 0.0423),         # 4 AWG
    (0.0973, 0.0869, 0.0973, 0.0530),         # 3 AWG
    (0.1158, 0.1035, 0.1158, 0.0670),         # 2 AWG
    (0.1562, 0.1399, 0.1562, 0.0845),         # 1 AWG
    (0.1855, 0.1676, 0.1855, 0.1066),         # 1/0 AWG
    (0.2223, 0.2027, 0.2223, 0.1344),         # 2/0 AWG
    (0.2679, 0.2463, 0.2679, 0.1694),         # 3/0 AWG
    (0.3237, 0.2996, 0.3237, 0.2136),         # 4/0 AWG
    (0.3970, 0.3700, 0.3970, 0),              # 250 kcmil
    (0.4608, 0.4290, 0.4608, 0),              # 300 kcmil
    (0.5242, 0.4880, 0.5242, 0),              # 350 kcmil
    (0.5863, 0.5460, 0.5863, 0),              # 400 kcmil
    (0.7073, 0.6619, 0.7073, 0),              # 500 kcmil
    (0.8676, 0.8163, 0.8676, 0),              # 600 kcmil
    (1.0496, 0.9887, 1.0496, 0),              # 750 kcmil
    (1.3478, 1.2748, 1.3478, 0),              # 1000 kcmil
)


class ConduitConductor:
    """NecConduitFillEngine.cs:63-85."""

    __slots__ = ("gauge", "gauge_unit", "insulation")

    def __init__(self, gauge=None, gauge_unit=None, insulation=THWN2):
        self.gauge = gauge
        self.gauge_unit = gauge_unit
        self.insulation = insulation

    @staticmethod
    def parse(gauge_label, insulation=THWN2):
        """Parse "10 AWG" or "250 kcmil". None for null/blank, the plugin's sentinel."""
        if gauge_label is None or not gauge_label.strip():
            return None
        parts = gauge_label.strip().split(" ")
        return ConduitConductor(gauge=parts[0],
                                gauge_unit=parts[1] if len(parts) > 1 else "AWG",
                                insulation=insulation)


class ConduitSizingResult:
    """NecConduitFillEngine.cs:88-111. Defaults match the C# field defaults."""

    __slots__ = ("success", "trade_size", "conduit_type", "conduit_area_sq_in",
                 "conductor_area_sq_in", "fill_pct", "max_fill_pct",
                 "total_conductors", "failure_reason", "note")

    def __init__(self, conduit_type=EMT):
        self.success = False
        self.trade_size = None
        self.conduit_type = conduit_type
        self.conduit_area_sq_in = 0.0
        self.conductor_area_sq_in = 0.0
        self.fill_pct = 0.0
        self.max_fill_pct = 0.0
        self.total_conductors = 0
        self.failure_reason = None
        self.note = None


def max_fill_fraction(conductor_count):
    """NEC Chapter 9 Table 1. NecConduitFillEngine.cs:120-126.

    NOT monotone: it DIPS to 0.31 at two conductors. Returns a fraction, never a
    percentage, and 0 for a non-positive count.
    """
    if conductor_count <= 0:
        return 0
    if conductor_count == 1:
        return 0.53
    if conductor_count == 2:
        return 0.31
    return 0.40


def find_wire_index(gauge, gauge_unit=None):
    """NecConduitFillEngine.cs:325-334. -1 when unknown; the unit is not matched."""
    if gauge is None:
        return -1
    for index, label in enumerate(WIRE_LABELS):
        if label.lower() == gauge.lower():
            return index
    return -1


def find_trade_index(trade_size):
    """NecConduitFillEngine.cs:336-344. -1 when unknown."""
    if trade_size is None:
        return -1
    for index in range(TRADE_COUNT):
        if TRADE_SIZES[index].lower() == trade_size.lower():
            return index
    return -1


def lookup_conductor_area(gauge, gauge_unit, insulation):
    """NEC Ch9 Table 5 lookup. 0 for an unknown combination (the plugin's sentinel)."""
    index = find_wire_index(gauge, gauge_unit)
    if index < 0 or index >= CONDUCTOR_SIZE_COUNT:
        return 0
    if insulation not in INSULATION_TYPES:
        return 0
    return CONDUCTOR_AREA[index][INSULATION_TYPES.index(insulation)]


def lookup_conduit_area(trade_size, conduit_type):
    """NEC Ch9 Table 4 lookup. 0 for an unknown trade size or an absent column."""
    index = find_trade_index(trade_size)
    if index < 0:
        return 0
    return CONDUIT_AREA[index][CONDUIT_TYPES.index(conduit_type)]


def conduit_type_label(conduit_type):
    """NecConduitFillEngine.cs:347-358."""
    return {EMT: "EMT", PVC_SCH40: "PVC Sch 40", PVC_SCH80: "PVC Sch 80",
            RMC: "RMC", LFNC_B: "LFNC-B"}.get(conduit_type, str(conduit_type))


def size_conduit(conduit_type, conductors):
    """Smallest trade size whose fill stays inside NEC Ch9 Table 1.

    NecConduitFillEngine.cs:196-266. Fails closed with the plugin's exact
    FailureReason text on a null/empty list, an unknown conductor, or no trade
    size large enough. Conductor areas accumulate in the LIST'S OWN ORDER, which
    is what makes the running float sum bit-identical to the plugin's.

    The `note` text is the one string in this module NOT verified against the
    licensed build: no DEMO probe file records it, so it is ported verbatim from
    the wt-b25-s17 checkout (line 254-256), ASCII hyphen included. The two
    strings that ARE evidenced use U+2014 there; if a later capture shows this
    note does too, change it here and nowhere else.
    """
    result = ConduitSizingResult(conduit_type=conduit_type)
    if not conductors:
        result.failure_reason = "No conductors specified."
        return result

    total_area = 0.0
    count = 0
    for conductor in conductors:
        if conductor is None:
            continue
        area = lookup_conductor_area(conductor.gauge, conductor.gauge_unit, conductor.insulation)
        if area <= 0:
            result.failure_reason = ("Unknown conductor: " + str(conductor.gauge) + " "
                                     + (conductor.gauge_unit if conductor.gauge_unit else "AWG")
                                     + " (" + str(conductor.insulation) + ")")
            return result
        total_area += area
        count += 1

    if count == 0:
        result.failure_reason = "No valid conductors specified."
        return result

    result.conductor_area_sq_in = total_area
    result.total_conductors = count
    max_fill = max_fill_fraction(count)
    result.max_fill_pct = max_fill * 100.0

    column = CONDUIT_TYPES.index(conduit_type)
    for trade in range(TRADE_COUNT):
        conduit_area = CONDUIT_AREA[trade][column]
        if conduit_area <= 0:
            continue  # LFNC-B stops at 2"
        fill = total_area / conduit_area
        if fill <= max_fill:
            result.success = True
            result.trade_size = TRADE_SIZES[trade]
            result.conduit_area_sq_in = conduit_area
            result.fill_pct = fill * 100.0
            result.note = ("NEC Ch9 T1/T4 - {0} conductors in {1}\" {2}: fill {3}% \u2264 {4}% max"
                           .format(count, TRADE_SIZES[trade], conduit_type_label(conduit_type),
                                   format_fixed(result.fill_pct, 1),
                                   format_fixed(result.max_fill_pct, 0)))
            return result

    result.failure_reason = ("No {0} trade size (up to 4\") fits {1} conductors ({2} sq in total, {3}% max fill)."
                            .format(conduit_type_label(conduit_type), count,
                                    format_fixed(total_area, 4), format_fixed(max_fill * 100.0, 0)))
    return result


def size_for_circuit(conductor_gauge_label, current_carrying_count, egc_gauge_label,
                     conduit_type=PVC_SCH40, conductor_insulation=THWN2):
    """N current-carrying conductors of one gauge plus an optional bare EGC.

    NecConduitFillEngine.cs:272-296.
    """
    conductor = ConduitConductor.parse(conductor_gauge_label, conductor_insulation)
    if conductor is None:
        result = ConduitSizingResult(conduit_type=conduit_type)
        result.failure_reason = "Invalid conductor gauge: " + str(conductor_gauge_label)
        return result
    conductors = [conductor for _ in range(current_carrying_count)]
    if egc_gauge_label is not None and egc_gauge_label.strip():
        egc = ConduitConductor.parse(egc_gauge_label, BARE)
        if egc is not None:
            conductors.append(egc)
    return size_conduit(conduit_type, conductors)


# --------------------------------------------------------------------------- #
# NecOcpdSizing (CableSizing/NecOcpdSizing.cs)
# --------------------------------------------------------------------------- #
# NEC 240.6(A) standard fuse and inverse-time breaker ratings, amperes.
STANDARD_OCPD_SIZES = (
    15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90, 100,
    110, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450,
    500, 600, 700, 800, 1000, 1200, 1600, 2000, 2500, 3000,
    4000, 5000, 6000,
)

# NEC 250.122: (max OCPD amps, copper WIRE_LABELS index, aluminum index).
EGC_TABLE = (
    (15, 0, 1), (20, 1, 2), (30, 2, 3), (40, 2, 3), (60, 2, 3),
    (100, 3, 4), (200, 4, 5), (300, 5, 6), (400, 6, 8), (500, 7, 9),
    (600, 8, 10), (800, 9, 11), (1000, 10, 12), (1200, 11, 13),
    (1600, 12, 15), (2000, 13, 16), (2500, 15, 17), (3000, 16, 17),
    (4000, 17, 19), (5000, 19, 20), (6000, 19, 20),
)


class OcpdSizingResult:
    """NecOcpdSizing.cs:20-32."""

    __slots__ = ("continuous_current_a", "min_ocpd_a", "ocpd_rating_a", "egc_gauge", "note")

    def __init__(self, continuous_current_a, min_ocpd_a, ocpd_rating_a, egc_gauge, note):
        self.continuous_current_a = continuous_current_a
        self.min_ocpd_a = min_ocpd_a
        self.ocpd_rating_a = ocpd_rating_a
        self.egc_gauge = egc_gauge
        self.note = note


def next_standard_ocpd(minimum_amps):
    """Smallest NEC 240.6(A) size >= minimum_amps; the input above 6000 A.

    NecOcpdSizing.cs:83-92. Returning the input is the plugin's documented
    non-standard passthrough, not a failure.
    """
    for size in STANDARD_OCPD_SIZES:
        if size >= minimum_amps:
            return float(size)
    return minimum_amps


def size_egc(ocpd_amps, material=COPPER):
    """NEC 250.122 EGC gauge label, or None above the table's 6000 A limit.

    NecOcpdSizing.cs:99-112. None is the plugin's own sentinel and callers
    render it as "N/A"; it is never silently replaced by the largest row.
    """
    for max_ocpd, cu_index, al_index in EGC_TABLE:
        if ocpd_amps <= max_ocpd:
            index = cu_index if material == COPPER else al_index
            return WIRE_LABELS[index] + " " + WIRE_UNITS[index]
    return None


def size_pv_source_ocpd(isc_a, egc_material=COPPER):
    """NEC 690.9(B): OCPD >= Isc x 1.25 x 1.25, rounded up per 240.6(A).

    NecOcpdSizing.cs:121-142.
    """
    i_cont = isc_a * 1.25
    min_ocpd = i_cont * 1.25
    ocpd = next_standard_ocpd(min_ocpd)
    egc = size_egc(ocpd, egc_material)
    note = ("NEC 690.9(B) - Isc {0}A {t} 1.25 {t} 1.25 = {1}A {a} {2}A OCPD; EGC: {3} per NEC 250.122"
            .format(format_fixed(isc_a, 2), format_fixed(min_ocpd, 2), format_double(ocpd),
                    egc if egc is not None else "N/A", t=TIMES, a=ARROW))
    return OcpdSizingResult(i_cont, min_ocpd, ocpd, egc, note)


def size_feeder_ocpd(continuous_current_a, is_continuous=True, egc_material=COPPER):
    """NEC 215.3 / 210.20(A) feeder OCPD plus its EGC.

    NecOcpdSizing.cs:148-171. The separator is U+2014 EM DASH: that is what the
    licensed build emits (leaffeederocpd_probes.json line 11). The conditional
    " x 1.25 = <F1>A" segment is elided entirely for a non-continuous load, and
    a missing EGC renders as "N/A", never as an empty string.
    """
    min_ocpd = continuous_current_a * 1.25 if is_continuous else continuous_current_a
    ocpd = next_standard_ocpd(min_ocpd)
    egc = size_egc(ocpd, egc_material)
    nec_ref = "NEC 215.3" if is_continuous else "NEC 210.20(A)"
    segment = (" {t} 1.25 = {0}A".format(format_fixed(min_ocpd, 1), t=TIMES)) if is_continuous else ""
    note = ("{0} {e} {1}A{2} {a} {3}A OCPD; EGC: {4} per NEC 250.122"
            .format(nec_ref, format_fixed(continuous_current_a, 1), segment,
                    format_double(ocpd), egc if egc is not None else "N/A",
                    e=EM_DASH, a=ARROW))
    return OcpdSizingResult(continuous_current_a, min_ocpd, ocpd, egc, note)
