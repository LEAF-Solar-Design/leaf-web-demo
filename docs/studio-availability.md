# Studio availability manifest

One table decides which Studio shell features a visitor can use and which are shown disabled with a "not yet available" sentence: `web/src/site/studioAvailability.js`. It exists so that a visitor on staging never meets a control that fails, spins or errors. It is availability and messaging only. It never changes what an engine, a tool or the server will do.

## The rule for a row

| Evidence | Row |
| --- | --- |
| Works on staging, with a walk to point at | `status: 'enabled'`, `disclaimer: null` |
| Does not work | `status: 'unavailable'` plus a disclaimer |
| Unverified | Walk it on staging. Works: enabled. Fails: unavailable, and log the gap |

A disclaimer is short and plain, says the feature is not yet available and what it will do, and never promises a date. `web/src/site/studioAvailability.test.js` enforces the copy rules and pins every feature id a source file reads to a real row.

## Landing a feature

In the pull request that makes the feature work, set its row to `status: 'enabled'`, set `disclaimer: null`, and point `evidence` at the staging walk that proves it. Do not delete the row. The capability measure reads the table:

```
node web/scripts/studio_availability.mjs --json
```

## Where a row applies

`hosts` names the hostnames a row speaks for. A finding made on staging disables the control on `platform-staging.leafdesign.ai` only, so a local stack, the offline demo and a later production promotion keep their own behaviour until someone walks them. A row with no `hosts` holds everywhere because the code itself decides it.

## Walking a disabled feature

A lane fixing an unavailable feature has to walk it on staging before it may flip the row. Add `?availability=walk` to the address, or set `localStorage['leaf.availability.walk'] = '1'`, and every manifest disable lifts for that browser. It is a walking aid, never a grant: the server still decides what runs.

## Rows that adopt an existing sentence

Some controls already shipped disabled with their own honest reason (Print, the Model tab, Snap mode and Polar tracking). Those rows carry `adopted: true` and repeat the control's sentence byte for byte; the test knows each file and pins the two equal. No source path goes in the manifest itself, because it ships in the production bundle. The control keeps its own gate, because flipping such a row cannot build the feature.

## Evidence

The audit behind the first rows (click walks, journeys and screenshots at 1440 and 390, signed out and signed in) is in `leaf-research/studio-availability-overhaul-2026-11/`. Its README lists every finding, including the gaps this manifest does not cover.
