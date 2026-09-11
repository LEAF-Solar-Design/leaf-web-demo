# Marathon card replay

This is the marathon lane of the BuildQueueCard proof. A fixture pass proves
preparation only. It does not prove a live marathon, an oracle, or a deployment.

## Setup (not visible actions)

Use the repository-managed local stack and its existing start-leaf launcher.
Before starting that stack, create a dedicated empty temporary directory. Set
both `LEAF_MARATHON_RUNS_DIR` and `LEAF_E2E_MARATHON_FIXTURE_ROOT` to its absolute
path in the stack and Playwright environments. Set `LEAF_E2E_MANAGED=1`.
Use the stack's normal isolated local stores, local auth, `demo-tenant`, and
source SHA stamping. Do not reuse a live or retained runs root. Setting the
variable only in Playwright cannot configure an already running API process.

The browser test creates a unique `fixture-replay-*` run under
`<root>/demo-tenant/<run_id>/`. It writes state and a manifest, then advances
the fixture to verified and finally writes an explicitly synthetic promotion.
These writes are setup stimuli, not real producer history. The API is never
intercepted. The reader remains `server/marathon_runs.py:list_runs`.

From `web` in the configured managed environment:

```text
npx playwright test e2e/local/marathon-build-queue-card.spec.mjs --config=playwright.local.config.mjs --workers=1
```

## Visible actions and expected states

Route: `http://127.0.0.1:5275/app?surface=cad` (or the managed local base URL).
Viewport: 1600 by 1000. Enable the existing rail flag, open the route, and click
the running badge to expand Job monitor. Find the uniquely titled fixture card.

1. Running: fold lane, round 2, verified off, promoted off, cancel declared.
2. Verified without promotion: done, verified on, promoted off, promote declared.
3. Synthetic promotion: done, both marks on, no actions. This is a fixture
   artifact change only. Do not click Promote or invoke a deployment.

At each state retain the actual `GET /api/builds?limit=200` body with the
directory tenant binding, timestamp, source snapshot hashes, and a viewport
screenshot showing the card. Playwright output and the attached marathon
receipt are labeled `fixture-replay`. The shared proof metadata helper is
used in memory; its generic tier vocabulary is not emitted as marathon proof.

## Receipt validation and limits

```text
python scripts/marathon_card_evidence.py --check FILE --require-tier fixture-replay
```

The CLI prints one JSON verdict. Only an exact tier match exits zero.
The top-level keys are schema, evidence_tier, run_id, source_sha,
tenant_binding, source_artifacts, observed_records, screenshots, assertions.
Each observation has an observation_id, epoch-millisecond observed_at, run_id,
tenant_binding, capture_mode, explicit synthetic/reconstructed booleans,
source_artifacts filename references, and the unmodified api_result body.
Each screenshot has filename, sha256, observation_id, run_id, tenant_binding,
card_visible and card_state. Source snapshots have filename and sha256.

Retained real replay uses capture_mode `replay` and real retained producer or
oracle artifacts. Live proof uses capture_mode `live`, a contemporaneous
nonterminal observation followed by a terminal observation, visible evidence
for both, and real producer or oracle artifacts. Those artifacts declare kind
`producer` or `oracle` and synthetic/reconstructed false. Relabeling this
fixture receipt cannot meet those checks. An offline schema validator cannot
authenticate an author's declarations or prove screenshot contents; Fable
must inspect the retained bytes, provenance, and visible result. Never upgrade
a replay into live proof by editing its tier or reconstructing running state.

The reader scopes by tenant directory, skips symlinked runs, and skips missing,
oversized, malformed or non-object state with one counted warning. A manifest
is optional and is the only source of started/elapsed timing. Observation
timestamps never substitute for missing manifest start times.

## Stop conditions and cleanup

Stop on an unready stack, missing dedicated root, wrong tenant/run, missing
fold source, stale or missing card state, failed screenshots, or invalid
receipt. Do not patch API responses or claim a pass after a skipped test.
The test removes only its unique run directory in finally. Retain Playwright
artifacts for review. The stack owner stops the managed stack and removes its
dedicated temporary stores after collecting evidence. On an interrupted test,
remove only the recorded fixture run after confirming its path under that root.

Host context supplied by the planner on 2026-09-11: the marathon root was
unset and no live runs existed. No run is launched by this replay. Before any
later spend request, check for an already authorized live marathon and observe
it read-only where possible. Never launch a duplicate to obtain evidence.
