// Project custody for the browser editor. Transports own authentication; this
// adapter owns the captured project/version and resumable publication request.
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const SHA = /^[0-9a-f]{64}$/
const MAX_BYTES = 16 * 1024 * 1024
const isUuid = (value) => typeof value === 'string' && UUID.test(value)
const isBytes = (value) => Object.prototype.toString.call(value) === '[object Uint8Array]'

function refused(message = 'The project drawing changed. Open its current version and try again.') {
  return Object.assign(new Error(message), { status: 409, code: 'project_editor_scope_changed' })
}

function scopeSnapshot(value, requireDrawing = false) {
  if (!value || !isUuid(value.organization_id) || !isUuid(value.project_id)) throw refused()
  const drawing = value.drawing_id ?? null
  const version = value.input_version_id ?? null
  if ((drawing !== null || version !== null || requireDrawing) && (!isUuid(drawing) || !isUuid(version))) throw refused()
  return Object.freeze({ organization_id: value.organization_id, project_id: value.project_id,
    drawing_id: drawing, input_version_id: version })
}

export function projectDrawingEditorScopeKey(scope) {
  if (!scope) return ''
  return JSON.stringify([scope.organization_id, scope.project_id, scope.drawing_id ?? null, scope.input_version_id ?? null])
}

async function digestBytes(bytes) {
  const hash = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(hash), (value) => value.toString(16).padStart(2, '0')).join('')
}

function versionReceipt(receipt, scope, importing = false) {
  const version = receipt?.drawing_version ?? (importing ? receipt?.drawingVersion : null)
  if (!version || !isUuid(version.version_id) || !isUuid(version.drawing_id)
      || version.project_id !== scope.project_id || version.org_id !== scope.organization_id
      || (!importing && version.drawing_id !== scope.drawing_id)
      || !Number.isSafeInteger(version.seq) || version.seq < 1) {
    throw Object.assign(refused('The saved project drawing could not be verified.'), { outcomeUnknown: true })
  }
  return version
}

function verifyPublication(receipt, scope, requestId, expectedDigest = null) {
  const version = versionReceipt(receipt, scope)
  if (receipt.organization_id !== scope.organization_id || receipt.project_id !== scope.project_id
      || receipt.drawing_id !== scope.drawing_id || receipt.parent_version_id !== scope.input_version_id
      || receipt.request_id !== requestId || !SHA.test(receipt.source_sha256)
      || !SHA.test(receipt.intake_sha256) || (expectedDigest && receipt.source_sha256 !== expectedDigest)
      || version.version_id === scope.input_version_id) {
    throw Object.assign(refused('The saved project drawing could not be verified.'), { outcomeUnknown: true })
  }
  return Object.freeze({ ...receipt, drawing_version: Object.freeze({ ...version }) })
}

