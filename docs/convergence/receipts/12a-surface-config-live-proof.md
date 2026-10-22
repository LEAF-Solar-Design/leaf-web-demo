# Ledger 12a receipt: surface-config live proof

Ledger row 12a ("Fold-eligible requests classified and served by commit and fold") is earned by a passing
surface-config LIVE PROOF on staging: an admin-tier principal commits a surface-config overlay, the committed
value is served and VISIBLY reflected in the rendered surface, and the baseline is restored.

## Result

`scripts/verify_surface_config_live.mjs` via `run_surface_config_proof_staging.mjs` against
`https://platform-staging.leafdesign.ai`, source revision `a6da0e3341361b0e562ada845fb994db76428b00`,
on an internal admin tenant. Receipt: `12a-surface-config-live-proof.json` (ok true), all six checks:

- `staging_identity_and_source` — verified admin session on staging at the exact source revision.
- `rendered_control_bound` — the authoring control renders when the committed overlay sets `cad.authoring` true.
- `exact_candidate_and_source` — the committed candidate overlay is read back exactly, with a fresh source receipt.
- `visible_change` — after committing `cad.authoring` false and reloading, the authoring control detaches (the
  committed change is served and rendered).
- `application_source_unchanged` — the deployment identity did not move during the proof.
- `baseline_get_and_visible_restored` — the baseline overlay is restored and re-rendered.

## Note on the harness fixes this proof depended on

The rendered leg surfaced two real defects, both fixed before this proof could pass:

- `useSurfaceContract` latched its fetch-once singleton on the initial demo/`mock` render, so after a `/try`
  session activated the real tenant overlay was never fetched and no committed surface-config reflected in the
  rendered surface. Fixed so the first real (non-mock) subscription fetches once.
- The staging `platform-entitlements` deploy step was not registered as a lease adapter, so its release was
  refused and left the staging mutation lease held. Fixed by registering it plus a coverage test over every
  leased deploy script.

The committed-value serve path was independently measured serving in well under a second.
