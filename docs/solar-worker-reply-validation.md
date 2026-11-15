# Solar worker reply integrity

Parent: the saved Solar drawing editor's native edit, Undo and save lifecycle.

A successful worker edit supplies both written bytes and its reparsed entity
projection. The session checks that the reply matches the pending operation,
carries a nonempty Uint8Array within the existing 64 MiB history bound, and
contains entities with finite vertices. An explicit byte length must match.
These are transport integrity checks, not DXF decoding or geometry equivalence.
The existing file-open and server publication limits remain separate.

An invalid success may mean that the worker already changed its document.
The session terminates that worker and invalidates its generation, projection
and history. It clears busy and exposes a typed transport error. It retains
the last trusted edited and committed bytes for download. Save cannot run
until a document is parsed again. Late replies from the terminated generation
cannot restore state. Opening again creates a fresh worker.

Valid matching refusals keep the current worker and document available for a
retry. Valid successes retain ordinary Undo and save behavior. Existing valid
transport replies without a pending operation stay compatible; this change
does not add request IDs or make duplicate replies idempotent.

From `web/`, run:

```sh
npx vitest run src/cadedit/solarWorkerReply.test.jsx src/cadedit/engineSession.test.jsx src/cadedit/engineUndo.test.jsx src/cadedit/solarByteOwnership.test.jsx src/cadedit/pendingPropertyEdit.test.jsx
```

The five new ordinary cases use a scripted Worker API transport with the real
React session and boundary. They inject missing bytes, a non-finite vertex
and a wrong-operation success after a trusted saved revision and another
unsaved edit. They check retained bytes, no unproved history or projection,
blocked publication, late-reply isolation and clean reopen/edit recovery.
A matching refusal remains recoverable. Two actions before React renders
keep the first operation pending without posting a second edit or clearing
busy. All existing assertions remain.

The explicit-target property fixture completes its first worker operation
before dispatching its second operation. Its existing two exact payload
assertions remain, with added checks for the pending and completed busy states.

These invalid shapes are synthetic fault controls. They are not observed
output from the native engine. Keep real worker/WASM, strict DXF, database,
browser and LIVE delivery evidence separate. This change neither proves
engine rollback nor claims that retaining an old projection would prove it.
