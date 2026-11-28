import { track } from '../telemetry.js'
import { getDiagnostics, sanitizeDetail } from './diagnostics.js'
import { isToolResponse, validateToolPayload } from './branchTools.js'

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
    ...(Object.hasOwn(value, 'syncCursor') ? ['syncCursor'] : []),
    ...(Object.hasOwn(value, 'capabilities') ? ['capabilities'] : [])]
  return isHandshake(value, 'host_bridge_ready', extras) &&
    (!Object.hasOwn(value, 'capabilities') || isCapabilities(value.capabilities)) &&
    (!Object.hasOwn(value, 'syncCursor') || (isRecord(value.syncCursor) &&
      Object.keys(value.syncCursor).length === 2 &&
      typeof value.syncCursor.drawingStoreId === 'string' && /^[a-z0-9][a-z0-9_-]{0,62}$/.test(value.syncCursor.drawingStoreId) &&
      Number.isSafeInteger(value.syncCursor.version) && value.syncCursor.version > 0)) &&
    (!hasEndpoint || isBridgeEndpoint(value.bridgeEndpoint)) &&
    IDENTITY_KEYS.every((key) => typeof value[key] === 'string' && UUID.test(value[key]))
}

function isCapabilities(value) {
  return isRecord(value) && [Object.prototype, null].includes(Object.getPrototypeOf(value)) &&
    Reflect.ownKeys(value).length === 3 && ['planGrammarMax', 'runTool', 'hostApi'].every((key) => Object.hasOwn(value, key)) &&
    Number.isSafeInteger(value.planGrammarMax) && value.planGrammarMax >= 1 && value.planGrammarMax <= 3 &&
    typeof value.runTool === 'boolean' && Number.isSafeInteger(value.hostApi) && value.hostApi >= 1
}

function isUnbound(value) {
  if (!isRecord(value)) return false
  const hasGrantVersion = Object.hasOwn(value, 'bindingGrantVersion')
  return isHandshake(value, 'host_bridge_unbound', hasGrantVersion ? ['drawingRevision', 'bindingGrantVersion'] : ['drawingRevision']) &&
    (!hasGrantVersion || (Number.isInteger(value.bindingGrantVersion) && value.bindingGrantVersion === 1)) &&
    value.drawingRevision === '00000000-0000-0000-0000-000000000000'
}

export const SEND_COPY_MAX_BYTES = 16777216
const SEND_COPY_MAX_BASE64 = 22369624
const SEND_COPY_WAIT_MS = 200_000
const FINGERPRINT_SCHEMA = 'leaf.drawing-fingerprint.v1'
const SEND_COPY_KEYS = ['byteLength', 'dxfBase64', 'fileName', 'fingerprint', 'reason', 'requestId', 'sha256', 'status']
const FINGERPRINT_KEYS = ['bytes', 'layer_count', 'layers', 'layers_digest', 'layers_omitted', 'model_objects', 'reason', 'schema', 'sha256', 'status']
const SEND_COPY_FAILURES = ['active_document_changed', 'session_changed', 'prompt_pending', 'replay',
  'no_model_objects', 'too_large', 'binary_or_unreadable', 'export_failed']
