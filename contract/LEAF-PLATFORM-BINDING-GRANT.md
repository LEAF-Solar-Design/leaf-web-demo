# Server-signed drawing binding grant

R10 lane E: a proposed authorization boundary for the Leaf Automation AutoCAD plugin and Leaf Automation Studio. This is a design decision, not an implemented control. It preserves the same workspace, project, material, and execution identity across the native and web surfaces.

## Problem

The bridge authenticates messages from its page with a shared session key. That proves possession of the key, not workspace membership. A script executing in the accepted page can sign a fresh `drawing.bind` request containing invented or unrelated IDs. Native validation checks GUID syntax and the DWG fingerprint, but does not resolve the IDs against server records.

Evidence paths under `C:/tmp/askall-r10-7c6cf39a/review/src/` are frozen review snapshots:

- `branch2025/SignedDrawingBindingTransport.cs`: `sessionKey = Convert.ToBase64String(_session.SessionKey)` gives the page the key. `Guid.TryParseExact(payload.Value<string>("platformTenantId"), "D", out var tenant)` and `identity = new HostDocumentIdentity(tenant, project, drawing, version,` show local validation followed by identity construction.
- `studio/LeafPlatformScene.jsx`: `bridge.start()` is in the mount effect, before the signed-in rendering branches. Hiding controls is not native authorization.
- `branch2025/LeafPlatformWebViewHost.cs`: `if (answer != DialogResult.Yes)` preserves a native approval step. `DrawingPlatformBindingStore.TryBindUnbound(document.Database, identity)` performs the write.
- `branch2025/AutoCadDrawingBindingStore.cs`: `if (nod.Contains(RecordName)) return false;` prevents this path from replacing an existing binding.

The practical risk is a user approving a misleading connection because the page shows one project name while the native prompt shows opaque IDs. The result is an incorrect persistent local identity. This does not establish cross-tenant cloud access, arbitrary commands, or geometry modification. The red-hat report, `C:/tmp/askall-r10-7c6cf39a/review/runs/red-hat/out-codex-astra-high.txt`, finding 2, says: "This proves a local authorization and binding-integrity gap." The summary at `C:/tmp/askall-r10-7c6cf39a/review/REVIEW.md` puts this finding under `Astra-only, worth fixing (source-cited, not re-run)`.

A grant narrows what a hostile page can request. It cannot stop that page requesting a valid grant within the signed-in user's rights or lying elsewhere in its UI. Native confirmation remains necessary.

## Goals and non-goals

Require server authorization of the complete identity tuple before first binding. Tie that authorization to one plugin session and DWG. Show server-sourced workspace, project, drawing, and version names in a native prompt. Preserve default No, first-binding-only storage, origin checks, and the existing signed transport.

Do not introduce another workspace identity, move user tokens into the plugin, authorize later cloud requests, or make this grant a general command capability. Existing bindings, Select/Zoom authorization, queued-command document races, flood protection outside binding, and DWG ownership attestation remain separate work. A fingerprint identifies the host document; it does not prove that its geometry matches the uploaded version.

## Grant format

Use a compact JWS with a dedicated RSA-3072 signing key, SHA-256, and PKCS#1 v1.5 signatures (`RS256`). RSA verification fits the existing Windows/.NET crypto surface and avoids ECDSA signature-format conversions. Keep the private key in the server's managed signing service, separate from Auth0 keys and bridge HMAC secrets. Only the grant issuer may request signatures. Infrastructure and signer provisioning are implementation work, not actions authorized by this document.

The protected header has exactly `alg`, `kid`, and `typ`, in that order. Values are `RS256`, a configured key identifier, and `leaf-binding-grant+jws`. No embedded keys, key URLs, alternate algorithms, or unprotected headers are accepted.

The payload has these fields, in this exact order:

