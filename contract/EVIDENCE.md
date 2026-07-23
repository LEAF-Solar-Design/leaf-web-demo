# EVIDENCE.md — frozen evidence + review-signature contracts

Status: **FROZEN** (wave-3 L4, census #30 chip `chip-evidence-contract-freeze`,
2026-07-23). These two contract identifiers name wire/storage shapes that
offline verifiers, the AutoCAD title-block target (Branch2025
`AutoCadTitleBlockEvidenceTarget`), and future consumers depend on
byte-for-byte. Any field addition, removal, rename, algorithm change, or
canonicalization change is a NEW contract version (`…v2`), never an edit to
`…v1`. The validator suite `platform/tests/test_evidence_freeze_static.py`
pins this document against the code and fails loud on drift in either
direction. KMS provisioning (key + IAM) is deliberately OUT of this freeze
(queued behind terraform health + the #44 custody decision); the provider
SEAM is frozen, the deployment is not.

Ground truth: `platform/evidence.py`, `platform/signing.py`, migrations
`0008_evidence_bundles.sql` / `0009_review_signatures.sql`.

## 1. `leaf.evidence.v1` — deterministic evidence-bundle manifest

### 1.1 Manifest shape (exact key set)

| Key | Value |
|---|---|
| `bundleVersion` | literal `leaf.evidence.v1` |
| `algorithm` | literal `sha256-merkle-v1` |
| `metadata` | non-empty JSON object, caller-supplied |
| `entries` | array of `{path, size, sha256}` — sorted by `path`, unique, relative, traversal-free (no leading `/`, no `..` segment) |
| `rootSha256` | lowercase hex Merkle root (§1.3) |

### 1.2 Canonical JSON (`canonical_bytes`)

UTF-8 of `json.dumps` with `sort_keys=True`, separators `(",", ":")`,
`ensure_ascii=False`, `allow_nan=False`. Exactly three non-JSON types are
converted, and each lands on the wire as a JSON STRING (the conversion hook
returns `str`, which `json.dumps` serializes quoted): `uuid.UUID` → `str`,
`datetime`/`date` → ISO-8601 `isoformat()`, `Decimal` → fixed-point
`format(value, "f")` (so `Decimal("1.10")` encodes as `"1.10"`, never a
float). Any other type raises — never a silent best-effort encoding.

### 1.3 Merkle construction (`sha256-merkle-v1`)

Domain-separated SHA-256, all digests over raw bytes:

1. Header pseudo-entry: `header = {bundleVersion, algorithm, metadata}`
   (exactly those three keys), `header_sha = sha256(canonical_bytes(header))`
   hex; it enters the tree as leaf path `@manifest-metadata`.
2. Leaf hash: `sha256(b"leaf-evidence-leaf-v1\0" + path_utf8 + b"\0" +
   bytes.fromhex(content_sha256))`.
3. Level order: the header pseudo-leaf FIRST, then one leaf per entry in
   `entries` order (path-sorted).
4. Odd level: duplicate the LAST node.
5. Node hash: `sha256(b"leaf-evidence-node-v1\0" + left + right)`.
6. `rootSha256` = final node, lowercase hex.

### 1.4 Offline verification error vocabulary (closed set)

`verify(manifest, blobs)` returns `{valid, errors, rootSha256}`; `errors`
draws only from:

`unsupported_bundle_contract` · `missing_entries` ·
`entries_not_unique_and_sorted` · `entry_set_mismatch` · `invalid_entry` ·
`missing:<path>` · `digest_mismatch:<path>` · `invalid_metadata` ·
`root_mismatch`

An empty error list is the ONLY success signal (`valid` is its derivation).

## 2. `leaf.review-signature.v1` — professional countersign record

### 2.1 Signed payload (exact key set, this is what the provider signs)

`canonical_bytes` (§1.2) of:

| Key | Value |
|---|---|
| `signatureContract` | literal `leaf.review-signature.v1` |
| `bundleId` | bundle UUID as string |
| `rootSha256` | the bundle's Merkle root (§1.3) |
| `credentialId` | credential UUID as string |
| `signedAt` | ISO-8601 timestamp |

Five keys, no more: the signature binds identity + bundle root + time, and
nothing else. Signature bytes are verified against the credential's stored
public key BEFORE the record persists.

### 2.2 History operation

Countersign appends `operationType: review.bundle.countersigned` whose
payload carries exactly: `signatureId`, `bundleId`, `rootSha256`,
`credentialId`, `signatureContract`, `signatureAlgorithm`,
`signatureSha256` (hex of the signature bytes' SHA-256).

### 2.3 Signing-readiness vocabulary (closed set)

`review_context` reports `{signing_available, reason, credential}` with
`reason` drawn only from:

`active_credential_required` · `credential_revoked` · `credential_expired` ·
`signature_provider_unavailable` · `signature_provider_mismatch` · `null`
(available)

### 2.4 Countersign preconditions (all enforced server-side, in order)

1. The bundle passes §1.4 offline verification AND its stored `root_sha256`
   matches — a bundle that cannot be re-verified cannot be signed.
2. The bundle is not superseded: no later history operation other than
   `review.bundle.countersigned` / `evidence.root.delivered` exists.
3. Every failing compliance finding is waiver-approved — unresolved failures
   block with the finding ids named.
4. Active, unexpired credential owned by the acting binding; provider
   algorithm matches the credential's `signature_algorithm`.
5. Idempotency: the same `Idempotency-Key` (or the same
   `(bundle_id, credential_id)` pair) returns the EXISTING record; the same
   key with different countersign input errors.

### 2.5 Provider seam (frozen interface, open deployment)

`SignatureProvider.sign(provider_key_ref, payload) -> bytes` with an
`algorithm` attribute; shipped implementations `LocalEd25519Provider`
(`ed25519`) and `AwsKmsEcdsaProvider` (ECDSA P-256 via KMS). Adding a
provider is additive; changing `sign`'s contract is a new version.
