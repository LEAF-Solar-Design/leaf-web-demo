# k2: StringSizer's success path (string-sizer)

Captured 2026-09-24 on the VMC (release build 1.1.4.4) after aws-string-sizer 6ebe5a7e made the service return a JSON
object. Same form inputs as k1 (zip 78701, typed CEC module, Sungrow SG250HX, close mount, tilt 5, azimuth 180). The
result form showed 41 modules on the standard design simulation. Its Voc_cold check failed
(41 x 37.5 V = 1538 V > 1500 V), "Pick shorter length" set 39, and Close committed PanelsInSequence 39, VocColdPasses
true, VocColdPerModule 37.505023875, VocColdStringVoltage 1462.695931125 and VocColdMaxDcVoltage 1500.

`k2-intake.json` (request, settings before) and `k2-response.json` (the served bytes) come from the Branch2025 adapter
`tools/parity/w4_evidence.py` step k2. Studio's `string_sizer_outcome` ports the result form's rule (Voc_cold =
voc x (1 + bvoc / 100 x (mintemp - 25)), pass when n x Voc_cold <= string_design_voltage, pick shorter = floor(Vmax /
Voc_cold)). The receipt is `docs/parity/receipts/string-sizer/w4-k2.json` (pass, 0 diffs). The k1 receipt stays as the
record of the refused double-encoded response.

The response carries no `mintemp`, so the plugin computes Voc_cold at 0 C for every site (its model defaults the field
to zero) and Studio does the same; tracked as Solar residuals R28.
