# Saved Solar frame inspection

Parent: Studio18 S01, generated layout inspection within the shared Solar CAD project.

Integration: parent S01 PR 2888 merged as 5bd0388ef53e46df6f31a15799309484d5c734b6. G2 PR 2894 now targets main. Merge pinned main 2f0083d60ad0379aa4888dfab0b831f3ee80b021 into this branch without rewriting history. The two conflicts are S01 materialization and its tests; main's versions equal original parent 681a02bd, so preserve G2's existing bytes. Settled source 21985a65 remains an ancestor.

Scope: existing layout GET route, canonical materialization reader, publication client, Solar workspace tools, a frame inspector and focused tests. App.jsx stays outside this slice.

Acceptance: every saved frame is reachable through bounded pages; display canonical CAD handle, row, column, elevation, corners, preset pose and pile-template association with units. Refuse stale/foreign project versions and missing model associations. Clear a prior result when project/version changes or refresh fails. No geometry mutation or evaluator inputs.

Verification: focused Python materialization tests and Vitest client/workspace/inspector files, then exact-head full native CI owned by this worker. Preserve database-dependent skips, never count them as passes. Confirm the merged index differs from pinned main only in the original nine G2 files and passes changed-slice whitespace checks.

Ownership: exact remote claims commit bf2a16481d0762fbefc9d01c70a2c7ba143d5e15 was read without registry mutation; no matching S01 or planned file claim. Refreshed PR inventory has no cross-benchmark overlap in this slice.
