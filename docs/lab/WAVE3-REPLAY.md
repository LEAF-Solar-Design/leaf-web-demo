# Wave 3 offline readback

Capture six sanitized JSON projections from staging on the release candidate.
Keep them outside version control under `artifacts/lab-wave3`; never commit
these artifacts. Use Node 20 or later from the repository root. The checker
has no dependencies, subprocesses or network access. It reads only the six
named receipt files and this guide. It proves consistency of the supplied
observations, not their authenticity or the health of a live deployment.

Do not copy raw responses, logs, screenshots, credentials, personal data or
private identifiers into these files or this guide. Select only the fields
listed below. Do not retain document, run, request or account identifiers,
random request tokens, authorization data or free text. The same restrictions
apply to keys and nested strings. No extra fields are accepted.

## Common fields

Each file is one JSON object, at most 256 KiB, stored as a regular file rather
than a symbolic link. Include exactly `schema`, `sha`, `capturedAt`, `proofKey`,
`pass`, plus its listed fields. Set `sha` to the verified leaf-web-demo release
candidate commit, exactly 40 lowercase hexadecimal characters; do not use an
unverified branch tip or a placeholder. `capturedAt` is the actual capture time
in UTC ISO-8601 with seconds and optional fractional seconds, ending in `Z`.
Generate one fresh 32-character lowercase hexadecimal `proofKey` for this
capture session and reuse it in all six projections; it is a public correlation
marker, never a credential. Set `pass: true` only after the described observation
succeeds. Never manufacture a missing measurement or edit a failed observation
to make the checker pass. Capture all six against the same pinned graph revision.

## Capture projections

### replay.json — leaf.wave3-replay.v1

Use the completed `leaf.lab-replay.v1` receipt returned by
`server.lab_artifacts_git.enable_mirror()`. Inspect its per-document watermark,
heads and publication map privately for the chosen graph. Copy only `status`
as `complete`, the chosen watermark revision as `revision`, its published
revision as `publishedRevision` (or null if absent), the completed `last_commit`
as `headCommit`, and `divergent: false` after confirming replay is complete
without divergence. Keep the raw replay receipt private; the checker never
opens it. It does not contain a canonical digest or publication count.

### ledger.json — leaf.wave3-ledger.v1

Read `GET /api/lab/docs/{doc}/head` and the pinned
`GET /api/lab/docs/{doc}/revisions/{revision}`. Publish through
`POST /api/lab/docs/{doc}/publish` with the expected head, and reread the head
and publication metadata. Copy only `revision`, `publishedRevision` from the
head's `published_revision` (positive integer or null), `digest` from the
revision's canonical `sha256` (64 lowercase
hexadecimal characters), and `publicationCount` (nonnegative integer) from
the stored publication metadata. Do not substitute a hash of a response body;
the revision digest also appears in the `Lab-Sha256` commit trailer. If the
deployed API does not expose the count, obtain the count of `lab_publish_receipts`
in the capture's private scope through the authorized Lab ledger tool. Use that
same scope for the activation observation; absence is a pending proof, never a
guessed count. Do not use the replay ordinal as a publication count.

### run.json — leaf.wave3-run.v1

Save a graph containing at least one admitted tool node, then create a run
with `POST /api/lab/runs` referencing its stored document and graph revision.
Poll `GET /api/lab/runs/{run}` and its bounded event pages to completion.
Inspect the corresponding broker result through the authorized broker tool.
Copy only `revision`, canonical `digest`, positive integer `toolNodeCount`,
`runState: "succeeded"`, `brokerState: "succeeded"`, and `brokerDigest` from
the broker's pinned graph digest. Both digests are 64 lowercase hexadecimal
characters. Confirm an actual broker execution; a successful pure-only graph
or an enqueue acknowledgment does not establish this observation.

### activation.json — leaf.wave3-activation.v1

Create the published graph activation with `POST /api/lab/activations` and
inspect `GET /api/lab/activations`. Observe the custom-transform widget on
`/mini` after closing the editor, allowing the activation to produce its output.
Copy only `revision`, canonical `digest`, `publicationCount`, `activatedAt`,
`editorClosedAt`, `observedAt`, `widgetRendered: true`, and
`widgetSurface: "/mini"`. Record real UTC observation times satisfying
`activatedAt <= editorClosedAt < observedAt`. Confirm the widget renders the
activated revision's output after closure, rather than an editor-local preview.
Read the ledger publication count again at this observation; the activation
listing does not itself supply that count. Do not simply copy the earlier count
without confirming it is unchanged.

