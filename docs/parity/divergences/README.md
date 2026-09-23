# Declared divergences

The plugin is the compatibility reference, not an infallible authority. Known plugin defects are
recorded here as deliberate divergences, and they are never reproduced to pass a differential.

A divergence is a claim about the PLUGIN: it says the plugin's behaviour on this capability is
wrong, that Studio does the correct thing instead, and that the comparator therefore reports a
small, known, exactly enumerated set of diffs. It is never a licence for Studio to be wrong. A
Studio bug is a Studio bug; it gets fixed, not declared.

## What the gate accepts

`scripts/solar_parity_status.py` counts a receipt whose comparator verdict is `fail` toward its
capability only when all four of these hold. Any one of them failing is a
`RECEIPT_DIVERGENCE_INVALID` finding that names the check that failed, never a silent pass.

1. The receipt carries a top-level `divergence` block with a `finding`, a non-empty
   `declared_diffs` list, and a one-line `summary`. Unknown keys inside the block are refused.
2. The comparator's `diffs` equal `declared_diffs` as SETS: no undeclared diff, and no declared
   diff missing. A defect that grows a new symptom stops passing until someone looks at it.
3. `finding` is a repo-relative path to a Markdown document committed under
   `docs/parity/divergences/`, and that file exists in the repo the gate runs from.
4. Every ordinary receipt rule still passes: the capability version is the one the ledger expects,
   synthetic or fallback fields are flagged, the plugin state is `committed`, and the run survived
   a save and reopen.

A `divergence` block on a passing receipt is an input error (exit 2): a divergence exists only to
explain a failing comparison. When a capability has both a clean passing receipt and a divergence
receipt, the clean pass wins and the capability is not reported as diverged.

## How it is reported

A capability settled by a divergence counts as passing, because it has met the spec's bar, and it
is also reported apart so nobody loses sight of it: `capabilities_diverged` and
`duty_rows_diverged` in the counts, a `divergences` list in the JSON output, and a "declared
divergences" section in the human report naming each capability with its finding path.

## Writing a finding

One Markdown file per defect, named for the capability and the defect
(`autofillrevert-handle-drift.md`). State, in this order:

- the plugin command and the version the defect was proven on, with the date;
- what the plugin does, what a correct implementation does, and why the plugin is the wrong one;
- the evidence: the fixture, the observed counts or output, and the plugin's own messages;
- the exact comparator diff strings the defect produces, matching `declared_diffs` character for
  character;
- what would retire the divergence: the plugin fix that makes this document obsolete.
