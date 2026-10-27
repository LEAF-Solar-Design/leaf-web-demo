import { track } from '../telemetry.js'
import { getDiagnostics, sanitizeDetail } from './diagnostics.js'

export const LEAF_PLATFORM_CONTRACT_VERSION = 'leaf.platform.v1alpha1'
export const PROTOCOL_VERSION = 'leaf.web-bridge.v1'
export const DISPATCH_MODE = 'autocad_idle_document_lock'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const IDENTITY_KEYS = ['platformTenantId', 'projectId', 'drawingId', 'drawingVersionId']
const unavailable = () => ({ status: 'unavailable', ready: null, bindingGrantVersion: null, selectedObjectId: null, selectedHandles: null, lastCommand: null, helloSentAt: null })
const isRecord = (value) => !!value && typeof value === 'object' && !Array.isArray(value)
const isObjectId = (value) => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,200}$/.test(value) && value === value.trim()

function normalizeHandles(value) {
  if (!Array.isArray(value) || value.length < 1 || value.length > 1000) return null
  const handles = []
  const seen = new Set()
  for (const handle of value) {
    if (typeof handle !== 'string' || !/^[0-9A-Fa-f]{1,16}$/.test(handle) || handle !== handle.trim()) return null
    const normalized = handle.toUpperCase()
    if (seen.has(normalized)) return null
    seen.add(normalized)
    handles.push(normalized)
  }
  return handles
}

function dotNetTimestamp(date) {
  return date.toISOString().replace('Z', '0000+00:00')
}

function signedTimestamp(value) {
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,7}))?(Z|[+-]\d{2}:\d{2})$/.exec(value)
  if (!match || match[0] !== value) return null
  return `${match[1]}.${(match[2] || '').padEnd(7, '0')}${match[3] === 'Z' ? '+00:00' : match[3]}`
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (isRecord(value)) return `{${Object.keys(value).sort().map((key) =>
    `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  return JSON.stringify(value)
}

function base64Bytes(value) {
  return Uint8Array.from(atob(value), (character) => character.charCodeAt(0))
}

function unsigned(envelope) {
  return {
    protocolVersion: envelope.protocolVersion, sessionId: envelope.sessionId,
    messageId: envelope.messageId, issuedAt: envelope.issuedAt,
    expiresAt: envelope.expiresAt, origin: envelope.origin, verb: envelope.verb,
    drawingFingerprint: envelope.drawingFingerprint,
    drawingRevision: envelope.drawingRevision, payload: envelope.payload,
  }
}

