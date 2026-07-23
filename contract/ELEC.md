# `/api/elec` contract, DRAFT

Status: **DRAFT.** This schema is not frozen. The contract freeze ritual must
approve any breaking change before this route becomes a stable platform API.

`POST /api/elec` is the adapter-first electrical estimate endpoint. It accepts
only an immutable `drawing_id` and positive integer `dwg_version`. It resolves
that exact version through the drawing store. It does not accept paths, URLs,
or caller-provided intake files.

This endpoint is fail-closed. A check with missing inputs reports
`insufficient_input` or `requires_engineer_review`; it never reports a pass.
The Python tier is not a replacement for the Core workbook authority.

## Request

```json
{
  "drawing_id": "demo",
  "dwg_version": 1,
  "modules_per_string": 12,
  "string_count": 2,
  "module": {
    "watts": 550,
    "voc": 50,
    "vmp": 42,
    "isc": 10,
    "temperature_coefficient_pct_per_c": -0.3
  },
  "inverter": {
    "architecture": "central",
    "topology": "combined_input",
    "mppt_min_v": 300,
    "mppt_max_v": 800,
    "max_dc_voltage": 1000,
    "max_dc_input_a": 30,
    "optimizer_max_input_isc": null,
    "optimizer_max_input_voltage": null,
    "design_min_temp_c": -10,
    "design_max_temp_c": 70
  },
  "rate_card": null,
  "expected_adapter_sha256": "<reviewed 64-character lowercase SHA-256>"
}
```

All request objects reject unknown fields. `expected_adapter_sha256` is
required to receive electrical check results. An omitted digest returns a
structured `ADAPTER-SOURCE-PIN` `insufficient_input` result. A mismatched
digest fails closed before calculation.

### Central string-inverter inputs

`architecture` is `central`. `topology` must be one of:

- `per_string_inputs`: `max_dc_input_a` is a limit for each independently
  rated string input. The continuous-current check uses `Isc x 1.25`.
- `combined_input`: `max_dc_input_a` is a combined limit. The
  continuous-current check uses `string_count x Isc x 1.25`.

`max_dc_voltage` is required for the safety ceiling. Cold string Voc is
compared with it under NEC 690.7(A)(3). It is not interchangeable with
`mppt_max_v`, which remains a distinct MPPT-operating-window check.

### SolarEdge optimizer inputs

`architecture` is `solaredge` and `topology` must be
`optimizer_per_module`. `optimizer_max_input_voltage` is the verified
per-optimizer Absolute Maximum Input Voltage. Cold module Voc is compared
with that value. `optimizer_max_input_isc` is the optimizer datasheet input
rating. Bare module Isc is compared with that value.

The adapter does not infer SolarEdge output or bus current. It returns
`requires_optimizer_model` for generic OCPD rather than applying the central
module-Isc OCPD calculation.

## Response

Successful responses carry the standard envelope fields, solver hashes, the
resolved drawing version, electrical check rows, and engine tier
`elec_calc-python`. They do not include an `intake_sha256`: electrical inputs
are currently caller supplied, so an intake digest would falsely imply that it
proves the calculation inputs.

Each check has one of `pass`, `fail`, `insufficient_input`,
`requires_engineer_review`, or `requires_optimizer_model` in `status`.
`passed` is true only for `status: "pass"`. The check summary reports
`passed`, `failed`, and `needs_review` counts.

## Rate-card calibration, OPEN

No `$` per Wdc figure is embedded in this contract or implementation. If
`rate_card` is absent, `monetary` is a structured `not_calibrated` response. A
monetary amount is returned only with explicit `rate_card.currency` and
`rate_card.per_wdc` input. Rate calibration and the pricing surface remain out
of scope.
