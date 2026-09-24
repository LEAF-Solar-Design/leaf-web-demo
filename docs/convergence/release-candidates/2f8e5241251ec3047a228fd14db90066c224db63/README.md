# Release candidate 2f8e5241

Candidate `2f8e5241251ec3047a228fd14db90066c224db63` (tree `8088e232e0ae7560bbdfe38b6353d7a6087042c1`), frozen from main after the W6 wave. The operator authorized this candidate's production transaction; production serves it since 2026-09-24 07:56Z, promoted from `ae5dc847`. Checks 02 and 10 below were taken before that transaction, which is why they read PENDING.

## Checks

| # | check | verdict |
| --- | --- | --- |
| 01 | main == 2f8e5241251ec3047a228fd14db90066c224db63 | PASS |
| 02 | staging | PENDING |
| 03 | managed proof | PASS |
| 04 | Start board | PASS |
| 05 | surfaces | PASS |
| 06 | SSD2 | PASS |
| 07 | SSD1 | PASS |
| 08 | SSD3 and SSD5 | PASS |
| 09 | production plan | PASS |
| 10 | production smoke | PENDING |
| 11 | auth ladder | PASS |
| 12 | hardening | PASS |
| 13 | door PR | PASS |
| 14 | operator items | OPERATOR |

## What staging served

Staging served `6c38cfea`, a descendant of the candidate, while these checks ran. The runtime-equivalence receipt binds the two: the descendant deletes nothing, changes no web file, touches no existing runtime file, and nothing outside its own new modules and tests imports the 4 modules it adds. So the staging rows below were measured on a build whose runtime equals the candidate's. Checks 02 and 10 read PENDING for that reason and because production still served the previous build when they ran. The candidate itself then went to all five staging services (row staging-candidate-release), and the push relay moved app to the next main merge within minutes.

## Evidence

Each row names its source, where it was measured, the command, the counts and its evidence tier. `evidence.json` beside this file carries the same rows as data.