async function signature(value, keyBase64) {
  const key = await crypto.subtle.importKey('raw', base64Bytes(keyBase64),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
  const bytes = await crypto.subtle.sign('HMAC', key,
    new TextEncoder().encode(canonical(unsigned(value))))
  return [...new Uint8Array(bytes)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
}

async function signatureIsValid(value, supplied, keyBase64) {
  if (!/^[a-f0-9]{64}$/.test(supplied)) return false
  const issuedAt = signedTimestamp(value.issuedAt)
  const expiresAt = signedTimestamp(value.expiresAt)
  if (!issuedAt || !expiresAt) return false
  const bytes = Uint8Array.from(supplied.match(/.{2}/g), (pair) => Number.parseInt(pair, 16))
  const key = await crypto.subtle.importKey('raw', base64Bytes(keyBase64),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['verify'])
  return crypto.subtle.verify('HMAC', key, bytes,
    new TextEncoder().encode(canonical({ ...unsigned(value), issuedAt, expiresAt })))
}

function isHandshake(value, kind, extra) {
  if (!isRecord(value)) return false
  const exact = ['kind', 'protocolVersion', 'contractVersion', 'sessionId', 'nonce', 'origin',
    'sessionKey', 'connectedAt', 'documentFingerprint', ...extra]
  return Object.keys(value).length === exact.length && exact.every((key) => Object.hasOwn(value, key)) &&
    value.kind === kind && value.protocolVersion === PROTOCOL_VERSION &&
    value.contractVersion === LEAF_PLATFORM_CONTRACT_VERSION &&
    typeof value.sessionId === 'string' && value.sessionId.length >= 16 &&
    typeof value.nonce === 'string' && value.nonce.length >= 16 &&
    typeof value.sessionKey === 'string' && /^[A-Za-z0-9+/]{43}=$/.test(value.sessionKey) &&
    typeof value.connectedAt === 'string' && Number.isFinite(Date.parse(value.connectedAt)) &&
    typeof value.origin === 'string' && typeof value.documentFingerprint === 'string' &&
    /^sha256:[a-f0-9]{64}$/.test(value.documentFingerprint)
}

function isBridgeEndpoint(value) {
  if (!isRecord(value)) return false
  const prototype = Object.getPrototypeOf(value)
  if (prototype !== Object.prototype && prototype !== null) return false
  return Reflect.ownKeys(value).every((key) => key === 'hostProcessId'
    ? Number.isSafeInteger(value[key]) && value[key] > 0
    : ['pipeName', 'hostVersion', 'readySentinelPath'].includes(key) &&
      typeof value[key] === 'string' && value[key].length <= 512)
}

function isReady(value) {
  if (!isRecord(value)) return false
  const hasEndpoint = Object.hasOwn(value, 'bridgeEndpoint')
  const extras = [...IDENTITY_KEYS, ...(hasEndpoint ? ['bridgeEndpoint'] : []),
    ...(Object.hasOwn(value, 'syncCursor') ? ['syncCursor'] : [])]
  return isHandshake(value, 'host_bridge_ready', extras) &&
    (!Object.hasOwn(value, 'syncCursor') || (isRecord(value.syncCursor) &&
      Object.keys(value.syncCursor).length === 2 &&
      typeof value.syncCursor.drawingStoreId === 'string' && /^[a-z0-9][a-z0-9_-]{0,62}$/.test(value.syncCursor.drawingStoreId) &&
      Number.isSafeInteger(value.syncCursor.version) && value.syncCursor.version > 0)) &&
    (!hasEndpoint || isBridgeEndpoint(value.bridgeEndpoint)) &&
    IDENTITY_KEYS.every((key) => typeof value[key] === 'string' && UUID.test(value[key]))
}

function isUnbound(value) {
  if (!isRecord(value)) return false
  const hasGrantVersion = Object.hasOwn(value, 'bindingGrantVersion')
  return isHandshake(value, 'host_bridge_unbound', hasGrantVersion ? ['drawingRevision', 'bindingGrantVersion'] : ['drawingRevision']) &&
    (!hasGrantVersion || (Number.isInteger(value.bindingGrantVersion) && value.bindingGrantVersion === 1)) &&
    value.drawingRevision === '00000000-0000-0000-0000-000000000000'
}

/** Signed WebView2 channel. Session keys stay in memory and are used only for HMAC. */
export class LeafHostBridge {
  constructor({ channel, location, replayCapacity = 4096, diagnostics = getDiagnostics(), now = Date.now } = {}) {
    this.injectedChannel = channel
    this.location = location
    this.replayCapacity = replayCapacity
    this.channel = null
    this.state = unavailable()
    this.listeners = new Set()
    this.seenHostMessages = new Map()
    this.pendingCommand = null
    this.diagnostics = diagnostics
    this.now = now
    this.telemetryTimes = new Map()
    this.rejectedCount = 0
    this.handshakeTimer = null
    this.bindTimer = null
  }

  diagnose(phase, detail = {}) {
    const safe = sanitizeDetail(phase, detail)
    try { this.diagnostics.record(phase, safe) } catch { /* diagnostics must not break the bridge */ }
    if (!['handshake', 'bind-result', 'command-outcome', 'timeout', 'envelope-rejected'].includes(phase)) return
    try {
      const rejected = phase === 'envelope-rejected'
      if (rejected) this.rejectedCount = Math.min(Number.MAX_SAFE_INTEGER, this.rejectedCount + 1)
      const key = `${phase}:${safe.status ?? ''}`
      const now = this.now()
      const previous = this.telemetryTimes.get(key)
      if (previous !== undefined && now - previous < (rejected ? 60_000 : 10_000)) return
      this.telemetryTimes.set(key, now)
      track('leaf_platform_bridge', { phase, ...(rejected ? { count: this.rejectedCount } : safe) })
      if (rejected) this.rejectedCount = 0
    } catch { /* telemetry must not break the bridge */ }
  }

  start() {
    if (this.channel) return this.state
    this.channel = this.injectedChannel === undefined ? globalThis.window?.chrome?.webview ?? null : this.injectedChannel
    this.diagnose('start', { kind: this.channel ? 'webview' : 'none' })
    if (!this.channel) return this.state
    this.state = { ...unavailable(), status: 'connecting' }
    this.channel.addEventListener('message', this.onMessage)
    this.retryHello()
    return this.state
  }

  retryHello() {
    if (!this.channel) return
    this.state = { ...this.state, helloSentAt: Date.now(),
      status: this.state.ready ? this.state.status : 'connecting' }
    this.channel.postMessage({ kind: 'host_bridge_hello', contractVersion: LEAF_PLATFORM_CONTRACT_VERSION })
    this.diagnose('hello-sent')
    clearTimeout(this.handshakeTimer)
    if (!this.state.ready) this.handshakeTimer = setTimeout(() => {
      this.handshakeTimer = null
      this.diagnose('timeout', { kind: 'handshake' })
    }, 10_000)
    this.publish()
  }

  stop() {
    clearTimeout(this.handshakeTimer)
    clearTimeout(this.bindTimer)
    this.finishCommand('superseded', null, false)
    this.channel?.removeEventListener('message', this.onMessage)
    this.channel = null
    this.seenHostMessages.clear()
    this.state = unavailable()
    this.publish()
  }

  subscribe(listener) {
    this.listeners.add(listener)
    listener(this.state)
    return () => this.listeners.delete(listener)
  }

  finishCommand(status, reason, publish = true, callback = null) {
    const pending = this.pendingCommand
    if (!pending) return
    if (pending.action === 'export_dxf' && status === 'applied') {
      pending.callback = callback
      if (!pending.export) return
    }
    clearTimeout(pending.timer)
    this.pendingCommand = null
    const outcome = { action: pending.action, status, reason }
    this.diagnose('command-outcome', { status, reason })
    if (publish) {
      this.state = { ...this.state, lastCommand: outcome }
      this.publish()
    }
    if (pending.sync && (!callback || (pending.action === 'export_dxf' && status !== 'applied'))) pending.reject(new Error(reason || status))
    else pending.resolve(pending.action === 'export_dxf' && status === 'applied'
      ? pending.export : pending.sync ? callback : outcome)
  }

  async focusObject(target, action = 'focus') {
    if (this.state.status !== 'connected' || !this.channel) throw new Error('AutoCAD bridge unavailable')
    const objectHandles = isRecord(target) && Object.keys(target).length === 1 && Object.hasOwn(target, 'objectHandles')
      ? normalizeHandles(target.objectHandles) : null
    const payload = isObjectId(target) ? { objectId: target } : objectHandles ? { objectHandles } : null
    if (!payload) throw new Error('Invalid drawing object identity')
    if (!['select', 'focus'].includes(action)) throw new Error('Invalid drawing action')
    return this.sendCommand('drawing.focus_objects', action, payload)
  }

  applyPlan({ planText, planSha256, parentVersion, resultVersion, drawingStoreId }) {
    if (!Number.isSafeInteger(parentVersion) || parentVersion < 1 || resultVersion !== parentVersion + 1) {
      return Promise.reject(new Error('invalid_plan_version'))
    }
    return this.sendCommand('drawing.apply_plan', 'apply_plan', {
      planText, planSha256, parentVersion, resultVersion,
      resultDrawingVersionId: this.state.ready?.drawingVersionId, drawingStoreId,
    }, 'leaf.mutation_plan.v1')
  }

  exportDxf() { return this.sendCommand('drawing.export_dxf', 'export_dxf', {}) }

  syncCursor({ drawingStoreId, version, exportSha256 }) {
    return this.sendCommand('drawing.sync_cursor', 'sync_cursor', { drawingStoreId, version, exportSha256 }, 'leaf.sync_cursor.v1')
  }

  async sendCommand(verb, action, payload, allowedMutation) {
    if (this.state.status !== 'connected' || !this.channel) throw new Error('AutoCAD bridge unavailable')
    const sync = verb !== 'drawing.focus_objects'
    const ready = this.state.ready
    const channel = this.channel
    const now = new Date()
    const expires = new Date(now.getTime() + 60_000)
    const command = {
      kind: 'command', contractVersion: LEAF_PLATFORM_CONTRACT_VERSION,
      commandId: crypto.randomUUID(), platformTenantId: ready.platformTenantId,
      projectId: ready.projectId, drawingId: ready.drawingId,
      drawingVersionId: ready.drawingVersionId, nonce: crypto.randomUUID(),
      issuedAt: dotNetTimestamp(now), expiresAt: dotNetTimestamp(expires), action,
      dispatchMode: DISPATCH_MODE, payload,
      ...(allowedMutation ? { allowedMutation } : {}),
    }
    const body = {
      protocolVersion: PROTOCOL_VERSION, sessionId: ready.sessionId,
      messageId: crypto.randomUUID(), issuedAt: dotNetTimestamp(now),
      expiresAt: dotNetTimestamp(expires), origin: ready.origin,
      verb, drawingFingerprint: ready.documentFingerprint,
      drawingRevision: ready.drawingVersionId, payload: command,
    }
    this.finishCommand('superseded', null)
    let resolve, reject
    const result = new Promise((done, fail) => { resolve = done; reject = fail })
    const pending = { commandId: command.commandId, action, resolve, reject, sync, timer: null }
    this.pendingCommand = pending
    pending.timer = setTimeout(() => {
      if (this.pendingCommand === pending) {
        this.diagnose('timeout', { kind: 'command' })
        this.finishCommand('unknown', 'timeout')
      }
    }, sync ? action === 'export_dxf' ? 60_000 : 30_000 : 15_000)
    void signature(body, ready.sessionKey).then((signed) => {
      if (this.pendingCommand !== pending) return
      if (this.channel !== channel || this.state.ready !== ready) throw new Error('AutoCAD connection changed')
      channel.postMessage({ ...body, signature: signed })
      this.diagnose('command-sent', { action })
    }).catch((error) => {
      if (this.pendingCommand !== pending) return
      clearTimeout(pending.timer)
      this.pendingCommand = null
      reject(error)
    })
    return result
  }

  async bindDrawing(identity) {
    if (this.state.status !== 'unbound' || !this.channel) throw new Error('Active DWG is not available for connection')
    if (!isRecord(identity) || Object.keys(identity).length !== IDENTITY_KEYS.length ||
        !IDENTITY_KEYS.every((key) => typeof identity[key] === 'string' && UUID.test(identity[key]))) {
      throw new Error('Invalid platform drawing identity')
    }
    const ready = this.state.ready
    const channel = this.channel
    const now = new Date()
    const expires = new Date(now.getTime() + 60_000)
    const body = {
      protocolVersion: PROTOCOL_VERSION, sessionId: ready.sessionId,
      messageId: crypto.randomUUID(), issuedAt: dotNetTimestamp(now),
      expiresAt: dotNetTimestamp(expires), origin: ready.origin,
      verb: 'drawing.bind', drawingFingerprint: ready.documentFingerprint,
      drawingRevision: ready.drawingRevision,
      payload: { ...identity, documentFingerprint: ready.documentFingerprint },
    }
    this.state = { ...this.state, bindingResult: 'Waiting for confirmation in AutoCAD.' }
    this.publish()
    const signed = await signature(body, ready.sessionKey)
    if (this.channel !== channel || this.state.ready !== ready) throw new Error('AutoCAD connection changed')
    channel.postMessage({ ...body, signature: signed })
    this.diagnose('bind-sent')
    clearTimeout(this.bindTimer)
    this.bindTimer = setTimeout(() => {
      this.bindTimer = null
      this.diagnose('timeout', { kind: 'bind' })
    }, 60_000)
  }

  async bindDrawingWithGrant(grant) {
    if (this.state.status !== 'unbound' || !this.channel || this.state.bindingGrantVersion !== 1) throw new Error('Active DWG is not available for connection')
    if (typeof grant !== 'string' || !grant) throw new Error('Invalid binding grant')
    const ready = this.state.ready
    const channel = this.channel
    const now = new Date()
    const body = {
      protocolVersion: PROTOCOL_VERSION, sessionId: ready.sessionId,
      messageId: crypto.randomUUID(), issuedAt: dotNetTimestamp(now),
      expiresAt: dotNetTimestamp(new Date(now.getTime() + 60_000)), origin: ready.origin,
      verb: 'drawing.bind_grant', drawingFingerprint: ready.documentFingerprint,
      drawingRevision: ready.drawingRevision, payload: { grant },
    }
    this.state = { ...this.state, bindingResult: 'Waiting for confirmation in AutoCAD.' }
    this.publish()
    const signed = await signature(body, ready.sessionKey)
    if (this.channel !== channel || this.state.ready !== ready) throw new Error('AutoCAD connection changed')
    channel.postMessage({ ...body, signature: signed })
    this.diagnose('bind-sent')
    clearTimeout(this.bindTimer)
    this.bindTimer = setTimeout(() => {
      this.bindTimer = null
      this.diagnose('timeout', { kind: 'bind' })
    }, 60_000)
  }

  onMessage = (event) => { void this.receive(event.data).catch(() => {}) }

  async receive(value) {
    if (!this.channel) return
    const origin = (this.location ?? globalThis.window?.location)?.origin
    if (isReady(value) || isUnbound(value)) {
      if (value.origin !== origin) {
        this.diagnose('handshake-rejected', { reason: 'origin' })
        return
      }
      clearTimeout(this.handshakeTimer)
      clearTimeout(this.bindTimer)
      this.finishCommand('superseded', null, false)
      this.seenHostMessages.clear()
      this.state = isReady(value)
        ? { ...unavailable(), status: 'connected', ready: value }
        : { ...unavailable(), status: 'unbound', ready: value, bindingResult: null,
          bindingGrantVersion: Object.hasOwn(value, 'bindingGrantVersion') ? value.bindingGrantVersion : null }
      this.diagnose('handshake', { kind: isReady(value) ? 'ready' : 'unbound' })
      this.publish()
      return
    }
    const ready = this.state.ready
    const unbound = this.state.status === 'unbound'
    const malformedHandshake = isRecord(value) && ['host_bridge_ready', 'host_bridge_unbound'].includes(value.kind)
    if (malformedHandshake) this.diagnose('handshake-rejected', { reason: 'shape' })
    const rejectEnvelope = (reason) => { this.diagnose('envelope-rejected', { reason }) }
    if ((!unbound && this.state.status !== 'connected') || !isRecord(value)) {
      if (!malformedHandshake) rejectEnvelope(isRecord(value) ? 'session' : 'shape')
      return
    }
    const envelope = value
    if (envelope.protocolVersion !== PROTOCOL_VERSION) return rejectEnvelope('shape')
    if (envelope.sessionId !== ready.sessionId || envelope.origin !== origin ||
        envelope.drawingRevision !== (unbound ? ready.drawingRevision : ready.drawingVersionId) ||
        envelope.drawingFingerprint !== ready.documentFingerprint) return rejectEnvelope('session')
    if (!(unbound ? ['drawing.bind_result'] : ['drawing.selection_changed', 'host.callback', 'drawing.dxf_export']).includes(envelope.verb)) return rejectEnvelope('verb')
    if (typeof envelope.messageId !== 'string') return rejectEnvelope('shape')
    if (this.seenHostMessages.get(envelope.messageId) > Date.now()) return rejectEnvelope('replay')
    if (typeof envelope.issuedAt !== 'string' || typeof envelope.expiresAt !== 'string' ||
        !signedTimestamp(envelope.issuedAt) || !signedTimestamp(envelope.expiresAt)) return rejectEnvelope('timestamp-format')
    if (typeof envelope.signature !== 'string') return rejectEnvelope('shape')
    const now = Date.now()
    const issuedAt = Date.parse(envelope.issuedAt)
    const expiresAt = Date.parse(envelope.expiresAt)
    if (!Number.isFinite(issuedAt) || !Number.isFinite(expiresAt) ||
        issuedAt > now + 15_000 || expiresAt <= now || expiresAt <= issuedAt ||
        expiresAt - issuedAt > 120_000) return rejectEnvelope('lifetime')
    try {
      if (!await signatureIsValid(envelope, envelope.signature, ready.sessionKey)) return rejectEnvelope('signature')
    } catch (error) {
      rejectEnvelope('signature')
      throw error
    }
    // Verification yields. A new session or concurrent duplicate must not cross it.
    const verifiedAt = Date.now()
    if (this.state.ready !== ready || !this.channel) return rejectEnvelope('session')
    if (expiresAt <= verifiedAt) return rejectEnvelope('lifetime')
    for (const [messageId, expiry] of this.seenHostMessages) {
      if (expiry <= verifiedAt) this.seenHostMessages.delete(messageId)
    }
    if (this.seenHostMessages.has(envelope.messageId)) return rejectEnvelope('replay')
    if (this.seenHostMessages.size >= this.replayCapacity) return rejectEnvelope('capacity')
    this.seenHostMessages.set(envelope.messageId, expiresAt)
    if (unbound) {
      const accepted = isRecord(envelope.payload) && envelope.payload.accepted === true
      const reason = isRecord(envelope.payload) && typeof envelope.payload.reason === 'string'
        ? envelope.payload.reason : accepted ? null : 'invalid_result'
      clearTimeout(this.bindTimer)
      this.diagnose('bind-result', { status: accepted ? 'accepted' : 'rejected', ...(reason === null ? {} : { reason }) })
      this.state = { ...this.state, bindingResult: accepted
        ? 'DWG connected. Starting the signed cross-probe session.'
        : `DWG connection was not changed (${reason.replaceAll('_', ' ')}).` }
      this.publish()
    } else if (envelope.verb === 'host.callback') {
      const callback = envelope.payload
      if (!isRecord(callback) || callback.kind !== 'callback' ||
          callback.contractVersion !== LEAF_PLATFORM_CONTRACT_VERSION ||
          typeof callback.commandId !== 'string' || !UUID.test(callback.commandId) ||
          callback.commandId.toLowerCase() !== this.pendingCommand?.commandId.toLowerCase() ||
          !['applied', 'stale', 'rejected'].includes(callback.status) ||
          !(callback.reason === null || typeof callback.reason === 'string') ||
          !IDENTITY_KEYS.every((key) => typeof callback[key] === 'string' &&
            callback[key].toLowerCase() === ready[key].toLowerCase())) return rejectEnvelope('shape')
      this.finishCommand(callback.status, callback.reason, true, callback)
    } else if (envelope.verb === 'drawing.dxf_export') {
      const data = envelope.payload
      const pending = this.pendingCommand
      if (!isRecord(data) || typeof data.commandId !== 'string' ||
          pending?.action !== 'export_dxf' || data.commandId !== pending.commandId) return
      try {
        if (Object.keys(data).sort().join(',') !== 'byteLength,commandId,dxfBase64,exportSha256' ||
            !Number.isSafeInteger(data.byteLength) || data.byteLength < 0 || data.byteLength > 16777216 ||
            typeof data.dxfBase64 !== 'string' || data.dxfBase64.length > 22369624 ||
            data.dxfBase64.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(data.dxfBase64) ||
            typeof data.exportSha256 !== 'string' || !/^[a-f0-9]{64}$/.test(data.exportSha256)) throw new Error()
        const bytes = base64Bytes(data.dxfBase64)
        if (bytes.length !== data.byteLength) throw new Error()
        const digest = await crypto.subtle.digest('SHA-256', bytes)
        const sha256 = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
        if (sha256 !== data.exportSha256) throw new Error()
        if (this.pendingCommand !== pending || this.state.ready !== ready) return
        pending.export = { bytes, sha256 }
        if (pending.callback) this.finishCommand('applied', pending.callback.reason, true, pending.callback)
      } catch {
        if (this.pendingCommand === pending) this.finishCommand('rejected', 'export_digest_mismatch')
      }
    } else if (envelope.verb === 'drawing.selection_changed' &&
        isRecord(envelope.payload) && envelope.payload.kind === 'selection_event') {
      const selectedObjectId = isObjectId(envelope.payload.payload?.objectId)
        ? envelope.payload.payload.objectId : null
      const selectedHandles = selectedObjectId ? null : normalizeHandles(envelope.payload.payload?.objectHandles)
      this.state = { ...this.state, selectedObjectId, selectedHandles }
      this.publish()
    } else rejectEnvelope('shape')
  }

  publish() { for (const listener of this.listeners) listener(this.state) }
}

export function createLeafHostBridge(options = {}) { return new LeafHostBridge(options) }

let defaultBridge
export function getLeafHostBridge() {
  defaultBridge ??= createLeafHostBridge()
  return defaultBridge
}
