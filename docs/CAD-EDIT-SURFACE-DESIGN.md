# CAD editing surface — architecture and first slice

Status: **first slice landed** (`web/src/cadedit/`, behind `cad_edit`).
Envelope: `cad_edit` — *"Browser editing path skeleton behind the isolated
engine boundary"*, negative control *"With `cad_edit` off, editing routes
refuse and the editing UI never mounts; probe asserts both."*

This document describes the whole editing surface, then states exactly what
the first slice does and does not do. Read the "Not in the first slice"
section before quoting any capability from here.

---

## 1. The shape of the surface

```
  browser main thread                         isolated engine worker
  ─────────────────────                       ──────────────────────
  CadEditSurface.jsx                          documentWorker.js
    file input  ──── bytes ──┐                  ├─ dxfLineDocument.parse
    entity list  ◄───────────┤                  ├─ dxfLineDocument.applyEdit
    delete / move ───────────┤                  ├─ dxfLineDocument.serialize
    download   ◄─────────────┘                  └─ re-parse the WRITTEN bytes
             │                    ▲
             │                    │
             └──►  EngineBoundary ┘   (web/src/cad/engineWorker.js, unmodified)
                   schema-validates EVERY message in BOTH directions,
                   drops a malformed one with a counted receipt
```

Four rules hold everywhere on this surface:

1. **The engine is only ever reached through `EngineBoundary`.** The
   boundary validates `init` / `loadDocument` / `applyEdit` / `dispose`
   outbound and `ready` / `documentLoaded` / `editApplied` / `error`
   inbound. A malformed message is dropped with a counted receipt, never
   raised as an exception into React. `engineBoundary.test.js` fences this
   statically: nothing outside `web/src/cad/` may import a CAD engine module
   other than `engineWorker.js`, which is why the surface lives in
   `web/src/cadedit/` and not next to the boundary.
2. **The worker is spawned lazily.** Not at mount, not at import — on the
   first document open. With the flag off there is no component, so there is
   no worker, no thread, and no wasted instantiation.
3. **Lossless or refuse.** The surface never writes back a document it
   cannot represent in full (§4).
4. **No claim of byte fidelity, anywhere.** (§4.)

### How a user opens a drawing

Today: a local file picker (`.dxf`), read in-page to a `Uint8Array` and
posted through the boundary. Nothing is uploaded; nothing leaves the tab.

Later: the same `loadDocument` message, sourced from the project's canonical
drawing version instead of a local file. That is a change to where the bytes
come from, not to the boundary or the surface — which is why the file picker
is an acceptable first source rather than a throwaway.

### Edit operations

| Operation | State | Notes |
| --- | --- | --- |
| Delete a selected entity | **in the slice** | Entity ids stay stable across a delete, so a held selection keeps meaning the same entity. |
| Move a selected entity by (dx, dy) | **in the slice** | Bounded delta, overflow-checked post-condition. |
| Draw (line, rect, circle, arc) | later | Needs an insert op plus a canvas; the message schema already has room (`applyEdit` carries `op` + `payload`). |
| Trim / extend / offset / fillet | later | Needs real geometry, i.e. the wasm engine, not the LINE-subset stand-in. |
| Undo / redo | later | The model is already immutable per edit (`applyEditToDocument` returns a new document), so an undo stack is a worker-side ring, not a redesign. |
| Multi-select, marquee | later | |

The dormant ribbon skeleton at `web/src/cad/EditSurface.jsx` (card C1-6) is a
façade with no engine behind it and is **not** mounted anywhere. It is
superseded by this surface and should be retired in a follow-up; this slice
left it untouched rather than delete another card's landed acceptance oracle.

---

## 2. The engine behind the boundary

The vendored MPL-2.0 wasm CAD engine works end to end **in Node**: the crate
is rev-pinned under `vendor/`, the compiled wasm measures 2,004,823 bytes raw
/ 698,941 gzip, and the one-LINE DXF fixture round-trips exactly at the
entity level through the real wasm (see the day-3 spike doc under `docs/`).

It cannot be spawned from a browser page today, for one concrete reason: the
vendored worker entry is a **Node module** — it imports `node:fs` and
`node:path` to load fixtures from disk, and the compiled artifact that exists
is a `--target nodejs` build. A browser slice needs a `--target web` build
plus a byte source that is not the filesystem. Neither exists in this tree,
and building one needs a Rust toolchain that is not on this machine.

