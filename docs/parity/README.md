# Solar parity: the ledger, the receipts, and the oracle

This folder holds the done-check for the Branch2025 to Studio parity program. One
question is asked and answered here: for a given wave, does Leaf Automation Studio
do what the Branch2025 plugin does, with evidence.

Three pieces:

- `solar-ledger.json`, one row per Branch2025 command surface registration,
  classified once and assigned to a delivery wave.
- `solar-ledger.schema.json`, the JSON Schema (draft 2020-12) for that file.
- `receipts/`, one comparator receipt per capability and fixture. See
  `receipts/README.md`.

The oracle is `scripts/solar_parity_status.py`. It reads both and exits
0 pass, 1 parity not met, 2 the input could not be trusted.

```
python scripts/solar_parity_status.py                      # full report, all production rows
python scripts/solar_parity_status.py --require w1         # wave 1 and everything before it
python scripts/solar_parity_status.py --require all-production --json
python scripts/solar_parity_status.py --ledger PATH --receipts DIR
```

Today the ledger ships with zero rows against `registrations_expected: 394`, so the
oracle exits 1 with `ROW_COUNT`. That is the intended starting state: the ledger
lane fills the rows, and the count is the first thing that has to close.

## The ledger

Top level: `schema`, `registrations_expected`, `source` (how that count was
produced, so it can be reproduced), and `rows`.

Row fields, all validated by the script before any verdict is formed:

| Field | Meaning |
| --- | --- |
| `global` | the registration's global identifier, unique across the ledger |
| `class` | `T` tool, `F` feature, `P` platform-only, `V` view-only, `H` host-bound, `A` alias |
| `maturity` | `production`, `preview`, `tutorial`, `internal` |
| `capability` | kebab-case capability name, shared by rows that prove out together |
| `capability_version` | bump it when observable behaviour changes; every older receipt goes stale |
| `family`, `interaction` | grouping and how the user reaches it |
| `engine` | `browser`, `cloud-service`, `server-builtin`, `dotnet-worker`, `autocad-lane`, `platform`, `none` |
| `wave` | 0 to 5, or null for out of scope |
| `status` | `resolved` or `unresolved`; unresolved means the classification is still open |
| `exclusion_rationale` | required and non-empty for a `V`, `H` or `P` row with a null wave |
| `alias_of` | required for class `A`; names an existing row that is not itself an alias |
| `evidence` | where the classification came from: file, line, command output |

A row carries parity duty when its class is `T` or `F` and its maturity is
`production`. Nothing else owes a receipt. Several rows may share one capability;
the capability is proven once and reported once.

## What the oracle checks

Ledger level, over every row in every scope, each reported as a finding and worth
exit 1:

- `ROW_COUNT`: the row count does not match `registrations_expected`
- `DUPLICATE_GLOBAL`: a global appears twice
- `UNRESOLVED`: a row is still unresolved
- `ALIAS_DANGLING`: a class `A` row whose `alias_of` names no row, or names another alias
- `EXCLUSION_MISSING`: a `V`, `H` or `P` row with no wave and no rationale
- `WAVE_MISSING`: a production `T` or `F` row with no wave

Per capability, for duty rows inside the requested scope:

- `RECEIPT_MISSING`: nothing under `receipts/<capability>/`
- `RECEIPT_FAIL`: every receipt carries comparator verdict `fail`
- `RECEIPT_STALE`: the receipt names a version the ledger does not expect, or disagrees with its own `studio.capability_version`
- `RECEIPT_SYNTHETIC`: synthetic or fallback fields without `synthetic_flagged`
- `RECEIPT_NOT_COMMITTED`: produced against a plugin state other than `committed`
- `RECEIPT_NO_REOPEN`: `survived_reopen` is not true

One clean passing receipt settles a capability. A passing receipt that trips any
rule above does not count, and a failing receipt beside a clean one is harmless.

`--require wN` scopes the receipt checks to duty rows with `wave <= N`; the ledger
level checks always run over all rows. `--require all-production` and the default
report cover every duty row.

## Fail closed

Exit 2 means the answer is unknown, and unknown is never a pass. A missing file,
invalid JSON, an unknown enum value, an unknown field, a receipt filed under the
wrong capability, or an input past its ceiling all land there with one line on
stderr and no traceback. Ceilings: 5 MB and 5000 rows for the ledger, 2 MB per
receipt, 20000 receipt files.

The script is stdlib only, reads no network, writes nothing, and orders its
findings by code then name so two runs on one tree produce identical output.
