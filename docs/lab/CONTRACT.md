# Lab contracts

The Lab connects blocks with data pipes. A pipe is the only way one block
depends on another. Card placement, Lane and display order add no dependencies.
These contracts describe data and validation. They do not grant permission to
run a block, sign a request or place a Test trade.

## Frozen standards

The Lab consumes three byte-exact inputs. Their provenance, sizes and SHA-256
values are recorded in `contract/fixtures/lab/SOURCES.json`:

| Frozen path | Standard | SHA-256 |
| --- | --- | --- |
| `contract/fixtures/lab/glossary.json` | STD-1 | `fd90f747d222a386243cb3d89c210f48f2fdc7f3f0f099dbb9e753caa47d4335` |
| `contract/fixtures/lab/leaf-block-manifest.v1.schema.json` | STD-5 | `a4b2c4a327294a03afd1f302473b959361595f865d3ec15a8df3fa8b76b40cd1` |
| `contract/fixtures/lab/lanes.json` | Nine Lanes | `586405de7277ac9e47cf291291623be143d67a894c2193a667609f9a6e25f3da` |

Use glossary display words such as Strategy, Market, Cash out, Bet size,
Risk level, Lane, Test and Keeper. Existing machine identifiers remain stable.
The frozen Lane identifiers are not display labels. Do not edit or normalize
the frozen copies, including line endings. The root `.gitattributes` already
marks the Lab fixture directory `-text`.

## Blocks and pins

`contract/lab-block.v1.schema.json` uses `allOf` with STD-5. It adds constraints
to the shared fields rather than declaring a competing manifest format. The
shared `schema_version` is the integer 1; the profile is `lab-registry`. There
is deliberately no additional Lab discriminator in a shared manifest.

A manifest names its immutable identifier, display title, semantic version and
code SHA-256. It declares typed inputs and outputs, settings and presentation
schemas, samples, p50/p99 latency budgets, Lane, effect, execution target,
residency, permissions, state and at least one Test identifier. Latency uses the
shared `legacy-us-to-std4-ns-ms-v1` profile: p50 must not exceed p99. Values stay
in microseconds; nanoseconds equal microseconds times 1000, and display
milliseconds equal microseconds divided by 1000.

Kinds are `builtin` (built-in), `settings` (settings-only), or `rust` (custom
Rust). All Lab blocks target `runner` with `tenant` residency. Custom Rust runs
inside the bot. Its code is registered separately, addressed by code hash;
code, scripts and executable extension fields never occur in a saved graph.
Extensions are new server-registry versions. The registry decides effect,
residency and permissions. A caller must compare a submitted manifest with
the full trusted registry record, not trust a record sent by the frame.

`contract/lab-pin.v1.schema.json` constrains the shared port definition. Each
pin names scalar or stream mode, a schema for one value or stream event, unit,
nullability, required/optional status and explicit default status. A declared
default must exist and validate; an undeclared default is refused. Null values
require the nullable flag. Samples contain one value or event per named pin.
Outputs must have samples; required inputs without defaults must have samples.

Compatibility is directional. Modes and units must match. A nullable output
cannot feed a nonnullable input. Identical schemas are compatible. For simple
scalar schemas, integer may widen to number; source bounds and enum choices
must be subsets of the destination. Other changes require exact schema equality.
There is no implicit unit conversion, stream collection or coercion. A required
input must have a pipe or declared default. At most one pipe feeds each input.

Embedded schemas use a bounded, data-only vocabulary: explicit `type`, `const`,
`enum`, `required`, `properties`, `additionalProperties`, `items`, numeric bounds,
array and string length bounds, `title` and `description`. Objects must name
their properties and close additional properties; arrays must type their items.
Nested schemas follow the same rules. References, formats and executable schema
keywords in registry settings or pins are refused. This differs from the fixed
contract schemas, which use references and composition. Presentation hints may
contain `order` and `labels`; hints never alter validation or authority.

`contract/fixtures/lab/blocks.json` contains illustrative trusted records for
all 13 block versions from Ops plus a custom Rust example. The adapter was read
from `ops-dashboard/src/lib/leafhft-blocks/catalog.ts` at local master commit
`50594041451dda5c6c72545c685a417cbf3e6527`. The version and setting bounds come
from `vendor/leafhft-block-catalog/catalog.json` at that commit. Its source
identifies `leafhft/blocks/codec.py`, commit `2476f534`. Version 1 becomes
`1.0.0`; settings preserve integer/float bounds and required/optional status.
Locked settings still need the trusted server's authorization checks. Titles
use the glossary. Code hashes, latency budgets and samples in these fixtures
are synthetic examples, not production code registrations or measurements.

## Saved graphs

`contract/lab-graph.v1.schema.json` identifies `leaf.lab-graph.v1`. A document
contains only `schema`, `blocks`, `pipes` and `outputs`. Each block has an instance
identifier, an exact registry reference (`id`, `version`, `code_sha256`) and
authored settings. Pipes and graph outputs refer to `{node, port}` endpoints.
There are at most 200 blocks and 400 pipes; the graph is at most 262144 UTF-8
bytes. Empty drafts are allowed. IDs are lowercase, hyphen-separated and bounded.

Validation also checks unique block IDs, known registry versions and code
hashes, settings against the pinned schema, known endpoints, compatible pins,
single input attachment, required inputs and an acyclic dependency graph.
A cycle error names the entire cycle, for example
`market -> strategy -> bet-size -> market`. These checks cannot be expressed
by JSON Schema alone. Graph output slugs are immutable: pass the previously
accepted document and reject removing a slug or changing its endpoint. Registry
updates also preserve existing output pin names and definitions. New output
slugs may be added. Deleting an entire graph is a separate lifecycle operation.

