# assistant-contract: the assistant wire contract, written once

This package is the one source of truth for what the assistant server, the
worker and every client (web and Swift) exchange. It is written once in zod 4,
emitted as OpenAPI 3.1 and JSON Schema 2020-12, and pinned by seven golden
conversations that every consumer decodes at its pin (Part VI, K0; decisions
K-D3, K-D4, K-D5, K-D7, K-D8). zod is the only runtime dependency.

INTERNAL ONLY, never publish. Consumers vendor a sha256-pinned copy per K-D1.

## Files

- `src/reason.ts`: shared scalars (ids, UTC timestamps, digests, seq) and
  ReasonV1 with the reason table. Every reason offers one exit
  `next{label, action: retry|wait|refresh|open_settings|pair|none}`;
  `rate_limited` carries `retry_at`. The client authors text only for
  `client.offline` and `client.timeout` (`clientReason()`); the server may
  never send those two (`ServerReasonV1`). `ErrorV1` is every refusal body.
- `src/proposal.ts`: ProposalV1, kinds `control | build | config_draft | slot_promotion`.
- `src/card.ts`: ConfirmCardV1 and ApproveRequestV1 `{card_digest, request_id, presence?}`, with an optional PresenceAssertionV1.
- `src/thread.ts`: ThreadV1, ItemV1, BlockV1, ToolCallV1, ThreadEventV1
  (with `requires_action`), EventsPageV1, TurnRequestV1, WorkerEventV1 and
  GoldenConversationV1.
- `src/emit.ts`: the pure emitters for `schema/openapi.json` and
  `schema/assistant.schema.json`.
- `src/integration.ts`: strict, versioned connector, connection, credential
  reference, transform mapping, operation, dataset, query, read and dataset-element
  contracts shared by Studio and Ops.
- `fixtures/*.json` plus `fixtures/FIXTURES.sha256`: the seven golden
  conversations (idle, answer-with-view, tool-failed, card-approved,
  card-expired, rate-limited, worker-offline).

## The rules the decoder enforces

The `walk.resume` and `walk.side_quest` proposal kinds are produced only by the claudewalk concierge (AD6). Their fixture is `fixtures/proposals/walk.json`; the subdirectory keeps it out of the seven golden conversations and `FIXTURES.sha256`.

These are refusals at decode time, not advice to renderers.

- view, card, artifact and notice blocks are server-only: each requires
  `origin: "server"`, a user item carries text only, and a server item carries
  server blocks only.
- The worker may send only status, text, tool, error, card_ref and
  artifact_ref events (K-D3). Every variant is strict, so a worker cannot add
  `origin` or smuggle a card onto an allowed type; the events endpoint answers
  400 for anything WorkerEventV1 refuses.
- Every ConfirmCardV1 string carries its origin. Title, buttons and receipt are
  server-only. A field above Approve must be server-origin; model prose may sit
  only in the collapsed assistant's note. Approvals and declines name the
  actor; expiry and supersession do not.
- A view cites the tool invocations it was projected from, every row cites one
  of those, and each must be a completed invocation recorded earlier in the
  thread (K-D4). A failed tool carries its reason, so a failed read renders as
  failed. View titles may not use the consent vocabulary.
- Items are in strictly increasing seq order. In a golden conversation an item
  keeps the seq of the event that first delivered it, and folding the item
  events reproduces `thread.items` exactly.

## Integration contracts

`IntegrationRefV1` pins published dependencies by slug, positive version and
lowercase SHA256 digest. Draft definitions do not require their own digest.
Definitions are reusable and scope-free: the host registry binds tenant/project
ownership. Connector publication is descriptive, never an authorization grant.
Auth declares only a mechanism and, for API keys, a header name; secret values
remain in the host's credential store outside these documents.