| result | source | origin | tier | counts |
| --- | --- | --- | --- | --- |
| staging-served | `6c38cfea` | staging | staging | `{"origins_serving":2,"origins":2,"serves_candidate":false}` |
| staging-candidate-release | `2f8e5241` | staging (all five services, read back by field) | staging | `{"services_on_candidate":5,"services":5}` |
| managed-proof | `2f8e5241` | local managed stack (web/scripts/run_unified_local_proof.ps1, VITE_CAD_EDIT=1, APS_LIVE=0) | local-e2e | `{"passed":84,"failed":0,"skipped":1,"flaky":1}` |
| start-board | `6c38cfea` | staging | staging | `{"rows":4,"passing":4}` |
| surfaces | `6c38cfea` | staging | staging | `{"profiles":4}` |
| ssd2-markers | `6c38cfea` | staging | staging | `{"served":25,"total":25}` |
| ssd1-elements | `2f8e5241` | local managed stack plus the console spec files and one pytest row | local-e2e | `{"proven":24,"total":24}` |
| live-plan-acceptance | `2f8e5241` | staging (the rail's plan-live-save-acceptance step) | staging | `{"contract_2":{"outcome":"refused","acceptance_outcome":null,"source":"2f8e5241","reason":"deployment identity source mismatch: app"},"contract_3":{"outcome":"r` |
| production-plan | `2f8e5241` | production (adapter --plan, zero writes by construction) | contract | `{"exit":0,"planned_operations":11,"promotion":"already_public"}` |
| production-smoke | `ae5dc847` | production (read-only) | production | `{"rows":8,"passed":8}` |
| production-terminal | `2f8e5241` | production (read-only, after the transaction) | production | `{"endpoints_serving_candidate":4,"endpoints":4,"pass":true,"smoke":["8"]}` |
| auth-ladder | `6c38cfea` | staging and production | staging | `{"staging":[401,200,403],"production":[401,200,403]}` |

- staging-candidate-release: released after the checks above; the push relay moved app to the next main merge within minutes, so the staging live acceptance could not bind one source (see live-plan-acceptance)
- live-plan-acceptance: refused at the identity precondition, which needs one source on all five staging services; the push relay moved app to the next main merge after the candidate's staging release. The last passing runs were on 74136c70 (2026-09-22), and no file the mutation Activity bakes differs between 74136c70 and the candidate
- production-smoke: a baseline of the build production served before the transaction; the candidate's own smoke is row production-terminal

### Managed proof against the baseline

Baseline main `2ccbeccd`: 78 passed, 3 failed, 1 skipped. Candidate: 84 passed, 0 failed, 1 skipped, 1 flaky.

- fixed since the baseline: e2e\local\cockpit-viewports.spec.mjs › shared phone workspace › all four profiles share one band and exclusive bottom drawers
- fixed since the baseline: e2e\local\one-shell-mount.spec.mjs › J1 row1, J1 row2, J1 row4, J1 row5, J1 row8: served Browser panes, first run, material and drawing-profile continuity
- fixed since the baseline: e2e\local\uploaded-drawing-run.spec.mjs › an uploaded DXF remains the authorized target of a catalog run
- new failures: none
- re-run alone after a timing failure in the full walk: e2e\local\one-shell-mount.spec.mjs › route matrix, rail ON › W4g bleed-2b: profile switches settle, fade inertly and keep one canvas (passed alone)

### Production plan

Exit 0, 11 planned operations, promotion `already_public`, zero writes. Image digests:

- app: `sha256:9d5f50f1cc5635b604ab43b113c85d83c4ab1f1d3f20a2f7260ed2e45e7f0a2a`
- broker: `sha256:0442cc3479d431b8b091dfa742153233523f354f6a3f2dc5fbc4a2798477acfa`
- canonical-worker: `sha256:e2bdd0cb39ab38174aa518ca656a2c1d6cee90f65fdc5688a10c62846ed7beb9`
- harness: `sha256:44b92b87a7b8ddbfcb350e9247dbe4e0fe8f0cf0faf77578238210994fb6d806`
- web: `sha256:bb12e4ce43b548a72cb8a9530149d975ebd9b03036c886229fb048f7d0d89448`

## Rollback

- production: re-run the activation adapter with the recorded baseline: production returns to ae5dc847, its five image digests and its three task-definition revisions as recorded before the transaction
- staging: staging follows main through the relay; a revert of the offending merge on main is the rollback
- door: unchanged: the door already forwards to the production origin

## Unresolved decisions

- P-001 (operator): DOD6 signatures (gate public launch, not the technical promotion: decision 4A)
- P-002 (operator): counsel review
- P-003 (operator): Autodesk invoice US$1,620 due 2026-10-04 (money)
- P-006 (operator): trademark / public branding hold (gates public launch, decision 4A)
- P-074 (operator): the nine Postgres decisions (gates Open a project)
- P-071 (operator): E2B template and author broker gateway (money)
- P-020 (operator): the 12a live tenant credential
- P-142 (operator): Studio SKU (pricing decision, leaf_website)
- P-145 (operator): billing chain (leaf_website, after P-142)
- iOS (operator (iOS lane)): the catalog projection activation apply, the owner's approve click, and fact (b) on project c6dbda41
- #106 (operator): CODE_MAP_PR_TOKEN for ops-dashboard

## Resolved

- PROMOTE: the operator's yes for this candidate (2026-09-24 07:05Z); production transaction ok at 07:56Z

## Commits since the previous production candidate

- 2f8e5241 Phone workspace row: a budget that fits its measured walk (#1413)
- 2c8e1e8a Browser board: host the project's campaign and summary panels instead of lying under them (#1412)
- b027e014 Build rail: say when the build feed is stale and how to recover (#1410)
- 005aefa1 Start board: a real-size catalog never covers the Create project button (#1411)
- 8006febb Solar local graph commit end to end, and the session seats the real head (#1407)
- 6c458c1d Harness readiness answers for its own stores; the app asks the harness (#1409)
- 719007b3 ci(gate): --jobs auto may use up to 16 workers (#1408)
- 43863824 Managed proof runner provisions an isolated marathon fixture root (#1406)
- 4610d5d1 Production smoke: the served build must be the candidate with the rail on (#1405)
- 2928e28f Candidate verifier: judge a repeat release, not only the first promotion (#1404)
- 032b4c6b Viewer: an edit keeps the view the drafter moved (#1403)
