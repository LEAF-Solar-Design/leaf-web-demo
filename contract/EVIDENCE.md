# EVIDENCE.md, frozen evidence contracts v1

Status: **FROZEN**. This document freezes `leaf.evidence.v1` and
`leaf.review-signature.v1` from the shipped `platform/evidence.py` and
`platform/signing.py` implementations.

## Scope

These contracts describe offline-verifiable evidence manifests and professional
review signatures over their roots. They do not define evidence-producing
workflows, credentials, or authorization decisions.

**KMS infrastructure is out of scope.** Key custody and AWS KMS integration
remain queued behind Terraform health and the #44 custody decision. A provider
may produce a signature only through the structural algorithm names below.

## Common wire rules

All records are UTF-8 JSON objects. Names are case-sensitive. A conformance
validator rejects a missing required field, a field with the wrong type, and an
unknown field. A JSON integer excludes `true` and `false`.

For either v1 identifier, frozen fields cannot be removed, renamed, retyped,
or given a different meaning. A compatible extension must be an explicitly
documented optional field, added to this document and its validator in the same
change. An incompatible change requires a new contract identifier. Until such
an extension is promoted, unknown fields reject.

UUID values use the canonical hyphenated UUID text form. A SHA-256 value is 64
lowercase hexadecimal characters. `base64` means non-empty standard RFC 4648
base64 text with padding when required.

## `leaf.evidence.v1`

An evidence bundle is a manifest plus the bytes addressed by each entry. The
manifest has exactly these required fields:

| Field | Type and rule |
|---|---|
| `bundleVersion` | string, exactly `leaf.evidence.v1` |
| `algorithm` | string, exactly `sha256-merkle-v1` |
| `metadata` | non-empty JSON object with string keys and JSON values |
| `entries` | non-empty array of entry objects, ordered lexicographically by `path` with no duplicate path |
| `rootSha256` | SHA-256 lowercase hexadecimal string |

Each `entries` item has exactly these required fields:

| Field | Type and rule |
|---|---|
| `path` | non-empty string, relative, does not start with `/`, and no `/`-separated component is `..` |
| `size` | non-negative JSON integer, equal to the addressed byte length |
| `sha256` | SHA-256 lowercase hexadecimal string for the addressed bytes |

### Canonical serialization and root

Canonical bytes are UTF-8 from JSON serialization with lexicographic key
ordering, compact separators `,` and `:`, `ensure_ascii=false`, and
`allow_nan=false`. The implementation serializes UUIDs as strings, dates and
datetimes as ISO 8601 text, and decimals as fixed-point text before encoding.

The root is the hexadecimal SHA-256 Merkle root. Its leaves are, in order, the
metadata leaf followed by the sorted entry leaves:

1. Build the header object `{bundleVersion, algorithm, metadata}` and hash its
   canonical bytes to `headerSha256`.
2. Hash each leaf as `SHA256(domain || path UTF-8 || NUL || hexDigestBytes)`,
   using `leaf-evidence-leaf-v1\0`. The metadata path is
   `@manifest-metadata`, and entry paths and digests come from `entries`.
3. While a level has more than one item, duplicate its final item when it has
   odd length. Hash every pair as `SHA256("leaf-evidence-node-v1\0" || left ||
   right)`.
4. The final digest is `rootSha256`.

Bundle bytes are not embedded in the manifest. A verifier requires the bundle
byte path set to equal the manifest path set and checks every length, entry
digest, and root.

## `leaf.review-signature.v1`

A review signature countersigns one frozen evidence root. Cryptographic
verification is provider-specific and is not part of this shape contract.

### Signed payload

The canonical signed JSON object has exactly these required fields:

| Field | Type and rule |
|---|---|
| `signatureContract` | string, exactly `leaf.review-signature.v1` |
| `bundleId` | canonical UUID string |
| `rootSha256` | SHA-256 lowercase hexadecimal string from the evidence manifest |
| `credentialId` | canonical UUID string |
| `signedAt` | ISO 8601 timestamp with an explicit UTC offset |

The bytes signed are the common canonical serialization of this payload. The
payload binds the named credential and timestamp to exactly one bundle root.

### Returned signature record

The review-signature API record has exactly these required fields:

| Field | Type and rule |
|---|---|
| `signature_id` | canonical UUID string |
| `history_operation_id` | canonical UUID string |
| `bundle_id` | canonical UUID string, equal to `signed_payload.bundleId` |
| `credential_id` | canonical UUID string, equal to `signed_payload.credentialId` |
| `root_sha256` | SHA-256 lowercase hexadecimal string, equal to `signed_payload.rootSha256` |
| `signature_algorithm` | `ed25519` or `ecdsa-p256-sha256` |
| `signature_base64` | non-empty standard base64 text |
| `signed_payload` | the exact signed-payload object above |

The structural validator checks only field names, types, formats, and these
cross-field bindings. It does not verify a signature against a live key or a
KMS provider.

## Implementation note

`platform.evidence.verify()` is an integrity verifier, not a strict schema
validator. It currently ignores unrecognized manifest and entry keys while it
checks the frozen fields. The conformance validator in
`server/tests/test_evidence_contract_freeze.py` rejects those keys so an
unpromoted extension cannot silently become part of this contract. This is a
known strictness difference, not permission to add v1 fields without the
additive-change process above.
