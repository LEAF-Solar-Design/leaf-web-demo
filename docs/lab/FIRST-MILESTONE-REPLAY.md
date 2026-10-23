# First-click replay

Run from the repository root with Node 20 or later. The automated script uses no cookies, tokens or browser session. Keep the operator receipt and screenshots outside git.

```powershell
node scripts/studio-lab-embed-proof.mjs --self-test
# Origins: the Studio staging alias and the dashboard origin from web/src/lab/embed/hostOrigins.js (public deployment URLs, not Kashbot host rows). <program-receipts-dir> is the program receipt folder outside git.
node scripts/studio-lab-embed-proof.mjs --studio https://platform-staging.leafdesign.ai --ops https://ops-dashboard-seven.vercel.app --receipt-dir <program-receipts-dir> --allow-missing-receipt
node scripts/studio-lab-embed-proof.mjs --studio https://platform-staging.leafdesign.ai --ops https://ops-dashboard-seven.vercel.app --receipt-dir <program-receipts-dir> --max-age-hours 24 --json
npm --prefix web run proof:local -- lab-milestone.spec.mjs
```

The first command exercises real HTTP requests against two loopback fixture servers, the checked-in schema PINs and canonical golden, fresh and stale receipts, SHA mismatch, missing screenshots, malformed walk/privacy fields, missing-receipt policy, absent and present CSP directives, exact-origin CSP rejection, dashboard framing protection and both accepted API cache policies. The fixture directory is temporary and removed on exit.

The second command is the pre-walk automation check. Only an absent receipt may be skipped. A present stale or malformed receipt fails even with `--allow-missing-receipt`. The third command is the proof gate: it must exit 0 without that flag. `--json` emits a single object with SHAs, checks, measured headers in `csp`, a sanitized receipt summary and `ok`; without it each check gets one evidence line. No response payloads or receipt free text are printed. Store the final JSON outside git as the program's SEL-107 receipt.

For the local e2e, first start the real managed local stack with its harness built, Lab enabled and a local demo principal authorized for the Lab rail. Use the local config's default ports or set `LEAF_E2E_BASE_URL` and `LEAF_E2E_API_BASE`. If the stack requires live auth, inject its granted local demo bearer through `LEAF_E2E_LAB_JWT` in the process environment. Never paste it into this recipe or a receipt. The test uses the same demo tenant as other local proofs and never mocks Lab status, Jobs or evaluation. SEL-108 must be available: it supplies Jobs, Count, Latest and Values. The Jobs endpoint must provide a source timestamp for the `as of` badge; `age unknown` does not pass that assertion.

The browser walk drags all three real kinds, connects Jobs.rows to Count.value and Latest.value through the Inspector's Configure controls, checks the four inspector tabs and Count's numeric display and badges, then reloads and requires an empty canvas. It writes `lab-milestone-receipt.json` in the Playwright test output directory with only kind names, badge names and assertions. Video, trace and automatic screenshots are disabled for this spec to avoid saving source rows. Do not treat an unmanaged readiness skip as a pass.

## Operator receipt

Use an actual authenticated dashboard session. Open `/studio-lab`, confirm that the frame becomes ready, drag a Dashboard source, wire a transform and show a Value or Chart card with its `as of` and `p50` badges. Check frame and parent headers and confirm there are no CSP violations in browser developer tools. Absent CSP directives must be recorded as absent, not attested as an enforced policy. Confirm the iframe points to the intended Studio deployment, uses the expected sandbox and no-referrer policy, and that Retry and Open in Studio work. Record only the number of relayed hosts.

Read Studio's deployed `source_sha` from `/api/health`. Dashboard `/api/health` is credential-gated in the available source; its successful response exposes `data.build.sourceSha` (or `build.sourceSha` before wrapping), derived from build-time `BUILD_COMMIT_SHA` / `VERCEL_GIT_COMMIT_SHA`. If the unauthenticated script cannot read it, independently read the deployed dashboard's full commit from the deployment UI and copy it into `ops_sha`. Never use `x-vercel-id`, a branch tip or the Studio protocol's hashed SHA as a deployment commit. A readable dashboard health SHA must match the receipt exactly.

