# Surface-config live proof

This runner serves convergence ledger row 12a, the live tenant counterpart to
`server/tests/test_surface_config.py::test_post_commits_exact_bytes_fresh_receipt_and_evicts_cache`.
It is prepared but **NOT yet executed live**. Offline adapter cases do not close
the live tenant proof.

## Required live input

The execution holder must supply:

1. An already authenticated browser page at
   `https://platform-staging.leafdesign.ai/try?surface=cad`, in a dedicated,
   exclusively held staging tenant with surface-config write entitlement.
2. That dedicated tenant's exact non-secret tenant id. The holder must confirm
   it is reserved for this proof. A staging hostname does not establish tenancy.
3. The exact expected deployed application source commit, 40 lowercase hex
   characters, obtained from the deployment owner.
4. Bound calls to the current shell's existing `api.js` clients:
   `getSession`, `getSurfaceConfig`, and `submitSurfaceConfig`. Supply these as
   the transport adapter. They must execute in the authenticated application
   context, not a new browser context or a Node HTTP client with copied tokens.
   The browser bridge must survive reload and resolve the new shell instance.
5. An existing valid complete overlay with `cad.authoring: true`, and a visible
   CAD Author tab (`#workspace-tab-author`). The runner does not seed a baseline
   or change the shell to make a refused candidate eligible.

No bearer, cookie, credential argument, authentication export, installation,
or browser launch is part of this runner. A bare page without the application
client adapter is refused. The adapter is an execution input, not an assertion
that a production bundle exports a global client or serves `/src/api.js`.

```js
import { surfaceConfigProofPage, runSurfaceConfigProof } from
  './web/scripts/verify_surface_config_live.mjs'

const proofPage = surfaceConfigProofPage(authenticatedPage, {
  transport: boundApplicationClients,
  dedicatedTenantId,
})
const receipt = await runSurfaceConfigProof({
  page: proofPage,
  baseUrl: 'https://platform-staging.leafdesign.ai',
  expectedSourceSha,
})
// Persist only this sanitized receipt. ok must be true to count the proof.
```

The deployment read uses the existing `captureStagingIdentity` application
staging client against `/api/ready`. It requires explicit staging, readiness,
no degraded mode, and exact source equality before and after the change.
This is that endpoint's source readback, not independent image attestation.

## Change and restoration

The anchor proves the boolean `cad.authoring` slot, including false and true.
The runner reverses that boolean from true to false while retaining the entire
baseline, including unrelated nested fields. In `ToolCast.jsx`,
`surfaceSlots.authoring === true` mounts the workspace rail and its Author tab.
Preflight requires that tab visible in the CAD stage; reload must detach it
while the selected CAD tab remains visible. Restoration must make it visible
again. The anchor's `sheets.chrome.tab: "My sheets"` is not a label change:
`ProductSurfaceTabs.jsx` treats it as a truthy visibility gate and still renders
the manifest label. The runner does not pretend that changing this string
proves visible copy changed.

Every POST is followed immediately by a fresh GET of the complete resource.
The source receipt must match, including its timestamp. A lost response is
resolved by GET and its source receipt; the runner never retries the candidate
POST. A normal response without a receipt fails. Cleanup runs in `finally`,
checks identity and exact overlay plus source before writing, restores the
baseline, checks its GET and source, reloads, and verifies visible restoration.

Observed concurrent changes stop cleanup without another POST. The current
endpoint has no compare-and-swap parameter, so a GET followed by POST cannot
exclude a writer arriving between those requests. The holder must reserve the
dedicated tenant for the whole run; this runner does not add a second fold or
claim atomic concurrency protection. It detects observed overlay or source
drift and leaves explicit conflict evidence for the holder.

The returned receipt contains fixed status codes, booleans, and hashes only.
It never retains overlays, raw identity, DOM text, source timestamps, URLs,
headers, or exception messages. `current` reports whether readback succeeded,
the overlay and source digests, and exact equality to baseline and candidate.
Restoration failure always makes `ok: false`, even if mutation proof succeeded.
If current state cannot be read it says `readable: false`; unknown is not restored.

## Offline verification

Fable runs `node --test web/scripts/test_verify_surface_config_live.mjs`.
The cases use injected page and transport adapters, with no browser, network,
new dependencies, or install. They cover success, preflight refusal, lost
response, readback and receipt mismatch, concurrent change, invisible change,
and both GET and visible restoration failure.
