/**
 * documentWorker — the worker-side half of the cad_edit editing slice.
 *
 * Runs as a dedicated MODULE worker spawned by CadEditSurface.jsx. It speaks
 * the SAME message schema web/src/cad/engineWorker.js's EngineBoundary
 * already validates in both directions (init -> ready, loadDocument ->
 * documentLoaded, applyEdit -> editApplied, anything else -> error), so the
 * surface talks to it through the unmodified boundary and gets the
 * boundary's validate-or-drop contract for free. It imports the schema
 * validator from the boundary module rather than restating it: one source of
 * truth for the wire format, and the only cross-directory import this file
 * makes (the src/cad import fence in engineBoundary.test.js allows exactly
 * engineWorker.js and nothing else).
 *
 * The document model behind it is dxfLineDocument.js, the bounded LINE-subset
 * stand-in, NOT the vendored MPL-2.0 wasm CAD engine: that engine's worker
 * entry under vendor/ is a Node module (node:fs / node:path) with no browser
 * build, so it cannot be spawned from a page today. Swapping it in is a
 * change to THIS file's three engine calls and nothing above it — see
 * docs/CAD-EDIT-SURFACE-DESIGN.md. (The crate is deliberately not named from
 * web/: scripts/check_license_fence.py denies any reference to it here other
 * than the one legal Worker-spawn shape.)
 *
 * Contract: handleMessage NEVER throws and NEVER mutates state on a refused
 * edit. Exactly one document is held per worker; loadDocument replaces it,
 * dispose drops it, so worker memory is O(one bounded document), never
 * O(documents opened).
 */
// Importing engineWorker.js here also evaluates ITS dual-mode worker branch,
// which is inert in this scope by construction: that branch requires
// `typeof importScripts === 'function'`, and importScripts exists only in a
// CLASSIC worker. This is a module worker, so this file owns the message
// loop outright.
import { validateBoundaryMessage } from '../cad/engineWorker.js'

import {
  MAX_DOCUMENT_BYTES,
  applyEditToDocument,
  isByteArray,
  parseDxfDocument,
  serializeDxfDocument,
  writeRefusal,
} from './dxfLineDocument.js'

// The single held document. Replaced by loadDocument, cleared by dispose.
let current = null

// Read-only projection of the model handed to the UI: the surface renders
// this and nothing else, so it can never reach into the engine's internals.
function projectEntities(doc) {
  return doc.entities.map((entity) => ({
    id: entity.id,
    type: entity.type,
    layer: entity.layer,
    start: [entity.start[0], entity.start[1], entity.start[2]],
    end: [entity.end[0], entity.end[1], entity.end[2]],
  }))
}

function loadedResponse(documentId, doc, refusal) {
  return {
    type: 'documentLoaded',
    documentId,
    entityCount: doc.entities.length,
    entities: projectEntities(doc),
    // Truthful, named report of what this build read but cannot rewrite.
    // `writable: false` is what makes the edit controls refuse instead of
    // quietly discarding the rest of the drawing.
    writable: refusal === null,
    refusal,
    unsupported: doc.unsupported,
  }
}

/**
 * Handles one boundary message. Returns the response message, or null when
 * the message warrants no reply. Never throws.
 */
export function handleMessage(raw) {
  const inbound = validateBoundaryMessage('toWorker', raw)
  if (!inbound.ok) return { type: 'error', message: `bad_message:${inbound.reason}` }
  const message = inbound.message

  if (message.type === 'init') return { type: 'ready' }

  if (message.type === 'dispose') {
    current = null
    return null
  }

  if (message.type === 'loadDocument') {
    const { documentId, bytes } = message
    if (typeof documentId !== 'string' || documentId === '') {
      return { type: 'error', message: 'bad_document_id' }
    }
    // Length check BEFORE the parse, so an oversized payload costs a
    // comparison rather than a decode.
    const byteLength = isByteArray(bytes) ? bytes.length : -1
    if (byteLength > MAX_DOCUMENT_BYTES) {
      current = null
      return { type: 'error', message: `document_too_large:${byteLength}` }
    }
    const parsed = parseDxfDocument(bytes)
    if (!parsed.ok) {
      // Fail closed: a refused parse leaves NO document loaded, so a
      // following applyEdit cannot land on stale state.
      current = null
      return { type: 'error', message: `parse_failed:${parsed.reason}` }
    }
    const refusal = writeRefusal(parsed.doc)
    current = { documentId, doc: parsed.doc, refusal }
    return loadedResponse(documentId, parsed.doc, refusal)
  }

  if (message.type === 'applyEdit') {
    const { op, payload } = message
    if (!current) return { type: 'editApplied', op, ok: false, reason: 'no_document_loaded' }
    if (current.refusal !== null) {
      // The lossless-or-refuse gate. Refused BEFORE the edit is computed, so
      // there is no path on which an unwritable document is mutated.
      return { type: 'editApplied', op, ok: false, reason: `not_writable:${current.refusal}` }
    }
    const edited = applyEditToDocument(current.doc, op, payload)
    if (!edited.ok) return { type: 'editApplied', op, ok: false, reason: edited.reason }

    // The write-back leg: serialize the edited model to DXF bytes and
    // re-parse those bytes, so the state reported to the UI is what a
    // reader would actually see in the saved file — not the in-memory model
    // the UI just asked for.
    const written = serializeDxfDocument(edited.doc)
    const reparsed = parseDxfDocument(written)
    if (!reparsed.ok) {
      return { type: 'editApplied', op, ok: false, reason: `writeback_reparse_failed:${reparsed.reason}` }
    }
    current = { documentId: current.documentId, doc: edited.doc, refusal: writeRefusal(edited.doc) }
    return {
      type: 'editApplied',
      op,
      ok: true,
      entityCount: reparsed.doc.entities.length,
      entities: projectEntities(reparsed.doc),
      bytes: written,
      byteLength: written.length,
    }
  }

  return { type: 'error', message: `unhandled_type:${message.type}` }
}

// Worker-scope install. Guarded so importing this module on the main thread
// (jsdom under vitest, a Node test, the app bundle) never touches `self` as a
// worker scope: `window` is defined in jsdom and in the page, and `self` is
// undefined in Node, so only a real dedicated worker reaches this.
if (
  typeof window === 'undefined' &&
  typeof self !== 'undefined' &&
  typeof self.postMessage === 'function'
) {
  self.addEventListener('message', (event) => {
    const response = handleMessage(event.data)
    if (response) self.postMessage(response)
  })
}
