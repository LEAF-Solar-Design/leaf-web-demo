# Studio18 S03 project-bound Solve preparation

Observed: LIVE Rooftop Solve asks for raw request JSON and frame ID after original whole-roof grouping. Derive the request on the server from the current saved graph; present named frames and an existing solver connection reference. Never create grants or infer tenant authority.

Owned scope: opt-in solve preparation on existing authenticated context GET, a pure preparation helper, project client option, prepared Solve form, workspace wiring and colocated tests/docs. Do not edit handed S02 graph-budget slices or foreign route inventories. No new route.

Acceptance: preparation uses validated graph/digest and current head; exact chosen frame and recorded global length; no writes, network, credential/grant resolution; existing context response unchanged by default. Wrong project/tenant/version cannot obtain preparation. UI submits the server request only after fresh bound context, refuses missing/stale/refused preparations, and retains existing unknown-outcome/no-resubmit safeguards. Correction form remains intact. Broker grant validation is unchanged.

Verification: registered test_sip_r1_routes.py and test_w1_solve_split.py; ProjectSolveReview, ProjectSolarWorkspace and solarProjectClient Vitest suites. Add meaningful negative cases for stale/missing/mismatched prepared requests. Baseline Python 73 passed. First local Vitest attempt failed setup-file loading across D:/C: junction spelling before collecting tests; retry uses canonical C: workdir. No assertion or timeout changes.

- [x] Protected context preparation and negative tests
- [x] Client and prepared Solve form, scoped stale/outcome tests
- [x] Focused checks and diff review:76Python and176web passed; no assertion removed
- [ ] Publish commit and PR request; native selected serialized after3497