Create a new external file named `sel107-operator-<timestamp>.json` using this shape. Replace the blank handle, time and commits only after the walk. Copy the complete `csp` object from the script's current `--json --allow-missing-receipt` output; the example below reflects the signed-out headers measured on 2026-10-23. `frame_ancestors` and `frame_src` are `absent` or `includes-origin`; the latter means the supplied directive includes the exact other origin. `api_cache_control` records each API's actual header string. A receipt fails if any of these facts differ on replay. The template intentionally does not pass. The operator handle must use 2 to 32 letters, digits or underscores, starting with a letter. Use an operator handle, never an email or machine identifier.

```json
{
  "schema": "leaf.sel107-operator-receipt.v1",
  "signed_by": "",
  "signed_at": "",
  "studio_sha": "",
  "ops_sha": "",
  "walk": [
    { "action": "Dashboard source dragged", "observed": "Dashboard source is on the canvas" },
    { "action": "Transform wired", "observed": "Transform has a connected input" },
    { "action": "Value or Chart card shown", "observed": "Value or Chart card shows as of and p50 badges" }
  ],
  "screenshots": ["studio-1.png", "dashboard-1.png"],
  "csp": {
    "frame_ancestors": "absent",
    "frame_src": "absent",
    "x_frame_options_ops": "DENY",
    "api_cache_control": {
      "principal": "public, max-age=0, must-revalidate",
      "relay": "public, max-age=0, must-revalidate"
    }
  },
  "hosts_relayed": 1
}
```

Signing is the operator's explicit attestation, not a cryptographic signature. Use UTC ISO time, for example the output of `(Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")`. Do not pre-attest a failed or incomplete walk. Save at least two nonempty PNG screenshots alongside the receipt, with neutral names `studio-N.png`, `dashboard-N.png` or `walk-N.png` (N is 1 to 99). Capture the actual authenticated walk while hiding row contents, identifiers, addresses and credentials. No host names, run identifiers, emails or payload digests belong in any receipt or screenshot. The validator accepts only the displayed walk phrases and exact receipt fields, so arbitrary private text cannot enter evidence through an extra field. It selects the most recently modified matching receipt, never falls back to an older passing receipt, and rejects future or expired signatures. Re-walk after either deployment changes.

## Pass and current gaps

A pass requires every HTTP, header, protocol and receipt check plus a completed local browser walk. The Studio embed must return 200, contain `Sign in to use Lab.` and have no X-Frame-Options DENY. If CSP `frame-ancestors` is present it must include the exact dashboard origin. The dashboard parent must return a 307 to sign-in or a 200 signed-out page, with X-Frame-Options DENY. If its CSP `frame-src` is present it must include the exact Studio origin. Absent directives are allowed and recorded as `absent`.

The planner measured no CSP on either page on 2026-10-23. The Studio bridge's host-origin allowlist gates embedding; the dashboard's global DENY header protects the parent page from being framed. Signed-out principal and relay probes both return 401 with `public, max-age=0, must-revalidate` from auth middleware. The script requires each probe to return 401 with either `no-store` or `max-age=0` and records the actual header. The route handlers' `private, no-store` applies to authenticated responses. These signed-out measurements do not attest the authenticated walk.

Three boundaries remain explicit:

- Dashboard build SHA: public health may be gated. In that case the receipt summary says `operator-deployment`, and freshness of that SHA is operator-attested rather than independently measured by the script.
- Signed-in behavior: the no-credential script checks the public boundary; the operator attests the actual authenticated embed, ready/relay status, screenshots and CSP results.
- Host privacy: hosts are counted, never named. No host rows, names, run identifiers, emails or credentials are stored in git or emitted by the proof script.