| Field | Meaning and constraints |
| --- | --- |
| `schemaVersion` | Integer `1`. |
| `iss` | Exact environment-specific issuer string pinned in plugin configuration. |
| `aud` | Exact string `leaf-automation:autocad:drawing.bind:v1`. |
| `platformTenantId` | Workspace UUID, equal to the server's canonical org ID. |
| `workspaceName` | Server org display name. |
| `projectId`, `projectName` | Authorized project UUID and stored name. |
| `drawingId`, `drawingName` | Drawing artifact UUID and stored name. |
| `drawingVersionId`, `versionName` | Version UUID and server label `Version N` from its sequence number. |
| `actorBindingId` | Active server identity binding that requested issuance. |
| `pluginSessionId` | Exact native-generated unbound session UUID. |
| `documentFingerprint` | Exact `sha256:` plus 64 lowercase hex digits from native ready. |
| `iat`, `exp` | Integer UTC Unix seconds. Default lifetime 120 seconds; `0 < exp - iat <= 300`. |
| `nonce` | Server-generated 32 random bytes encoded as unpadded base64url. |

UUIDs use lowercase, nonzero, hyphenated D form. Do not add a UUID version restriction. Names contain 1 to 200 Unicode scalar values. Reject control characters, unpaired surrogates, and bidi formatting controls. Names are plain text, never markup. If legacy names fail these rules, issuance returns a rename-required error rather than silently changing the identity label.

Canonical encoding is deliberately narrow: UTF-8 without BOM; one JSON object; keys in the order above; no whitespace or trailing newline. Strings escape only quotation marks as `\"` and backslashes as `\\`; other permitted characters are literal UTF-8. Integers use base-10 digits without leading zeros or exponent notation. No Unicode normalization is performed. Nulls, floats, duplicate keys, missing keys, and extra keys are invalid. The same rules and specified order apply to the header; `kid` uses 1 to 64 ASCII letters, digits, underscores, or hyphens.

Let H and P be unpadded base64url of those header and payload bytes. Sign the ASCII bytes `H.P`; transmit `H.P.S`, where S is unpadded base64url of the fixed 384-byte RSA signature. The plugin verifies the received bytes, then parses strictly and compares a canonical re-encoding to those bytes. It never verifies a reserialized substitute. Reject noncanonical base64url and grants over 8 KiB. Cross-language fixtures must cover quotes, backslashes, accented names, and integer time boundaries.

## Issuance

Add `POST /api/projects/{project_id}/drawing-versions/{version_id}/binding-grants`. Body fields are only `pluginSessionId` and `documentFingerprint`. The server derives drawing and workspace IDs from the selected version and authenticated actor. It ignores no extra fields: reject them. Display names, roles, lifetime, audience, and nonce cannot come from the page.

Use live token verification and active subject binding. Current `platform/deps.py` anchors are `payload = auth.verify_platform_token(authorization)` and `binding = resolve_active_identity_binding("auth0", str(subject))`. `platform/store.py` describes this lookup as `Identity lookup used only after JWT verification; never accepts org hints.` The new endpoint must refuse issuance when live auth is off. Existing `get_org_id` has a development seam, `return uuid.UUID(x_org_id)`, which must never reach a trusted signer.

Resolve the actor as the live branch of `platform/api.py::_get_lifecycle_actor` does. Apply named-project write membership, including invited project editors. Do not require a tenant-wide editor role in addition: the API explicitly states `an invited project editor may write only that project`. Reuse the semantics of `platform/project_lifecycle.py::_require_project_role`, whose anchor is `allowed = WRITE_ROLES if write else PROJECT_ROLES`. Require an active identity binding and active project owner/editor membership. Tenant ownership alone does not grant project access.

Existing catalog reads are weaker evidence: `platform/api.py` uses `org_id: uuid.UUID = Depends(get_org_id)` for `open_project`; `platform/store.py::get_project` scopes by `WHERE org_id = %(org_id)s AND project_id = %(project_id)s`. That establishes tenant-scoped lookup, not named-project membership. Do not turn catalog visibility into grant authority.

In one consistent database transaction, check the org and project are active, the project is not deleted, membership remains active, and the artifact and nondeleted version belong to that exact org and project. Reject archived, unavailable, or purge-pending material. Derive names from the joined rows, not a second client-supplied lookup. Preserve any canonical-authority restriction applicable to that project. Set `iat` at this decision, commit the decision and audit record, then sign without extending `exp`. Do not return a grant that expired during signing. Revocation after that decision leaves at most the grant's remaining lifetime; offline native verification cannot provide immediate revocation.

