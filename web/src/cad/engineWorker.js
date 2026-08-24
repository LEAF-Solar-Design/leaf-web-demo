// CAD engine Worker-API boundary (dormant behind the cad_edit flag).
//
// This is the ONLY module allowed to know about a CAD engine Worker. Nothing
// else in the main bundle should import a CAD engine internal directly — see
// engineBoundary.test.js, which fences that path statically. The file is
// dual-mode: imported by the main thread it exposes EngineBoundary; loaded as
// a dedicated Worker (self.postMessage exists, window does not) it runs the
// worker-side echo loop below. No real WASM engine is wired in yet — that is
// future work behind the same boundary, not a main-bundle concern.

const REQUEST_SCHEMA = {
  init: { required: [] },
  loadDocument: { required: ['documentId'] },
  applyEdit: { required: ['op', 'payload'] },
  dispose: { required: [] },
}

const RESPONSE_SCHEMA = {
  ready: { required: [] },
  documentLoaded: { required: ['documentId'] },
  editApplied: { required: ['op', 'ok'] },
  error: { required: ['message'] },
}

const SCHEMAS = { toWorker: REQUEST_SCHEMA, fromWorker: RESPONSE_SCHEMA }

// Schema-validates a boundary message in either direction. Never throws —
// callers get a structured ok/reason result so a malformed message can be
// dropped with a counted receipt instead of reaching the UI as an exception.
export function validateBoundaryMessage(direction, raw) {
  const schema = SCHEMAS[direction]
  if (!schema) return { ok: false, reason: 'unknown_direction' }
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    return { ok: false, reason: 'not_an_object' }
  }
  const { type } = raw
  if (typeof type !== 'string' || !schema[type]) {
    return { ok: false, reason: 'unknown_type' }
  }
  const missing = schema[type].required.filter((key) => !(key in raw))
  if (missing.length > 0) {
    return { ok: false, reason: `missing_fields:${missing.join(',')}` }
  }
  return { ok: true, message: raw }
}

// cad_edit is dormant by default: an explicit flags.cad_edit wins (tests,
// callers that already resolved the flag), otherwise fall back to the build
// env var, otherwise off.
export function isCadEditEnabled(flags) {
  if (flags && Object.prototype.hasOwnProperty.call(flags, 'cad_edit')) {
    return !!flags.cad_edit
  }
  if (typeof import.meta !== 'undefined' && import.meta.env) {
    const value = import.meta.env.VITE_CAD_EDIT
    return value === 'true' || value === true
  }
  return false
}

function defaultCreateWorker() {
  if (typeof Worker === 'undefined') {
    throw new Error('engineWorker: Worker API is not available in this environment')
  }
  return new Worker(new URL(import.meta.url), { type: 'module' })
}

// Main-thread handle onto the (possibly not-yet-instantiated) engine worker.
// Construction never instantiates anything; start() only instantiates a real
// Worker when cad_edit is on, so an off flag is a true negative control —
// no worker, no thread, no wasted instantiation.

// Cap on retained drop receipts (droppedCount still counts every drop).
export const MAX_DROPPED_RECEIPTS = 32

export class EngineBoundary {
  constructor({ flags, createWorker } = {}) {
    this._enabled = isCadEditEnabled(flags)
    this._createWorker = createWorker || defaultCreateWorker
    this._worker = null
    this._listeners = new Set()
    this.droppedCount = 0
    this.droppedReceipts = []
  }

  get enabled() {
    return this._enabled
  }

  get instantiated() {
    return this._worker !== null
  }

  start() {
    if (!this._enabled) return false
    if (this._worker) return true
    const worker = this._createWorker()
    worker.addEventListener('message', (event) => this._handleIncoming(event.data))
    this._worker = worker
    return true
  }

  onMessage(listener) {
    this._listeners.add(listener)
    return () => this._listeners.delete(listener)
  }

  post(raw) {
    const result = validateBoundaryMessage('toWorker', raw)
    if (!result.ok) {
      this._recordDropped('toWorker', result.reason)
      return false
    }
    if (!this._worker) return false
    this._worker.postMessage(result.message)
    return true
  }

  terminate() {
    if (this._worker) {
      this._worker.terminate()
      this._worker = null
    }
  }

  _handleIncoming(raw) {
    const result = validateBoundaryMessage('fromWorker', raw)
    if (!result.ok) {
      this._recordDropped('fromWorker', result.reason)
      return
    }
    for (const listener of this._listeners) listener(result.message)
  }

  _recordDropped(direction, reason) {
    this.droppedCount += 1
    // Bounded ring, never an unbounded log: a flood of malformed messages
    // must cost O(MAX_DROPPED_RECEIPTS) memory, not O(flood). droppedCount
    // stays the full total; the ring keeps only the newest receipts.
    this.droppedReceipts.push({ direction, reason, at: Date.now() })
    if (this.droppedReceipts.length > MAX_DROPPED_RECEIPTS) {
      this.droppedReceipts.splice(
        0, this.droppedReceipts.length - MAX_DROPPED_RECEIPTS)
    }
  }
}

// Worker-side half of the dual-mode file. Guarded so importing this module on
// the main thread (jsdom, vitest, the app bundle) never touches `self` as a
// worker scope. No WASM engine is loaded yet — the echo loop only proves the
// boundary's validate-or-drop contract holds from the worker side too.
if (
  typeof window === 'undefined' &&
  typeof self !== 'undefined' &&
  typeof self.postMessage === 'function' &&
  typeof importScripts === 'function'
) {
  self.addEventListener('message', (event) => {
    const result = validateBoundaryMessage('toWorker', event.data)
    if (!result.ok) return
    if (result.message.type === 'init') {
      self.postMessage({ type: 'ready' })
    }
  })
}