So the first slice runs a different engine behind the same boundary:
`web/src/cadedit/dxfLineDocument.js`, a bounded, fail-closed DXF LINE-subset
document model. It is deliberately narrow, and the swap to the real engine is
a change to `documentWorker.js`'s three engine calls (`parse`, `applyEdit`,
`serialize`) and nothing above them — the surface, the boundary, the message
schema and the tests above it do not move.

**This is a stand-in, and it is labelled as one everywhere it appears.** It
is not a general DXF engine and must never be described as one.

### Hardening contract of the stand-in

Stated in the module and enforced by `dxfLineDocument.test.js`:

* never throws — every entry point returns `{ ok: false, reason }`;
* every allocation bounded up front — `MAX_DOCUMENT_BYTES` (4 MiB),
  `MAX_GROUP_PAIRS` (400k), `MAX_ENTITIES` (20k), `MAX_EDIT_DELTA` (1e9). An
  oversized input costs a length comparison, not a decode;
* one linear pass over the group-code pairs; no whole-document regex, no
  rescan, no recursion;
* fails closed on a dangling group code, a non-numeric code, a non-finite
  coordinate, a nameless section, non-UTF-8 bytes, a non-byte-array input;
* a refused parse leaves **no** document loaded, so a following `applyEdit`
  cannot land on stale state;
* a refused edit never mutates — `applyEditToDocument` returns a new
  document and the worker only swaps its held document on success.

---

## 3. How an edit round-trips

1. `loadDocument { documentId, bytes }` → worker parses → `documentLoaded`
   carrying `entityCount`, the projected `entities`, `writable`, and a named
   `refusal` when it is not writable.
2. The user selects an entity and clicks Delete or Move.
3. `applyEdit { op, payload }` → worker applies the edit to its held
   document, **serializes the result to DXF bytes**, then **re-parses those
   bytes** and reports the entity count and entity list *from the re-parse*.
4. `editApplied { op, ok, entityCount, entities, bytes, byteLength }`.

Step 3 is the point. The number on screen after an edit is what a reader of
the saved file would actually see, not what the in-memory model was asked
for. If serialization silently lost the edit, the count would disagree and
the surface would say so.

---

## 4. Saving, given there is no byte fidelity

**Byte-identical round trip is not achievable and is not promised.** The real
`DxfWriter` emits a complete document (default table/object/class sections)
even from a minimal input, so the written bytes differ from the read bytes by
construction. Entity-level fidelity *is* achievable, and that is the only
fidelity claim this surface makes, in the doc, the code comments, and the UI
copy alike.

That forces a design decision about lossy writes, and the slice takes the
strict side:

> **Lossless or refuse.** The document model represents exactly the subset it
> can also write: a HEADER carrying `$ACADVER`, and an ENTITIES section of
> LINE records. Anything else in the source file — other sections (TABLES,
> BLOCKS, …), other entity types (CIRCLE, ARC, LWPOLYLINE, …), other header
> variables — is **read and reported**, and the write-back leg then refuses
> **by name**. It is never silently dropped.

Concretely, opening a real drawing shows the entities the model can read and
a visible read-only banner naming what it cannot rewrite; every edit control
is disabled and `applyEdit` refuses with `not_writable:<reason>` before the
edit is even computed. Silently re-serializing a drawing minus its TABLES is
data loss, not an edit — so the honest answer to "can I edit this DXF?" is
usually **no, and here is exactly why**.

**Saving is a download, not a persist.** The edited bytes come back over the
boundary and are offered as a `Blob` download. There is no server route, no
project version, no canonical-drawing write. `cad_edit` has no server-side
route at all on this revision, and this slice does not add one.

---

## 5. Staying behind the flag, and the license fence

**The flag.** `web/src/cadedit/flag.js` spells
`import.meta.env.VITE_CAD_EDIT === '1'` literally and nowhere else, with no
optional chain (an optional chain defeats Vite's static replacement and
silently turns the fence into a runtime check). `ToolCast.jsx` uses it as the
**first** operand:

```jsx
{ENV_CAD_EDIT && leftView === 'workspace'
  && !PUBLIC_DEMO && !transportMock && canOperate && workspace.openProjectId && (
  <CadEditSurface />
)}
```

