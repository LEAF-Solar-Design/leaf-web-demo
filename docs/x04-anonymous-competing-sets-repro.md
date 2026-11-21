# Anonymous competing sets

Parent: Studio X-bench X04 raw drawing intake, anonymous-geometry product PR3261.

Source: ce199e80eb2e03a7bf0e8c30e93cd55ccb75c466. Offline synthetic DXF only, executed sequentially on leaf-svc through offhost.py --scope verify --no-local --fallback none. No customer drawings, network to staging, upload, deploy or full CI.

## Observed

Two identical closed outlines retain distinct block-name set IDs. Selecting *U1 counts2 and highlights only A1,A2; selecting *U2 counts1 and highlights only B1. Union counts3. Explicit selection remains identical after emission and reread. Geometric overlap does not merge distinct physical INSERT handles.

```json
{
  "before": [
    {
      "count": 2,
      "handles": [
        "A1",
        "A2"
      ],
      "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
    },
    {
      "count": 1,
      "handles": [
        "B1"
      ],
      "set_id": "[\"INSERT\",\"Panels\",\"*U2\"]"
    }
  ],
  "input_sha256": "474d7f16ccb814ee2230c35c5791e42a67820522332332f970a1de64729d141c",
  "roundtrip": {
    "blocks": {
      "*U1": {
        "children": 1,
        "complete": true,
        "kinds": [
          "LWPOLYLINE"
        ]
      },
      "*U2": {
        "children": 1,
        "complete": true,
        "kinds": [
          "LWPOLYLINE"
        ]
      }
    },
    "ok": true,
    "selected": [
      {
        "count": 2,
        "handles": [
          "A1",
          "A2"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      },
      {
        "count": 1,
        "handles": [
          "B1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U2\"]"
      }
    ]
  },
  "union_count": 3,
  "union_handles": [
    "A1",
    "A2",
    "B1"
  ]
}
```

Captured result SHA-256: `8668bd7418f1302b52ade64d4b7b0b290e1baab30e075c8bb48f38e21d348679`. Original capture: `D:/agents/production-lanes-2026-11-10/XBENCH/status/X04-anon-repro-results.json`. Initial missing-three setup and overstrict numeric-threshold failures are retained separately; neither is represented as a passed run.

## Replay

Run the complete embedded synthetic recipe in [the transform reproduction](x04-anonymous-transform-repro.md#replay), then inspect the `competing` result section shown above. The recipe includes this exact case construction and uses the actual product functions.

This evidence does not establish live staging behavior or the ten held-out drawing grade.