const HEX64 = /^[a-f0-9]{64}$/
const exactKeys = (value, keys) => isRecord(value) && Object.keys(value).sort().join(',') === keys.join(',')
// Mirrors the plugin's SendCopyText: no control, format, line or paragraph separator, or lone surrogate.
const UNSAFE_TEXT = /[\p{Cc}\p{Cf}\u2028\u2029]|[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u
const codePoints = (value) => [...value].length

/** leaf.drawing-fingerprint.v1 shape check, the page twin of the plugin's DrawingFingerprintV1.IsValid. */
export function isDrawingFingerprint(value, sha256 = null, bytes = null) {
  if (!exactKeys(value, FINGERPRINT_KEYS) || value.schema !== FINGERPRINT_SCHEMA || value.status !== 'ok' ||
      value.reason !== null || typeof value.sha256 !== 'string' || !HEX64.test(value.sha256) ||
      typeof value.layers_digest !== 'string' || !HEX64.test(value.layers_digest) || !Array.isArray(value.layers)) return false
  if (sha256 !== null && value.sha256 !== sha256) return false
  if (bytes !== null && value.bytes !== bytes) return false
  const { model_objects: total, layer_count: layers, layers_omitted: omitted } = value
  if (![value.bytes, total, layers, omitted].every(Number.isSafeInteger) || value.bytes < 1 || value.bytes > SEND_COPY_MAX_BYTES ||
      total < 0 || total > 2147483647 || layers < 0 || layers > total ||
      value.layers.length !== Math.min(64, layers) || omitted !== Math.max(0, layers - 64)) return false
  let sum = 0
  let previous = Infinity
  for (const row of value.layers) {
    if (!exactKeys(row, ['count', 'name']) || typeof row.name !== 'string' || !Number.isSafeInteger(row.count) ||
        codePoints(row.name) > 255 || UNSAFE_TEXT.test(row.name) || row.count < 1 || row.count > total || row.count > previous) return false
    previous = row.count
    sum += row.count
  }
  return layers <= 64 ? sum === total : sum + layers - 64 <= total
}

function isSendCopyFileName(value) {
  return typeof value === 'string' && value.length >= 1 && value.length <= 255 && !/[\\/]/.test(value) &&
    !UNSAFE_TEXT.test(value) && value.trim() !== '' && value !== '.' && value !== '..'
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
    this.pendingSend = null
  }

  diagnose(phase, detail = {}) {
    const safe = sanitizeDetail(phase, detail)
    try { this.diagnostics.record(phase, safe) } catch { /* diagnostics must not break the bridge */ }
    if (!['handshake', 'bind-result', 'command-outcome', 'timeout', 'envelope-rejected', 'send-copy-result'].includes(phase)) return
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
    this.finishSend({ status: 'failed', reason: 'session_changed' })
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

  applyPlan({ planText, planSha256, parentVersion, resultVersion, drawingStoreId, resultContentSha256 }) {
    if (!Number.isSafeInteger(parentVersion) || parentVersion < 1 || resultVersion !== parentVersion + 1) {
      return Promise.reject(new Error('invalid_plan_version'))
    }
    if (typeof resultContentSha256 !== 'string' || resultContentSha256.length !== 64 || !/^[a-f0-9]{64}$/.test(resultContentSha256)) {
      return Promise.reject(new Error('invalid_result_digest'))
    }
    return this.sendCommand('drawing.apply_plan', 'apply_plan', {
      planText, planSha256, parentVersion, resultVersion,
      resultDrawingVersionId: this.state.ready?.drawingVersionId, drawingStoreId, resultContentSha256,
    }, 'leaf.mutation_plan.v1')
  }

  exportDxf() { return this.sendCommand('drawing.export_dxf', 'export_dxf', {}) }

  listTools() { return this.sendCommand('list_tools', 'list_tools', {}) }

  async runTool(payload) {
    validateToolPayload(payload)
    return this.sendCommand('run_tool', 'run_tool', JSON.parse(JSON.stringify(payload)), 'leaf.tool_run.v1')
  }

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
    if (this.pendingSend) throw new Error('A drawing copy is being sent')
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
    if (this.pendingSend) throw new Error('A drawing copy is being sent')
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

  /**
   * Ask AutoCAD for a consented copy of the unconnected DWG (M3 wire v6, verb drawing.send_copy).
   * The page sends only a request id. After the post the promise resolves exactly once:
   * {status: 'sent', fileName, bytes, sha256, fingerprint} or {status: 'declined'|'failed', reason}.
   * Page-side reasons: plugin_outdated, host_refused, timeout, session_changed, invalid_result.
   */
  async sendCopy() {
    if (this.state.status !== 'unbound' || !this.channel) throw new Error('Active DWG is not available for sending')
    if (this.pendingSend) throw new Error('A drawing copy is already being sent')
    if (this.bindTimer) throw new Error('A drawing connection is waiting for AutoCAD')
    const ready = this.state.ready
    const channel = this.channel
    const requestId = crypto.randomUUID()
    const now = new Date()
    const body = {
      protocolVersion: PROTOCOL_VERSION, sessionId: ready.sessionId,
      messageId: crypto.randomUUID(), issuedAt: dotNetTimestamp(now),
      expiresAt: dotNetTimestamp(new Date(now.getTime() + 60_000)), origin: ready.origin,
      verb: 'drawing.send_copy', drawingFingerprint: ready.documentFingerprint,
      drawingRevision: ready.drawingRevision, payload: { requestId },
    }
    let resolve
    const result = new Promise((done) => { resolve = done })
    const pending = { requestId, ready, resolve, timer: null }
    this.pendingSend = pending
    try {
      const signed = await signature(body, ready.sessionKey)
      if (this.pendingSend !== pending) return result
      if (this.channel !== channel || this.state.ready !== ready) throw new Error('AutoCAD connection changed')
      channel.postMessage({ ...body, signature: signed })
    } catch (error) {
      if (this.pendingSend === pending) this.pendingSend = null
      throw error
    }
    this.diagnose('send-copy-sent')
    // The consent box closes itself at 120 s; a result after 200 s is ignored.
    pending.timer = setTimeout(() => {
      if (this.pendingSend !== pending) return
      this.diagnose('timeout', { kind: 'send-copy' })
      this.finishSend({ status: 'failed', reason: 'timeout' })
    }, SEND_COPY_WAIT_MS)
    return result
  }

  finishSend(outcome) {
    const pending = this.pendingSend
    if (!pending) return
    clearTimeout(pending.timer)
    this.pendingSend = null
    this.diagnose('send-copy-result', { status: outcome.status, ...(outcome.reason ? { reason: outcome.reason } : {}) })
    pending.resolve(outcome)
  }

  async receiveSendCopy(data, ready) {
    const pending = this.pendingSend
    if (!pending || pending.ready !== ready || !isRecord(data) || data.requestId !== pending.requestId) return false
    const malformed = () => { this.finishSend({ status: 'failed', reason: 'invalid_result' }); return true }
    if (!exactKeys(data, SEND_COPY_KEYS)) return malformed()
    if (data.status !== 'sent') {
      const known = (data.status === 'declined' && data.reason === 'user_declined') ||
        (data.status === 'failed' && SEND_COPY_FAILURES.includes(data.reason))
      if (!known || !['fileName', 'byteLength', 'sha256', 'dxfBase64', 'fingerprint'].every((key) => data[key] === null)) return malformed()
      this.finishSend({ status: data.status, reason: data.reason })
      return true
    }
    if (data.reason !== null || !isSendCopyFileName(data.fileName) || !Number.isSafeInteger(data.byteLength) ||
        data.byteLength < 1 || data.byteLength > SEND_COPY_MAX_BYTES || typeof data.sha256 !== 'string' || !HEX64.test(data.sha256) ||
        typeof data.dxfBase64 !== 'string' || data.dxfBase64.length > SEND_COPY_MAX_BASE64 || data.dxfBase64.length % 4 !== 0 ||
        !/^[A-Za-z0-9+/]*={0,2}$/.test(data.dxfBase64) || !isDrawingFingerprint(data.fingerprint, data.sha256, data.byteLength)) return malformed()
    let bytes
    try { bytes = base64Bytes(data.dxfBase64) } catch { return malformed() }
    if (bytes.length !== data.byteLength) return malformed()
    const digest = await crypto.subtle.digest('SHA-256', bytes)
    const sha256 = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
    if (this.pendingSend !== pending) return true
    if (sha256 !== data.sha256) return malformed()
    this.finishSend({ status: 'sent', reason: null, fileName: data.fileName, bytes, sha256,
      fingerprint: JSON.parse(JSON.stringify(data.fingerprint)) })
    return true
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
      this.bindTimer = null
      this.finishCommand('superseded', null, false)
      this.finishSend({ status: 'failed', reason: 'session_changed' })
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
    if (!(unbound ? ['drawing.bind_result', 'drawing.send_copy_result'] : ['drawing.selection_changed', 'host.callback', 'drawing.dxf_export']).includes(envelope.verb)) return rejectEnvelope('verb')
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
    if (unbound && envelope.verb === 'drawing.send_copy_result') {
      if (!await this.receiveSendCopy(envelope.payload, ready)) rejectEnvelope('shape')
    } else if (unbound) {
      const accepted = isRecord(envelope.payload) && envelope.payload.accepted === true
      const reason = isRecord(envelope.payload) && typeof envelope.payload.reason === 'string'
        ? envelope.payload.reason : accepted ? null : 'invalid_result'
      if (this.pendingSend && !this.bindTimer) {
        // A refusal while a send waits answers the send (an old plugin refuses the verb). An
        // accepted bind result never completes a send, and no bind is in flight with one.
        if (accepted) return rejectEnvelope('shape')
        this.finishSend({ status: 'failed', reason: ['verb_not_allowed', 'unbound_verb_not_allowed'].includes(reason)
          ? 'plugin_outdated' : 'host_refused' })
        return
      }
      clearTimeout(this.bindTimer)
      this.bindTimer = null
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
      if (['list_tools', 'run_tool'].includes(this.pendingCommand.action)) {
        if (!isToolResponse(this.pendingCommand.action, callback.result) ||
            (this.pendingCommand.action === 'run_tool' &&
              (callback.status === 'applied') !== (callback.result.decision === 'accept'))) {
          this.finishCommand('rejected', 'invalid_response')
        } else this.finishCommand(callback.status, callback.reason, true, callback.result)
      } else this.finishCommand(callback.status, callback.reason, true, callback)
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
