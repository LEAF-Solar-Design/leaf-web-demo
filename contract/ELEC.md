# `/api/elec` contract, DRAFT

Status: **DRAFT.** This schema is not frozen. The contract freeze ritual must
approve any breaking change before this route becomes a stable platform API.

`POST /api/elec` is the adapter-first electrical estimate endpoint. It accepts
only an immutable `drawing_id` and positive integer `dwg_version`. It resolves
that exact version through the drawing store. It does not accept paths, URLs,
or caller-provided intake files.

## Request

```json
{
  "drawing_id": "demo",
  "dwg_version": 1,
  "modules_per_string": 12,
  "string_count": 20,
  "module": {
    "watts": 550,
    "voc": 50,
    "vmp": 42,
    "isc": 10,
    "temperature_coefficient_pct_per_c": -0.3
  },
  "inverter": {
    "architecture": "central",
    "mppt_min_v": 300,
    "mppt_max_v": 800,
    "max_dc_input_a": 15,
    "design_min_temp_c": -10,
    "design_max_temp_c": 70
  },
  "rate_card": null,
  "expected_adapter_sha256": null
}
```

All request objects reject unknown fields. `expected_adapter_sha256`, when
provided, pins the reviewed local adapter source and fails closed if it differs.

## Response

Successful responses carry the standard envelope fields plus solver hashes,
the resolved drawing version and intake hash, electrical check rows, and the
engine tier `elec_calc-python`. Checks report actual passed and failed counts.
The Python tier is not a replacement for the Core workbook authority.

## Rate-card calibration, OPEN

No `$` per Wdc figure is embedded in this contract or implementation. If
`rate_card` is absent, `monetary` is a structured
`{"status":"not_calibrated",...}` response. A monetary amount is returned
only with explicit `rate_card.currency` and `rate_card.per_wdc` input. Rate
calibration and the pricing surface remain out of scope.
