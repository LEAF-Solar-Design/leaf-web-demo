# Wave 2 stream receipt (SEL-220)

The authenticated staging proof of the Lab run stream passed on 2026-10-26: 7 of 7 tests in
`web/e2e/staging/lab-sse.spec.mjs`, run with the staging acceptance principal against the deployed staging app.

## Evidence

- Receipt: `artifacts/unified-surface-proof/staging/lab-sse/receipt.json`
  sha256:944855c67d657c140cec42f7a3c4b49c6b2e36a95ba73eff0a181cd4798f860b (schema `leaf.unified-surface-proof.v1`,
  evidence tier `staging`, capability JB-02). The receipt stays outside git; this page cites it by path and hash only.
- Deployed source the receipt observed: `695a977c9ac5d461966f60b8fb46f6a83e06dc8b`.
- Staging app task definition at the time: `leaf-platform-app:1111`, which carries `LEAF_LAB_STORE=postgres`,
  `LEAF_LAB_RUN_STORE=postgres` and `LEAF_LAB_SURFACE_ENABLED=1`.

## Measured

| Check | Result | Budget |
| --- | --- | --- |
| POST response to first run frame, 20 samples, nearest rank | p50 711 ms, p95 970 ms | p95 at most 1000 ms |
| Heartbeat and health on a finished run | both at 15.4 s; stream held open 16.0 s | by 17 s |
| Resume with `Last-Event-ID` after a client abort | matches the durable suffix (cursor 0, 0 of 0 events) | full suffix |
| Missing bearer, token in the query string, wrong bearer | 401, 400, 401 | 401, 400, 401 |
| Cursor gap | not observed | see limitations |

The p95 sits 30 ms under its budget. Treat a staging p95 above 1000 ms on a later run as a regression, not noise.

## Limitations

- The proof graph is one small pure block, so the stream cannot exceed `MAX_EVENTS=10000` in
  `server/lab_run_store.py` and no gap is fabricated. The gap path is covered by
  `server/tests/test_lab_stream.py::test_gap_then_public_events`.
- A one-block graph has only a terminal event, so the resume check records a zero-length suffix. It shows the cursor
  protocol works; it does not prove replay of a nonempty suffix.
- Resume is measured across a client abort. A real deployment drain and a credential expiring mid-stream are not induced.
- The JB-02 ledger subcases (stream loss, fallback, reload reattach, duplicate suppression) remain unproven. This receipt
  measures the Lab API, not the converse or job UI fallback.

## How it was produced

The staging token and the expected subject hash were read in process from Secrets Manager
(`leaf-platform-acceptance-human-token-staging`, `leaf-platform-acceptance-human-fixture-staging`) under an SSO identity
gate, passed to the Playwright child only through its environment, and scrubbed from every log line. Rerun with
`npx playwright test e2e/staging/lab-sse.spec.mjs --config playwright.staging.config.mjs` from `web/` with
`LEAF_E2E_STAGING_JWT` and `LEAF_E2E_STAGING_EXPECTED_SUBJECT_SHA256` set; a missing credential fails the suite rather
than skipping it.