`CredentialReferenceV1` contains only a UUID, purpose (`access_token`,
`refresh_token`, or `api_key`) and integer version from 1 through 2147483647.
`ConnectionV1` pins its connector and carries an owner UUID, default-port HTTPS
origin, 1..100 unique operation slugs, up to three credential references, provider
account metadata, a positive grant revision, lifecycle state and health. Credential
IDs (case-insensitive UUIDs) and purposes must each be unique. Origins cannot
contain credentials, paths (including `/`), queries or fragments. A verified
identity requires a nonempty provider account ID (at most 256 characters); revoked
state requires revoked health. Health check times are real UTC instants or null.
These records carry metadata, never permission: scope comes from the host's
authenticated envelope, never organization properties in this payload. The host
must resolve operation slugs against the pinned connector and verify provider
identity; schema validation does neither. Secret values, environment names,
credential URIs, file paths, key IDs and provider metadata extensions are refused.

`TransformMappingV1` describes the existing Ops mapping vocabulary: 1..100
unique `field_id` entries with a JSON `pointer` and an explicit conversion of
`identity`, `string_number` or `iso_instant`. Field IDs follow dataset field syntax
but additionally deny `__proto__`, `prototype` and `constructor`. Pointers allow
the empty root, require RFC 6901 escapes, and are bounded to 1024 characters and
32 decoded segments; the same reserved names are denied in decoded segments.
There is no code, expression evaluator, network access or model call in a mapping.
These schemas add definitions to both emitted documents without adding routes.

Dataset field `type` is a strict descriptor discriminated by `kind`. Money
declares currency and minor/major scale; ratio declares fraction/percent scale;
duration declares a unit; generic numbers may declare a display unit and
precision. Values are scalar JSON. Counts and minor-unit money use safe integers
(counts are nonnegative); other numeric values are finite. Nullable fields may
be absent or explicitly null; required fields must be present and non-null.
Null is preserved. Date-only and UTC instant values require real calendar dates.

Operation `pagination`, `idempotency`, and connector `auth` use `kind`
discriminators. JSON pointers follow RFC 6901, including the empty root pointer.
Provider idempotency and API-key descriptors use `header`. Only GET reads are
eligible for the Phase 1 generic runtime; declared POST reads are reserved for
a future verified adapter. GET does not imply a read effect. Unsafe writes with
no provider key may be described, but must remain disabled unless authority is
registered elsewhere. Schemas do not register authority or execute operations.

Both canonical documents retain object strictness, discriminated alternatives,
patterns, array/string bounds and numeric limits. Zod refinements additionally
enforce uniqueness (including normalized origins), real dates, IANA time zones,
decoded path traversal checks, valid default-port HTTPS origins, key/time field
references, effect/idempotency compatibility, reconciliation targets, and row
identity/type/nullability/status constraints, connection lifecycle/identity and
credential uniqueness checks, and mapping uniqueness/depth/reserved-name checks.
JSON Schema emission cannot
express these refinements; consumers must apply equivalent runtime checks.
The runtime must also check public network addresses, connection permissions,
query/filter/sort compatibility with registered dataset metadata, and recipe
field choices. Table/line recipes contain no executable expressions or URLs.

## Verify

```
npm --prefix packages/assistant-contract test
```

The suite checks schema round trips, the golden decode of every fixture, that
FIXTURES.sha256 recomputes, that the emitted OpenAPI and JSON Schema equal the
committed files, and the red-team refusals. On a mismatch it writes the freshly
emitted file to `<os tmpdir>/assistant-contract-emitted/` and names the path in
the failure; it never rewrites the tree on its own.

## Changing the contract

Edit the zod source, then regenerate the committed artifacts and review the
diff (PowerShell shown; in bash use `ASSISTANT_CONTRACT_WRITE=1 npm ...`):

```
$env:ASSISTANT_CONTRACT_WRITE = "1"; npm --prefix packages/assistant-contract test; Remove-Item Env:ASSISTANT_CONTRACT_WRITE
npm --prefix packages/assistant-contract test
```

FIXTURES.sha256 is `sha256sum -c` compatible and hashes LF-normalized bytes,
so a CRLF checkout recomputes the same pin. Consumers compare their vendored
copy against `git show <sha>:packages/assistant-contract/fixtures/...` at their
pin, never against a digest in their own repo (K-D8). A new event type is a
MAJOR change unless it travels as an unnamed frame with the type in the envelope.
