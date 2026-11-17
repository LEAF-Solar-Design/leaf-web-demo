# Anonymous name collisions

Parent: Studio X-bench X04 raw drawing intake, anonymous-geometry product PR3261.

Source: ce199e80eb2e03a7bf0e8c30e93cd55ccb75c466. Offline synthetic DXF only, executed sequentially on leaf-svc through offhost.py --scope verify --no-local --fallback none. No customer drawings, network to staging, upload, deploy or full CI.

## Observed

Duplicate literal BLOCK names silently use the last definition, so reordering width2/width8 changes rendered vertices while count1/A1 and emitted name stay unchanged. Case variants *U1 and *u1 remain separate reader/emitter/count identities, but viewer blockDefinitions normalizes names; both references render the last case variant. Reversing definitions makes both render the other outline. No collision diagnostic is produced. These observations expose ambiguous overwrite behavior; they are not a uniqueness guarantee.

```json
[
  {
    "blocks": {
      "*U1": [
        [
          1.0,
          2.0
        ],
        [
          3.0,
          2.0
        ],
        [
          3.0,
          3.0
        ],
        [
          1.0,
          3.0
        ]
      ]
    },
    "case": "duplicate_small_last",
    "diagnostics": [],
    "roundtrip_names": [
      "*U1"
    ],
    "roundtrip_selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      }
    ],
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      }
    ],
    "world": [
      [
        [
          100,
          200,
          0
        ],
        [
          102,
          200,
          0
        ],
        [
          102,
          201,
          0
        ],
        [
          100,
          201,
          0
        ]
      ]
    ]
  },
  {
    "blocks": {
      "*U1": [
        [
          1.0,
          2.0
        ],
        [
          9.0,
          2.0
        ],
        [
          9.0,
          3.0
        ],
        [
          1.0,
          3.0
        ]
      ]
    },
    "case": "duplicate_large_last",
    "diagnostics": [],
    "roundtrip_names": [
      "*U1"
    ],
    "roundtrip_selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      }
    ],
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      }
    ],
    "world": [
      [
        [
          100,
          200,
          0
        ],
        [
          108,
          200,
          0
        ],
        [
          108,
          201,
          0
        ],
        [
          100,
          201,
          0
        ]
      ]
    ]
  },
  {
    "blocks": {
      "*U1": [
        [
          1.0,
          2.0
        ],
        [
          3.0,
          2.0
        ],
        [
          3.0,
          3.0
        ],
        [
          1.0,
          3.0
        ]
      ],
      "*u1": [
        [
          1.0,
          2.0
        ],
        [
          9.0,
          2.0
        ],
        [
          9.0,
          3.0
        ],
        [
          1.0,
          3.0
        ]
      ]
    },
    "case": "case_variants",
    "diagnostics": [],
    "roundtrip_names": [
      "*U1",
      "*u1"
    ],
    "roundtrip_selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      },
      {
        "count": 1,
        "handles": [
          "B1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*u1\"]"
      }
    ],
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      },
      {
        "count": 1,
        "handles": [
          "B1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*u1\"]"
      }
    ],
    "world": [
      [
        [
          100,
          200,
          0
        ],
        [
          108,
          200,
          0
        ],
        [
          108,
          201,
          0
        ],
        [
          100,
          201,
          0
        ]
      ],
      [
        [
          100,
          200,
          0
        ],
        [
          108,
          200,
          0
        ],
        [
          108,
          201,
          0
        ],
        [
          100,
          201,
          0
        ]
      ]
    ]
  },
  {
    "blocks": {
      "*U1": [
        [
          1.0,
          2.0
        ],
        [
          3.0,
          2.0
        ],
        [
          3.0,
          3.0
        ],
        [
          1.0,
          3.0
        ]
      ],
      "*u1": [
        [
          1.0,
          2.0
        ],
        [
          9.0,
          2.0
        ],
        [
          9.0,
          3.0
        ],
        [
          1.0,
          3.0
        ]
      ]
    },
    "case": "case_variants_reversed",
    "diagnostics": [],
    "roundtrip_names": [
      "*u1",
      "*U1"
    ],
    "roundtrip_selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      },
      {
        "count": 1,
        "handles": [
          "B1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*u1\"]"
      }
    ],
    "selected": [
      {
        "count": 1,
        "handles": [
          "A1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*U1\"]"
      },
      {
        "count": 1,
        "handles": [
          "B1"
        ],
        "set_id": "[\"INSERT\",\"Panels\",\"*u1\"]"
      }
    ],
    "world": [
      [
        [
          100,
          200,
          0
        ],
        [
          102,
          200,
          0
        ],
        [
          102,
          201,
          0
        ],
        [
          100,
          201,
          0
        ]
      ],
      [
        [
          100,
          200,
          0
        ],
        [
          102,
          200,
          0
        ],
        [
          102,
          201,
          0
        ],
        [
          100,
          201,
          0
        ]
      ]
    ]
  }
]
```

Captured result SHA-256: `8668bd7418f1302b52ade64d4b7b0b290e1baab30e075c8bb48f38e21d348679`. Original capture: `D:/agents/production-lanes-2026-11-10/XBENCH/status/X04-anon-repro-results.json`. Initial missing-three setup and overstrict numeric-threshold failures are retained separately; neither is represented as a passed run.

## Replay

Run the complete embedded synthetic recipe in [the transform reproduction](x04-anonymous-transform-repro.md#replay), then inspect the `collisions` result section shown above. The recipe includes this exact case construction and uses the actual product functions.

This evidence does not establish live staging behavior or the ten held-out drawing grade.