That ordering is what lets Rollup drop the JSX, then the import, then the
whole surface. `!PUBLIC_DEMO` and `!transportMock` keep it off the public
`/try` demo and off any mock-transport session; `canOperate` and an open
project are the same operator gates the lifecycle panel uses.

**A known trap, now closed at the source.** `engineWorker.js`'s
`isCadEditEnabled` env fallback used to read `VITE_CAD_EDIT` as the string
`'true'` while this surface's build fence read it as `'1'` — a latent
split-brain that stayed invisible only because the surface passes the
already-resolved boolean explicitly (`flags: { cad_edit: true }`). Both
readers now compare against `'1'`, the repo-wide convention for every `VITE_*`
feature flag, so setting `VITE_CAD_EDIT=1` enables both consistently.

**The license fence.** `scripts/check_license_fence.py` allows references to
the vendored crate only under its own `vendor/` prefix and, from `web/`, only
inside the single legal `new Worker(new URL(..., import.meta.url))` spawn
shape. Nothing in `web/src/cadedit/` names the crate — an early draft did, in
prose comments, and produced 4 violations; the comments were rewritten. Both
`--self-test` and a full-tree scan are clean.

### Negative control

| Claim | Oracle |
| --- | --- |
| The editing UI never mounts with the flag off | `cadEditSurface.test.jsx`, flag-off and default-flag cases |
| The surface is not even *in* a flag-off build | `cadedit/bundleFence.test.js` — two real vite builds, markers present at `VITE_CAD_EDIT=1`, absent at `=0`, with the same positive control the lifecycle fence uses |
| No engine worker is spawned unless a document is opened | `cadEditSurface.test.jsx`, "never spawns the engine worker at mount" |
| No worker is instantiated with `cad_edit` off | `engineBoundary.test.js` (pre-existing) |
| No server route to refuse | `server/tests/test_cad_fence.py` — `cad_edit` has no server-side route on this revision, by design |

**Measured limitation of the build fence.** Vite's worker plugin emits the
worker chunk during *transform*, before tree-shaking, so a flag-off build
still writes the DXF engine bytes to `dist/assets/`. The fence measures the
consequence rather than assuming it away: with the flag off, the engine chunk
is an **orphan** — no entry, no other chunk, and not `index.html` names it —
so nothing ever fetches or executes it. The surface markers are genuinely
absent. Deleting those dead bytes outright needs a Rollup/Vite config change
and is future work.

---

## 6. Not in the first slice

Stated plainly so nobody quotes a capability this does not have.

* **No real CAD engine in the browser.** The LINE-subset stand-in is what
  runs. No arcs, circles, polylines, blocks, text, dimensions, layers beyond
  a name, colours, linetypes, or anything else.
* **Most real DXF files are read-only here** — anything with a TABLES
  section or a non-LINE entity, which is nearly every drawing a user has.
  The surface says so by name; it does not pretend.
* **No DWG.** DXF only.
* **No persistence.** No upload, no project version, no server route. Close
  the tab and the edit is gone unless it was downloaded.
* **No byte-identical round trip**, ever — see §4.
* **No canvas or visual preview.** Entities are a text list, not a drawing.
* **No undo/redo, no multi-select, no draw/trim/extend/offset/fillet.**
* **No real-Worker test coverage.** The component tests drive the real
  worker code through a transport double; jsdom has no `Worker`, so the
  actual browser `new Worker(new URL(...))` spawn is exercised by the vite
  build (it emits and references the chunk) and by hand, not by an automated
  browser test. An e2e spec is the honest next step.
* **The dormant C1-6 ribbon skeleton is still in the tree** and still not
  mounted.

## 7. Next steps, in order

1. `--target web` wasm build of the vendored engine + a browser byte source,
   then swap `documentWorker.js`'s three engine calls. This is the step that
   removes most of §6.
2. An e2e spec that drives a real `Worker` in a real browser.
3. Source the bytes from the project's canonical drawing version, and add
   the save-back route (which is where the envelope's "editing routes refuse"
   negative control becomes a real server-side assertion instead of a
   documented absence).
4. Retire `web/src/cad/EditSurface.jsx`. (The `VITE_CAD_EDIT`
   `'1'`-vs-`'true'` spelling split is done — every reader is on `'1'`.)
5. Drop the orphan worker chunk from flag-off builds.
