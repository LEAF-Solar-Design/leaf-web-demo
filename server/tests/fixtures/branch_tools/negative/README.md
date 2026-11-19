# Host proof fixtures

These packages are unsigned source fixtures. `expected.json` lists eleven cases
in execution order; package paths are relative to `fixtures/`. The positive
package is `proof/panel-gap-check` version 1.2.1, reviewed. Its scripts are byte
copies of the reference. The five negative packages each isolate a host refusal.

At proof time, start every case from a clean package and sign a fresh detached
`leaf.tool-envelope.v1` envelope with the real publisher key. Set `package` to
the manifest name, version and canonical raw-byte package digest, `tiers` to
`["self_hosted"]`, and `signed_at` to the proof's current UTC time. Use the
contract's canonical JSON and ECDSA P-256/SHA-256 DER signature, lowercase hex.
No signing key or signature belongs in these package directories.

Fix one UTC `now` for each case. Unless specified below, supply a fresh signed
`leaf.tool-revocations.v1` list with `issued_at = now` and `entries = []`.
Both envelope and list carry the publisher's `key_id` and
`alg = "ECDSA_P256_SHA256"`. Sign the canonical document without `signature`.
The Python tests use an ephemeral key and runtime keyring for the same recipe.

Apply exactly one operation after signing the original envelope:

| Operation | Exact mutation |
|---|---|
| `none` | Keep all bytes and signed documents unchanged. |
| `flip_signature_hex_char` | Replace the last lowercase hex character of the envelope's `signature` with the hex digit obtained by XORing its numeric value with 1. Preserve length and all preceding characters. Do not re-sign. This changes the low bit of the final DER signature byte without changing its structure. |
| `flip_lib_byte` | XOR the final byte (offset `length - 1`) of `lib/geometry.csx` with `0x01`. Preserve every other package byte, envelope field and signature. Do not recompute the signed digest or re-sign. |
| `revoke_digest` | Replace the revocation list's `entries` with one object containing the envelope's exact `name`, `version` and `package_digest`, plus `reason = "proof: revoked digest"` and `revoked_at = now`. Keep `issued_at = now` and sign the resulting list. Do not change the envelope. |
| `revocations_issued_8_days_ago` | Keep the revocation list's `entries = []`, set `issued_at = now - 8 days` (192 hours), then sign the resulting list. Keep the envelope fresh and unchanged. Verify as source `downloaded`; all other cases use source `bundled`. |

`subscribed_self_hosted` means a subscribed seat entitled to `self_hosted`.
`no_subscription` means a seat without that entitlement. Use host API 1 on both
AutoCAD 2023 and 2026 so `needs-host-api-2` refuses before script execution.
All cases except `unentitled` run with the subscribed seat.

The host must reject `unreviewed-tool` before execution. The
`write-without-capability` package declares only `drawing.read` but opens model
space for write, appends a line and attempts to commit. Confirm
`capability_violation` and that the transaction is aborted with no persisted
line; a decision code alone does not prove rollback. `slow-loop` ignores
cancellation and spins for five seconds against a 1000 ms timeout; confirm
`timeout` and host recovery. `throws-error` raises
`InvalidOperationException("proof: deliberate failure")`, expecting `script_error`.

The Linux tests validate packages, byte identity, case metadata, forbidden script
tokens, and the first five envelope decisions. CAD execution, seat entitlement,
host API checks, rollback and timeout recovery require the real host proof.

When run from a checkout, `runner-linux/test_negative_fixtures.py` sets
`runner.SCHEMA_PATH` to `contract/tool.schema.json` before validating packages.
The default sibling `runner-linux/tool.schema.json` is supplied by the Docker
image, rather than committed in the source tree.