### relay.json — leaf.wave3-relay.v1

On an ops-dashboard preview, enable the bounded relay flags including
`STUDIO_LAB_RELAY` and inspect its relay stats and bounded read behavior.
Use `/api/leafhft/lab/read/kashbot.run.list`, the corresponding
`kashbot.run.detail` read, and intent status by id. Record the verified
ops-dashboard commit as `opsSha` (40 lowercase hexadecimal characters).
Copy only `opsSha`, `listOk`, `detailOk`, `intentByIdOk`, `opaqueHandlesOnly`,
`capsHeld`, and `fallbackPolicyOk`; every boolean must be true. Confirm list,
detail and intent reads succeed, Studio sees only opaque `h-<16 hex>` handles,
and response, page and polling bounds remain enforced. Confirm fallback occurs
only for disabled or unsupported answers with permission, and markets remains
on the legacy path. Never paste relay payloads or stats dumps into the proof.

### rollback.json — leaf.wave3-rollback.v1

Copy only `controls`, an array containing exactly one entry for each id below.
Each entry has exactly `id`, `method`, `exercised`, `restored`. For a live check
use `method: "live"`, `exercised: true`, `restored: true` only after checking
both the stop behavior and recovery. For a reviewed procedure that was not
executed use `method: "documented"`, `exercised: false`, `restored: false`.

| id | Capture procedure |
| --- | --- |
| worker-disable | R1: set `LEAF_LAB_WORKER_DISABLED=1`; restore the worker environment. |
| worker-pause | R2, mandatory live: `POST /api/lab/runs/worker/pause`, observe suspended work, then `/api/lab/runs/worker/resume` and observe progress. |
| run-cancel | R3, mandatory live: `POST /api/lab/runs/{run}/cancel`, observe terminal cancellation; start a fresh disposable run and confirm normal execution for recovery. |
| git-flip-back | R4: review or exercise `lab_artifacts_git.flip_back`; confirm ledger mode and the recovery procedure. |
| activation-pause | R5, mandatory live: `POST /api/lab/activations/{activation}/pause`, observe suspended scheduling, then the matching `/resume` and observe scheduling recovery. |
| activation-off | R6: the matching `/off` is terminal; recovery requires a fresh activation, never resuming the old one. |
| tool-nodes | R7: turn `LEAF_LAB_TOOL_NODES` off, check enforcement, then on and readmit as required. |
| image-downgrade | R8: follow the operator image downgrade procedure; retain additive tables and confirm recovery on the intended image. |
| surface-rail | R9: turn `LEAF_LAB_SURFACE_ENABLED` off, check route gating, then restore. |
| relay-gate | Turn `STUDIO_LAB_RELAY` off, confirm permitted fallback behavior, then restore bounded relay reads. |

The other seven controls may use documented procedures from the operator guide.
Review their stop and recovery steps before marking them documented. Coordinate
global controls with the staging operator. Use disposable work for cancellation
and terminal off; `restored` attests recovered service, not reversal of a terminal
state. Complete live exercises before taking the final consistent observations.

## Run and cite the proof

CI exercises embedded fixtures, including hostile input and missing-file cases:

```sh
node scripts/verify-lab-wave3.mjs --self-test
```

The release proof requires every captured file:

```sh
node scripts/verify-lab-wave3.mjs --receipts artifacts/lab-wave3 --require-receipts
```

The checker emits one JSON verdict line with fixed check codes and booleans.
Exit 0 means pass, or pending when receipts are optional and absent. Exit 1
means a present file or this guide failed validation; that takes precedence over
missing files. Exit 3 means required receipts are missing. Exit 2 with usage
means invalid arguments. Without `--require-receipts`, absent files are pending
and do not establish a release proof. Unknown and duplicate flags are errors;
`--self-test` cannot be combined with other flags.

Cite evidence only as `<path> sha256:<hex>`, using the actual SHA-256 of the
sanitized receipt bytes, with 64 lowercase hexadecimal characters replacing
the placeholder. Use paths such as `artifacts/lab-wave3/replay.json`; cite all
six files individually. Do not embed receipt contents, links to private
responses, screenshots, logs, identifiers or unhashed claims. The file hash
in a citation identifies the sanitized bytes; the receipt `sha` binds the
application candidate, and `digest` binds the graph revision.
