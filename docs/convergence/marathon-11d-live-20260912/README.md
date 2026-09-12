# Ledger 11d live evidence: marathon-11d-live-20260912 (2026-09-12)

A real marathon run, observed live through the real card, earning the `live-run` tier of
`leaf.marathon-card-proof.v1`. Validated by the merged validator on this same tree:

    PYTHONSAFEPATH=1 python scripts/marathon_card_evidence.py \
      --check docs/convergence/marathon-11d-live-20260912/live-run-marathon-card-receipt.json \
      --require-tier live-run
    -> {"ok": true, "evidence_tier": "live-run"}   exit 0

The same receipt exits 1 against `--require-tier fixture-replay` and
`--require-tier retained-real-run-replay`, so the tier claim is exclusive, not decorative.

## What actually ran

- Producer: `claudewalk/scripts/run_leaf_marathon.py` driving the real `MarathonRunner`,
  `--safety-mode unattended_strict`, executor and verifier `claude-sonnet-5`, one round,
  one attempt. Outcome `mission_complete: true`, oracle printed `CARD_ORACLE_PASS`,
  **spent_usd 0.3066** of an operator-authorized 50.00 ceiling.
- Topology: `LEAF_MARATHON_RUNS_DIR/demo-tenant/marathon-11d-live-20260912/` with
  `state.json` and `run-manifest.json`, exactly the layout `server/marathon_runs.py` reads.
- Serving stack: `scripts/start-leaf.py` local stack (APS_LIVE=0), app on 8230, web on 5275,
  from source `2e50668fba80` (the `lane/13a-world-space-mount-20260912` head; its diff against
  main `c341830f` is web-only and flag-off, `server/` byte-identical).
- Observation: a passive Playwright observer on the real UI at `/app?surface=cad`,
  1600x1000, polling the real `GET /api/builds` with the auth-off default tenant. It never
  intercepted a route and never wrote producer state. It captured the run NONTERMINAL
  (`state: "running"`, 18:30:02Z) and then TERMINAL (`state: "done"`, 18:31:07Z), each with
  the actual API body (`sources.fold: "runs-dir"`, exactly one matching fold run) and a
  screenshot showing the card live in the Job monitor.

## Files

| file | role |
| --- | --- |
| `live-run-marathon-card-receipt.json` | the validated receipt; every artifact below is hash-bound in it |
| `observations.json` | the observer's raw capture log |
| `nonterminal-api-builds.json`, `terminal-api-builds.json` | the actual API bodies at each observation |
| `nonterminal-marathon-11d-live-20260912.png`, `terminal-marathon-11d-live-20260912.png` | the card visible at each state |
| `state.json`, `run-manifest.json` | the producer's own run state and manifest |
| `oracle-card_live_proof.log` | the oracle's output (`CARD_ORACLE_PASS`) |

## Recorded honestly

- This is a LOCAL stack proof, per the scoped recipe: the run directory topology, reader,
  route, mapper and card are all the real production code paths; the deployment is local.
  A staging-tenant rerun is a separate observation if ever wanted, not owed by row 11d.
- A harness dry run against a hand-seeded fixture was used to debug the observer BEFORE the
  paid run, then deleted; it is retained outside the repo and cited nowhere in the receipt.
- The executor and verifier transcripts (`exec-*.claude.log`, `verifier-sessions.jsonl`) are
  deliberately not committed; the receipt does not reference them.
- Timing fields: the producer manifest carries `created_at`, the reader keys on `started_at`;
  timing was left absent rather than rewritten, per the replay doc's rule.
