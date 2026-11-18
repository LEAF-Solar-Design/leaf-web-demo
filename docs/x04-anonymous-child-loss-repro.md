# Anonymous child retention

Parent: Studio X-bench X04 raw drawing intake, anonymous-geometry product PR3261.

Source: ce199e80eb2e03a7bf0e8c30e93cd55ccb75c466. Offline synthetic DXF only, executed sequentially on leaf-svc through offhost.py --scope verify --no-local --fallback none. No customer drawings, network to staging, upload, deploy or full CI.

## Observed

ARC and TEXT are supported reader/emitter children. ARC adds a projected path; TEXT remains an intake/emitted child but this block path projector does not draw it. Nested INSERT becomes OTHER, marks complete=false and is omitted by emission. No parseErrors are emitted for this unsupported child. Reread of the lossy emitted block reports complete=true. Normal counter still lists and counts its top-level INSERT1/A1 despite incomplete geometry. This is an existing fidelity boundary, not proof nested geometry survives.

```json
[
  {
    "case": "nested",
    "complete": false,
    "count": 2,
    "diagnostics": [],
    "emission": {
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
          "count": 1,
          "handles": [
            "A1"
          ],
          "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
        }
      ]
    },
    "kinds": [
      "LWPOLYLINE",
      "OTHER"
    ],
    "projected_paths": 1,
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      }
    ]
  },
  {
    "case": "arc",
    "complete": true,
    "count": 2,
    "diagnostics": [],
    "emission": {
      "blocks": {
        "*U1": {
          "children": 2,
          "complete": true,
          "kinds": [
            "LWPOLYLINE",
            "ARC"
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
          "count": 1,
          "handles": [
            "A1"
          ],
          "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
        }
      ]
    },
    "kinds": [
      "LWPOLYLINE",
      "ARC"
    ],
    "projected_paths": 2,
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      }
    ]
  },
  {
    "case": "text",
    "complete": true,
    "count": 2,
    "diagnostics": [],
    "emission": {
      "blocks": {
        "*U1": {
          "children": 2,
          "complete": true,
          "kinds": [
            "LWPOLYLINE",
            "TEXT"
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
          "count": 1,
          "handles": [
            "A1"
          ],
          "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
        }
      ]
    },
    "kinds": [
      "LWPOLYLINE",
      "TEXT"
    ],
    "projected_paths": 1,
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      }
    ]
  },
  {
    "case": "all",
    "complete": false,
    "count": 4,
    "diagnostics": [],
    "emission": {
      "blocks": {
        "*U1": {
          "children": 3,
          "complete": true,
          "kinds": [
            "LWPOLYLINE",
            "ARC",
            "TEXT"
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
          "count": 1,
          "handles": [
            "A1"
          ],
          "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
        }
      ]
    },
    "kinds": [
      "LWPOLYLINE",
      "OTHER",
      "ARC",
      "TEXT"
    ],
    "projected_paths": 2,
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      }
    ]
  }
]
```

Captured result SHA-256: `8668bd7418f1302b52ade64d4b7b0b290e1baab30e075c8bb48f38e21d348679`. Original capture: `D:/agents/production-lanes-2026-11-10/XBENCH/status/X04-anon-repro-results.json`. Initial missing-three setup and overstrict numeric-threshold failures are retained separately; neither is represented as a passed run.

## Replay

Run the complete embedded synthetic recipe in [the transform reproduction](x04-anonymous-transform-repro.md#replay), then inspect the `children` result section shown above. The recipe includes this exact case construction and uses the actual product functions.

This evidence does not establish live staging behavior or the ten held-out drawing grade.