The endpoint accepts host context through the page, so it cannot attest that a real plugin requested it. Native equality checks make a fabricated session or fingerprint unusable elsewhere.

Initial shared rate limits: 10 issuances per actor per minute, 3 per actor/session per minute, and 100 per workspace per minute, with no burst above those counts. Return 429 and `Retry-After`. Use 401 for invalid authentication, 403 for insufficient role, generic 404 for unavailable scoped targets, 422 for invalid input, and 503 for signing or audit failure. Return `{grant, expiresAt}` with `Cache-Control: no-store`. Apply existing authenticated-request and origin protections; do not create a cookie-only CSRF exception.

Record request ID, actor binding, workspace and identity tuple, session, fingerprint digest, nonce digest, key ID, issuance/expiry, policy version, decision, and bounded refusal reason. Never log tokens, session keys, or the compact grant. An issuance record is not proof of native acceptance. Optional client outcome telemetry must be marked untrusted.

## Native verification

Ship environment-specific trusted public keys and issuer configuration in the signed plugin release. Page messages cannot add keys or select a trust endpoint. For v1, rotate through signed plugin updates: distribute a release trusting old and new key IDs, then switch issuance after the supported fleet has that release. Retain the old verifier key through outstanding grants and remove it in the next supported release. A compromised key requires stopping issuance and a plugin update that removes trust; offline old installations cannot be remotely revoked. Unknown key IDs fail closed with an update message. No remote key fetch is required in v1.

Add a verifier called by `SignedDrawingBindingTransport.TryRead` after outer transport checks and before constructing `HostDocumentIdentity`. Accept a grant-specific bind verb and payload containing only `grant`. Verify size, strict encoding, pinned key/algorithm, signature, schema, issuer, audience, fields, and canonical form. Compare session and fingerprint to native state, not just the outer envelope. Check `iat <= now + 30`, `now < exp`, and the 300-second lifetime cap; there is no grace after expiry. Use a monotonic elapsed-time deadline after acceptance so clock rollback cannot extend an open prompt.

Reserve `(sessionId, nonce)` atomically before showing one prompt per session. Keep consumed nonces until expiry; cap the live set at 64 and reject new admissions when full rather than evicting a live entry. Decline, timeout, and write failure consume the nonce too. Duplicate requests return a replay result without another prompt. Session replacement revokes prior grants, and restarting the plugin creates a fresh unpredictable session, so persistent replay storage is unnecessary.

The prompt says: "Connect this DWG to this drawing in Leaf Automation Studio?" Show the native DWG filename and verified workspace, project, drawing, and version names. Put full IDs in an expandable details area for duplicate-name disambiguation. Render literal text with wrapping; never truncate the only identifying label. Default to No. State that the server verified access, not geometric equivalence. Names are verified metadata, not proof that a similarly named project is the intended one.

Before the write, recheck time, session generation, active document reference, and freshly computed fingerprint under the document lock. Keep `TryBindUnbound` as the final atomic first-write check. The existing fingerprint is based on `"leaf-dwg-fingerprint-v1\0" + database.FingerprintGuid.ToString()` in snapshot `AutoCadDrawingBindingStore.cs`; it is not a content checksum and copies may share it. Document reference plus session binding remains essential.

On any failure, leave the DWG unchanged and return a bounded reason through the signed bind-result channel. Do not fall back to unsigned identity fields. Give actionable messages for expiry, changed document, denied access, and required update. Catch malformed-input and crypto failures at the message boundary. The native dialog is the trusted confirmation surface; a compromised page can forge HMAC status text that the page itself displays.

## Page changes

After sign-in and an unbound ready message, request a grant only when the user selects Connect drawing. Carry the native session ID and fingerprint. Pass the compact string unchanged to the grant-specific bind method. Replace the snapshot `hostBridge.js` path `payload: { ...identity, documentFingerprint: ready.documentFingerprint }`; do not attach a second authoritative identity tuple.

Keep the existing asynchronous session guard, `if (this.channel !== channel || this.state.ready !== ready) throw new Error('AutoCAD connection changed')`, after both the API request and signing. Abort outstanding issuance on sign-out, workspace change, reconnect, or navigation. Drop grants from memory after completion and never persist them in storage, URLs, or analytics.