// publishHeld(kind, scope, value, { requestId }) publishes through a project checkout that is
// already held elsewhere and stays private there. It resolves the receipt, resolves null when no
// such checkout is held for this exact scope, or throws the refusal. This adapter never sees it.
export function createProjectDrawingEditor({ getScope, importFile, transport, getCapability = () => null,
  acquireCheckout, releaseCheckout, publishHeld = null,
  newRequestId = () => globalThis.crypto.randomUUID() } = {}) {
  if (typeof getScope !== 'function' || !transport || typeof transport.read !== 'function'
      || typeof transport.save !== 'function' || typeof transport.restore !== 'function') {
    throw new TypeError('Project editor requires its scoped transport.')
  }
  let currentKey = null
  let generation = 0
  let loadGeneration = 0
  const pending = new Map()

  function capture(requireDrawing = false) {
    const scope = scopeSnapshot(getScope(), requireDrawing)
    const key = projectDrawingEditorScopeKey(scope)
    if (currentKey !== key) {
      currentKey = key
      generation += 1
    }
    return { scope, key, generation }
  }

  function assertCurrent(attempt) {
    const now = capture()
    if (attempt.generation !== now.generation || attempt.key !== now.key) throw refused()
  }

  async function openFile(file, captured = capture()) {
    assertCurrent(captured)
    const load = ++loadGeneration
    if (typeof importFile !== 'function' || !file || !Number.isSafeInteger(file.size)
        || file.size <= 0 || file.size > MAX_BYTES) throw refused('Choose a DXF drawing up to 16 MiB.')
    const receipt = await importFile(file, captured.scope)
    assertCurrent(captured)
    if (load !== loadGeneration) throw refused()
    const version = versionReceipt(receipt, captured.scope, true)
    return Object.freeze({ ...receipt, drawing_version: Object.freeze({ ...version }), projectOperation: 'import' })
  }

  async function readVersion(versionId, captured = capture(true)) {
    assertCurrent(captured)
    const load = ++loadGeneration
    if (!isUuid(versionId)) throw refused()
    const scope = Object.freeze({ ...captured.scope, input_version_id: versionId })
    const result = await transport.read(scope)
    assertCurrent(captured)
    if (load !== loadGeneration) throw refused()
    if (!isBytes(result?.bytes) || result.bytes.length === 0 || result.bytes.length > MAX_BYTES
        || !SHA.test(result.source_sha256)) throw refused('The project drawing bytes could not be verified.')
    const bytes = new Uint8Array(result.bytes)
    const digest = await digestBytes(bytes)
    assertCurrent(captured)
    if (load !== loadGeneration) throw refused()
    if (digest !== result.source_sha256) throw refused('The project drawing bytes could not be verified.')
    return Object.freeze({ bytes, source_sha256: digest, versionId,
      documentId: typeof result.documentId === 'string' && result.documentId ? result.documentId : 'project.dxf' })
  }

  async function publish(kind, value, parent, expectedDigest, captured) {
    assertCurrent(captured)
    const scope = captured.scope
    if (!scope.drawing_id || parent !== scope.input_version_id) throw refused()
    let bytes = null
    if (kind === 'save') {
      if (!isBytes(value) || value.length === 0 || value.length > MAX_BYTES) throw refused('Choose a DXF drawing up to 16 MiB.')
      bytes = new Uint8Array(value)
      const computed = await digestBytes(bytes)
      assertCurrent(captured)
      if (!SHA.test(expectedDigest) || computed !== expectedDigest) throw refused('The edited drawing bytes changed before saving.')
    } else if (!isUuid(value)) throw refused()
    const material = kind === 'save' ? expectedDigest : value
    const key = JSON.stringify([captured.key, kind, material])
    const artifactKey = JSON.stringify([scope.organization_id, scope.project_id, scope.drawing_id])
    let request = pending.get(artifactKey)
    if (request && request.key !== key) throw refused('Check the previous save before saving a different drawing change.')
    if (request?.running) throw refused('Wait for the drawing to finish saving, then try again.')
    if (!request) {
      const requestId = newRequestId()
      if (!isUuid(requestId)) throw refused('The save request could not be created.')
      request = { key, requestId, bytes, capability: null, acquired: false, running: false }
      pending.set(artifactKey, request)
    }
    request.running = true
    let settled = false
    try {
      // A held project checkout publishes first: acquiring a second one would be refused.
      if (!request.capability && typeof publishHeld === 'function') {
        const held = await publishHeld(kind, scope, kind === 'save' ? request.bytes : value,
          { requestId: request.requestId })
        if (held !== null && held !== undefined) {
          const receipt = verifyPublication(held, scope, request.requestId, expectedDigest)
          settled = true
          assertCurrent(captured)
          return Object.freeze({ ...receipt, projectOperation: kind })
        }
        assertCurrent(captured)
      }
      if (!request.capability) {
        request.capability = getCapability(scope)
        if (!request.capability) {
          if (typeof acquireCheckout !== 'function') throw refused('Take the project edit lock before saving.')
          const acquired = await acquireCheckout(scope)
          request.capability = typeof acquired === 'string' ? acquired : acquired?.capability ?? acquired?.checkout_capability
          request.acquired = true
        }
        if (typeof request.capability !== 'string' || !request.capability) throw refused('Take the project edit lock before saving.')
      }
      assertCurrent(captured)
      const options = { requestId: request.requestId, capability: request.capability }
      const result = kind === 'save'
        ? await transport.save(scope, request.bytes, options)
        : await transport.restore(scope, value, options)
      const receipt = verifyPublication(result, scope, request.requestId, expectedDigest)
      settled = true
      assertCurrent(captured)
      return Object.freeze({ ...receipt, projectOperation: kind })
    } catch (error) {
      if (error?.outcomeUnknown !== true && Number.isInteger(error?.status) && error.status >= 400 && error.status < 500) settled = true
      throw error
    } finally {
      request.running = false
      if (settled) {
        if (pending.get(artifactKey) === request) pending.delete(artifactKey)
        if (request.acquired && typeof releaseCheckout === 'function') {
          try { await releaseCheckout(scope, request.capability) } catch { /* publication remains settled */ }
        }
      }
    }
  }

  function getSaveTarget() {
    const captured = capture()
    return Object.freeze({ kind: 'project', scopeKey: captured.key, headVersion: captured.scope.input_version_id,
      drawingId: captured.scope.drawing_id, openFile: (file) => openFile(file, captured),
      readVersion: (versionId) => readVersion(versionId, captured),
      save: (bytes, parent, digest) => publish('save', bytes, parent, digest, captured),
      restoreVersion: (versionId) => publish('restore', versionId, captured.scope.input_version_id, null, captured) })
  }

  return Object.freeze({ getSaveTarget, openFile, readVersion,
    save: (bytes, parent, digest) => publish('save', bytes, parent, digest, capture(true)),
    restoreVersion: (versionId) => { const captured = capture(true); return publish('restore', versionId, captured.scope.input_version_id, null, captured) },
  })
}
