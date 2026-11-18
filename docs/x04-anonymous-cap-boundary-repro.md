# Anonymous geometry bounds

Parent: Studio X-bench X04 raw drawing intake, anonymous-geometry product PR3261.

Source: ce199e80eb2e03a7bf0e8c30e93cd55ccb75c466. Offline synthetic DXF only, executed sequentially on leaf-svc through offhost.py --scope verify --no-local --fallback none. No customer drawings, network to staging, upload, deploy or full CI.

## Observed

59 and60 children retain all paths and complete=true.61 retains60, complete=false, no parseErrors, emission succeeds and counter still counts1/A1. Definition counts199 and200 retain both referenced boundary blocks.201 retains first200, reports blocksCapped201 and unresolved B1/*U200, drops that INSERT; only A1 remains selectable. Completeness describes retained definitions and is not a whole-drawing fidelity certificate.

```json
[
  {
    "boundary": "children",
    "complete": true,
    "declared_count": 59,
    "diagnostics": [],
    "emission_error": null,
    "emission_ok": true,
    "n": 59,
    "projected_paths": 59,
    "retained": 59,
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
    "boundary": "children",
    "complete": true,
    "declared_count": 60,
    "diagnostics": [],
    "emission_error": null,
    "emission_ok": true,
    "n": 60,
    "projected_paths": 60,
    "retained": 60,
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
    "boundary": "children",
    "complete": false,
    "declared_count": 61,
    "diagnostics": [],
    "emission_error": null,
    "emission_ok": true,
    "n": 61,
    "projected_paths": 60,
    "retained": 60,
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
    "all_retained_complete": true,
    "blocksCapped": null,
    "boundary": "definitions",
    "diagnostics": [],
    "emission_ok": true,
    "first_retained": true,
    "last_retained": true,
    "n": 199,
    "retained": 199,
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U0\"]"
      },
      {
        "count": 1,
        "handles": [
          "B1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U198\"]"
      }
    ]
  },
  {
    "all_retained_complete": true,
    "blocksCapped": null,
    "boundary": "definitions",
    "diagnostics": [],
    "emission_ok": true,
    "first_retained": true,
    "last_retained": true,
    "n": 200,
    "retained": 200,
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U0\"]"
      },
      {
        "count": 1,
        "handles": [
          "B1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U199\"]"
      }
    ]
  },
  {
    "all_retained_complete": true,
    "blocksCapped": 201,
    "boundary": "definitions",
    "diagnostics": [
      "INSERT B1: unresolved block reference *U200"
    ],
    "emission_ok": true,
    "first_retained": true,
    "last_retained": false,
    "n": 201,
    "retained": 200,
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U0\"]"
      }
    ]
  }
]
```

Captured result SHA-256: `8668bd7418f1302b52ade64d4b7b0b290e1baab30e075c8bb48f38e21d348679`. Original capture: `D:/agents/production-lanes-2026-11-10/XBENCH/status/X04-anon-repro-results.json`. Initial missing-three setup and overstrict numeric-threshold failures are retained separately; neither is represented as a passed run.

## Replay

Run the complete embedded synthetic recipe in [the transform reproduction](x04-anonymous-transform-repro.md#replay), then inspect the `caps` result section shown above. The recipe includes this exact case construction and uses the actual product functions.

This evidence does not establish live staging behavior or the ten held-out drawing grade.
