# Saved Solar frame inspection

Parent: Studio18 S01, generated layout inspection within the shared Solar CAD project.

Stack: held S01 PR 2888, source 681a02bd4e1fece7fe9adbc59edbe7db4fccaf03. Settled source 21985a65 is an ancestor.

Scope: existing layout GET route, canonical materialization reader, publication client, Solar workspace tools, a frame inspector and focused tests. App.jsx stays outside this slice.

Acceptance: every saved frame is reachable through bounded pages; display canonical CAD handle, row, column, elevation, corners, preset pose and pile-template association with units. Refuse stale/foreign project versions and missing model associations. Clear a prior result when project/version changes or refresh fails. No geometry mutation or evaluator inputs.

Verification: focused Python materialization tests and Vitest client/workspace/inspector files. Preserve database-dependent skips as pending root CI, never passes.

Ownership: exact remote claims commit bf2a16481d0762fbefc9d01c70a2c7ba143d5e15 was read without registry mutation; no matching S01 or planned file claim. Refreshed PR inventory has no cross-benchmark overlap in this slice.
