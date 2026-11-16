# X04 portable offline evidence

Parent: Studio X-bench X04 raw drawing intake, track 2. The offline CLI exports
and validates portable synthetic JSON evidence using the format below.

Save the worked example as example.json and run from the worktree root.

```powershell
python scripts/export_x04_offline_evidence.py example.json --out evidence.json
python scripts/validate_x04_offline_evidence.py evidence.json
```

Both commands require the documented schema, synthetic marker and exact drawing
identity. Export recomputes the result through the trusted count tool and
retains literal observed dependencies. Validation reports forged counts,
highlights or byte-integrity claims. Invalid input exits2 with diagnostics on
stderr before writing JSON. The optional output is replaced atomically after
the complete artifact is encoded. Referenced dependency targets stay unopened.

An artifact carries its synthetic drawing identity and exact input entities,
the observed dependency report, explicit selected IDs, recomputed count,
canonical parent highlights and an unverified original-byte integrity status.
This keeps every result bound to the input needed to reproduce it. JSON does
not authenticate itself; treat received counts, selections and overlays as
claims and rerun the trusted count tool before using them.

Worked example with one six-module INSERT parent and one planar quad:

```json
{
  "schema": "leaf.x04.offline-evidence.v1",
  "synthetic": true,
  "drawing": {
    "id": "synthetic-east-v1",
    "intake": {
      "inserts": [
        {
          "layer": "East",
          "name": "Panel",
          "handle": "A",
          "rows": 2,
          "columns": 3
        }
      ],
      "polylines": [
        {
          "layer": "East",
          "handle": "B",
          "closed": true,
          "pts": [
            [
              0,
              0
            ],
            [
              2,
              0
            ],
            [
              2,
              1
            ],
            [
              0,
              1
            ]
          ]
        }
      ]
    },
    "dependency_pairs": [
      [
        0,
        "STYLE"
      ],
      [
        3,
        "notes.shx"
      ]
    ]
  },
  "observed_dependencies": {
    "scope": "local_dxf_records",
    "dependencies": [
      {
        "kind": "font",
        "name": "notes.shx",
        "disposition": "font remapped",
        "remapped_to": "sans-serif"
      }
    ],
    "external_files_opened": false,
    "coverage_note": "Lists dependency records preserved in the local DXF conversion; raw DWG metadata coverage requires an independent inventory.",
    "schema": "leaf.drawing-dependencies.v1"
  },
  "request": {
    "drawing_id": "synthetic-east-v1",
    "set_ids": [
      "[\"INSERT\",\"East\",\"Panel\"]",
      "[\"LWPOLYLINE\",\"East\",\"\"]"
    ]
  },
  "result": {
    "count": 7,
    "highlight_handles": [
      "A",
      "B"
    ],
    "original_bytes_integrity": "unverified"
  }
}
```

The font row is recomputed from drawing.dependency_pairs embedded in the artifact.
Both CLIs require decoded records and reject contradictory dependency rows or coverage.
Changing all embedded inputs consistently creates a new synthetic claim.
An artifact provides consistency evidence without authenticating its source.

Validation checklist:

- Require the schema, synthetic marker and matching drawing/request identity.
- Preserve exact reference strings and distinct kinds without opening targets.
- List candidates from entities, then verify every requested ID against that listing.
- Recompute counts and overlays from entities; ignore supplied derived claims.
- Compare weighted physical unions and unique canonical parent handles exactly.
- Keep original-byte integrity unverified unless independent retained bytes and a trusted digest are actually checked.
- Keep conversion coverage, offline behavior and real browser/staging acceptance separate.

Focused offline checks are test_x04_untrusted_derived_counts.py,
test_x04_bigfont_dependencies.py and test_x04_overlay_replay_consistency.py.
The selector-payload and handle-alias-pressure benchmarks assert exact outcomes
without timing gates. No artifact from these examples contains customer data.
