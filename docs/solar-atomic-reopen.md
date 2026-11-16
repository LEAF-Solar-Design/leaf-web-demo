# Failed-open atomicity for edited drawings

Parent: Studio18 S01 hardening of native Solar editing and Undo/Redo.

Opening a corrupt replacement used to clear the current edited bytes and history. The session now keeps the prior session, owned byte copies and history while parsing a replacement. If parsing returns an error or refusal, it reloads the trusted current bytes through the existing worker load path. Only a successful restoration seats the prior editable session and its exact Undo/Redo stacks. The user sees that the new drawing failed and the previous drawing remains open.

A successful replacement still creates a new history floor. An edit in flight blocks replacement. Reset or unmount drops pending restoration, and generation checks discard late replies. File reads retain the pre-read session as the restoration baseline. Existing first-open failure behavior remains unchanged. Immutable project-version selection retains its existing deliberate generation retirement; this change does not alter server read/save/restore semantics.

If trusted-byte restoration also fails, the session retains bytes for download, clears the unproved editable projection and history, and blocks save until a valid parse. It does not claim that displaying old entities proves the worker holds that document. A transport refusal before loading restores the prior state directly because the worker did not parse the replacement.

Focused checks from `web`:

```sh
npx vitest run src/cadedit/solarAtomicOpen.test.jsx src/cadedit/engineSession.test.jsx src/cadedit/engineUndo.test.jsx src/cadedit/solarByteOwnership.test.jsx
```

Ten new scripted-worker cases cover error/refusal restoration, existing Undo/Redo and redo branches, successful replacement, busy edit protection, reset isolation, failed restoration, false/throwing posts and File import. The existing assertions remain intact. The combined focused run passes 120 tests.

Separate local actual React/Node Worker/WASM proof uses a public synthetic 54-frame, two-model fixture. Truncated and malformed replacements fail on unchanged current main. With this fix, each keeps 78,847 edited bytes, the document projection and history. Undo/Redo is byte-exact, a subsequent native move succeeds, and a valid later replacement resets history. Two independent strict decodes prove cumulative frame movement of two units with all other document fields, parameters, scope and handles exact. This is local proof, not browser, service, staging or scored acceptance.

Scope excludes request deadlines, worker reply schema changes and new routes. PR3131's reserved original source lines 1366 through 1424 remain byte-for-byte unchanged. Rollback is a normal source revert; there is no persistent schema or provider change.

Selected CI correction retains the established loading document name so head refreshes keep their open reach state. A matching document-load reply also clears transient selection-batch context. Existing head-fetch cancellation and batch-lifetime assertions cover these paths.
