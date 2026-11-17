# Anonymous INSERT fan-out

Parent: Studio X-bench X04 raw drawing intake, anonymous-geometry product PR3261.

Source: ce199e80eb2e03a7bf0e8c30e93cd55ccb75c466. Offline synthetic DXF only, executed sequentially on leaf-svc through offhost.py --scope verify --no-local --fallback none. No customer drawings, network to staging, upload, deploy or full CI.

## Observed

One complete anonymous definition with10/100/1000 distinct hexadecimal INSERT handles returns exact counts10/100/1000. Every ordered highlight handle matches the generated physical identity sequence and source bytes remain unchanged. Times are single-run observations on leaf-svc, with tracemalloc active across reader/emitter/counter. Peak measures Python allocations, not RSS, Node memory, browser behavior or real DWG extraction. No timing threshold or performance acceptance is imposed.

```json
[
  {
    "all_handles_in_order": true,
    "count": 10,
    "count_seconds": 0.0004938559723086655,
    "emit_seconds": 0.00713763601379469,
    "emitted_bytes": 5277,
    "handles_sha256": "513f303a178b7068e796d1ad3700297441844bdf8fc28ab25851d84f42a2c21e",
    "input_bytes": 926,
    "inserts": 10,
    "original_bytes_unchanged": true,
    "parse_seconds": 0.0011973700020462275,
    "python_peak_bytes": 71797
  },
  {
    "all_handles_in_order": true,
    "count": 100,
    "count_seconds": 0.002729579049628228,
    "emit_seconds": 0.03874796099262312,
    "emitted_bytes": 18952,
    "handles_sha256": "da26b61737e217f56523f273d75b064c37ac618c0801662b689a07b396f649a1",
    "input_bytes": 7041,
    "inserts": 100,
    "original_bytes_unchanged": true,
    "parse_seconds": 0.012367515009827912,
    "python_peak_bytes": 383855
  },
  {
    "all_handles_in_order": true,
    "count": 1000,
    "count_seconds": 0.029615200008265674,
    "emit_seconds": 0.41690088395262137,
    "emitted_bytes": 156497,
    "handles_sha256": "270db266058d5de275f4e415d63dc2faba03455795f446900964e0e2adff0b39",
    "input_bytes": 68986,
    "inserts": 1000,
    "original_bytes_unchanged": true,
    "parse_seconds": 0.126136236009188,
    "python_peak_bytes": 4302155
  }
]
```

Captured result SHA-256: `8668bd7418f1302b52ade64d4b7b0b290e1baab30e075c8bb48f38e21d348679`. Original capture: `D:/agents/production-lanes-2026-11-10/XBENCH/status/X04-anon-repro-results.json`. Initial missing-three setup and overstrict numeric-threshold failures are retained separately; neither is represented as a passed run.

## Replay

Run the complete embedded synthetic recipe in [the transform reproduction](x04-anonymous-transform-repro.md#replay), then inspect the `profile` result section shown above. The recipe includes this exact case construction and uses the actual product functions.

This evidence does not establish live staging behavior or the ten held-out drawing grade.