Authored settings use lowercase snake-case keys. Recursively through objects
and arrays, whole snake segments `secret`, `token`, `password`, `credential`,
`key`, `jwt`, `authorization`, `cookie`, `sub` and `selector` are refused, as are
the whole segment sequences `run_id` and `mount_alias`. Thus `max_tokens` is
allowed and `api_token` is refused. This rule applies to authored settings,
not the keys inside a trusted registry schema. A key filter cannot determine
whether a string value is sensitive; hosts must keep sensitive values out of
authored settings before saving.

Raw host selectors, run identifiers, host values and mount aliases live only
in frame memory. They are not block settings, graph fields or registry overrides.
No document field can spoof effect, residency, permissions or execution target.
The first-cut graph demonstrates Market to Strategy to Bet size, with a
fractional `fixed_count` of 0.25.

## Host messages

`contract/lab-embed.v1.schema.json` identifies `leaf.lab-embed.v1`, version 1.
Each envelope includes a channel nonce, principal proof, `source_sha`, proposal
revision and hash, monotonically increasing sequence, send/expiry times and
latency in microseconds. The proof is an opaque binding to an authenticated
server handshake, not a credential and not evidence of authentication by itself.
The host and frame must establish matching subject hashes through that trusted
handshake. No raw subject or authentication token crosses this protocol.

The receiver supplies trusted context separately from the envelope:

* The exact allowed origin and expected parent/frame window, plus the actual
  `MessageEvent.origin` and `MessageEvent.source`. Never use `*`. Window comparison
  uses identity, not a string supplied by the sender.
* An authenticated handshake, channel, principal proof, source hash and current
  proposal revision/hash. Every message must match all these bindings.
* A trusted current time and replay state scoped to this authenticated channel.
  Reset state only for a fresh authenticated handshake, not an incoming message.

`shell.ready` and `proposal` carry empty payloads. The proposal envelope binds
an already-held proposal; it does not transport a saved graph or host values.
`signed-request` carries only a request hash and `handoff: "server-sign"`.
The server must resolve that hash to the bound proposal, authorize the current
principal and sign the request on its own protected route. Accepting this
envelope does not sign, execute or authorize a Trade. Never send signing material.
`source.value` carries at most 200 rows in frame memory, never in saved graphs.

Shell envelopes are capped at 4096 UTF-8 bytes. `source.value` envelopes are
capped at 262144 bytes including metadata. The receiver caps accepted messages
at 20 in the rolling interval `(now - 1000ms, now]`, including source messages.
Send times cannot be in the future, lifetimes are positive and at most 30000ms,
messages at or past expiry fail, and sequence numbers must increase. Authentication
must be the boolean `true`. Trusted current time, replay sequence, replay timestamps
and supplied wire byte lengths must be nonnegative safe integers; replay timestamps
cannot be in the future. Refused messages do
not consume sequence or rate state. Rate limiting untrusted traffic before
parsing and closing abusive channels remain host responsibilities.

Measure incoming wire bytes before parsing and pass that size to validation;
canonical byte length is also checked. Reject duplicate object keys while
decoding JSON, and reject non-finite numbers and invalid Unicode. The contract
APIs accept parsed data, so they cannot recover duplicate keys already lost by
a decoder. No credential, raw subject, authentication token or host data belongs
in telemetry or persisted documents. There is no free-form telemetry field.

## Canonical bytes and shared conformance

Canonical JSON uses RFC 8785 (JCS): IEEE-754 binary64 numeric interpretation,
the ECMAScript shortest numeric form, finite numbers only, negative zero as
`0`, no spaces, UTF-16 code-unit key ordering, unchanged array order and no
Unicode normalization. Lone surrogates fail. Fixed numeric notation covers
`1e-6` through numbers below `1e21`; smaller/larger magnitudes use exponents
with a plus sign for positive exponents and no exponent-leading zeroes.
SHA-256 covers the canonical UTF-8 bytes with no BOM or trailing newline.

`digest-vectors.json` pins the canonical text, UTF-8 hex and digest independently
of implementation. It includes exponent boundaries, negative zero, subnormal
and maximum finite numbers, rounded binary64 values, escaping and supplementary
Unicode key ordering. `port-compat.v1.json` pins directional compatibility.
`ops-conformance.json` pins accepted and refused graph, manifest and host
operations. `lab-embed.v1.golden.json` pins all four host message shapes. Its
zero hashes and reserved `.invalid` origins are synthetic. No fixture contains
live host data or credentials.

The browser entry point is `web/src/lab/contract.js`. `createContract(bundle)`
accepts the four Lab schemas plus the frozen STD-5 schema, keyed by their paths
relative to `contract/`. It returns `schema(value, schemaName)`,
`block(manifest, trusted, previous)`, `graph(document, trustedRegistry, previous,
wireBytes)` and `embed(message, context, replayState)`. Successful semantic
calls return true; refusals throw. Schema-only validation does not replace
semantic validation. Registry keys are `id@version`. Supply registry data from
the server, not from the authored document. `canonicalJSON`, asynchronous
`digest`, and `compatible` are also exported. Digest uses Web Crypto.

The Python reference is in `server/tests/test_lab_contract.py`, using the
existing `jsonschema` dependency with local reference resources only. It checks
the same fixtures, frozen digests and schema validity. It invokes Node on the
same materialized cases, compares byte vectors and named cycle errors, and
checks refused messages leave replay state unchanged. If Node is unavailable,
it emits an explicit `DEFERRED` warning. This slice adds no runtime server or UI.

The planner runs `python -m pytest server/tests/test_lab_contract.py -q`.
