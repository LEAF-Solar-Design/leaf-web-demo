# Solar editor byte ownership

Parent: the saved Solar drawing editor's Undo, Redo and save lifecycle.

The editor accepts caller-owned Uint8Array views and exposes saved bytes for
downloads. Those arrays are mutable. A caller changing one must not change
the document snapshot later used by Undo, Redo or save.

The session retains one private copy for each referenced snapshot. It keeps
the existing public reference when its contents are unchanged. When bytes
have changed, history and save use a retained copy. Opening copies the
supplied view boundaries and refuses detached input before seating a new
document. Saving seats the retained bytes as both current history and the
committed revision, so a later edit and Undo return to a clean saved state.
Reset and document teardown release the snapshot map.

Run the ordinary regression suite from `web/`:

```sh
npx vitest run src/cadedit/solarByteOwnership.test.jsx src/cadedit/engineUndo.test.jsx src/cadedit/engineSession.test.jsx
```

The four ownership cases need no compiled engine, service, credentials or
project. Worker replies and the save callback are scripted. The cases cover
caller mutation before the first edit, mutation of exposed saved bytes,
detached input with later recovery, and saving then editing/undoing back to
the retained committed revision. Existing identity assertions stay intact.

This protects session snapshots; it does not turn public TypedArrays into
readonly objects or prove a browser/service publication. Retained snapshots
cost one additional byte copy while their corresponding arrays remain
referenced. Checking whether a public array changed takes time proportional
to its byte length. Keep real-worker and LIVE receipts separate from this
ordinary unit coverage when reusing the behavior in another workspace.