Show distinct pending states for server authorization and native confirmation. Retain selections on failure, announce results in a live region, prevent duplicate clicks, and offer an explicit retry that requests a fresh grant. A timeout means outcome unknown: reconcile native ready state before retrying. Starting the bridge after sign-in reduces exposure but does not replace native enforcement.

## Rollout and compatibility

Introduce `drawing.bind_grant` and advertise `bindingGrantVersion: 1` in unbound ready. This avoids sending an extra field to the old transport, whose snapshot requires exact payload keys through `SequenceEqual(exact.OrderBy(value => value, StringComparer.Ordinal))`.

| Pair | Behavior |
| --- | --- |
| Old plugin, new page | Detect absent capability. Disable new binding with an update instruction. Existing bound read/selection behavior stays as supported today. |
| New plugin, old page | With enforcement on, reject legacy `drawing.bind` with `binding_grant_required`. Show a native instruction to reload/update the page. |
| New plugin, new page | Issue, verify, confirm, then write using the grant. |

Use a native deployment flag `RequireServerBindingGrant`, initially off only for a bounded compatibility pilot. With it off, legacy binding retains its old risk and native prompt; label that mode in release notes. A grant-path failure never triggers automatic legacy retry. The page cannot set this flag. Separately gate server issuance for staged deployment.

Deploy issuer and key first, then capability-aware page and plugin. Turn native enforcement on after supported clients have keys, issuance is live, and focused cross-language/native checks cover wrong tenant/project, expired grants, replay, tampering, document changes, and duplicate clicks. Existing NOD records remain readable but are not retroactively certified. Roll back by disabling new binding while preserving established records, not silently restoring legacy authorization. No enforced-fleet claim includes old plugins that still allow legacy binding.

## Alternatives considered

- **Keep native Yes only:** cheap, but a raw-ID prompt leaves the server authorization gap and gives users little basis for approval.
- **Plugin calls the API with the user's token:** can check current access directly, but expands credential handling, sign-in, refresh, and network failure paths inside AutoCAD. It remains an option if immediate revocation becomes required.
- **Per-install device keys:** attest an enrolled installation, not user membership or the selected identity tuple. Enrollment, recovery, and key revocation add work without replacing server authorization.
- **Use the existing HMAC for grants:** every page key holder could mint them, defeating the intended boundary.

## Open questions

- Who may bind? Recommend active project owners and editors, including invited editors, using lifecycle membership policy. Read-only access should not authorize a persistent local binding.
- Is offline first binding required? Recommend no. Existing connections remain usable within their existing controls; new connections require fresh server authorization.
- Is a two-minute revocation delay acceptable? Recommend yes for this narrow local action with native confirmation. If not, choose native online redemption instead of claiming immediate revocation from a signed token.
- How should customers recover a mistaken binding? Recommend a separate explicit native repair workflow. Do not weaken first-binding-only behavior in this feature.
- Who owns signer rotation and minimum plugin version? Recommend the Leaf Automation release owner, with an overlapping-key release procedure before enforcement.
- Should existing records be marked verified? Recommend no. A future migration must reauthorize them; mere presence of a record proves no grant was checked.

## Effort

Sizes are engineering estimates, excluding release scheduling and operator decisions.

- Grant contract and cross-language byte fixtures: **S, 1 day**.
- API, consistent scoped lookup, project membership, audit, and rate limits: **M, 2 to 3 days**.
- Dedicated signer provisioning and overlapping-key release setup: **M, 1 to 2 days**.
- Native strict verifier, replay admission, dialog, and locked recheck: **M, 2 to 3 days**.
- Page capability negotiation, issuance, pending state, and recovery: **S, 1 to 2 days**.
- Focused integration checks and one real AutoCAD acceptance pass: **M, 1 to 2 days**.
- Pilot, compatibility notes, and enforcement rollout: **S, 1 day**, plus fleet update time.

Total: **9 to 14 engineering days**. The critical path is signer readiness, native verification, then supported-client rollout. This lane delivers the design only.
