export const LEAF_PLATFORM_CONTRACT_VERSION = 'leaf.platform.v1alpha1'
export const PROTOCOL_VERSION = 'leaf.web-bridge.v1'
export const DISPATCH_MODE = 'autocad_idle_document_lock'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const IDENTITY_KEYS = ['platformTenantId', 'projectId', 'drawingId', 'drawingVersionId']
const unavailable = () => ({ status: 'unavailable', ready: null, selectedObjectId: null, selectedHandles: null })
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
  const bytes = Uint8Array.from(supplied.match(/.{2}/g), (pair) => Number.parseInt(pair, 16))
  const key = await crypto.subtle.importKey('raw', base64Bytes(keyBase64),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['verify'])
  return crypto.subtle.verify('HMAC', key, bytes,
    new TextEncoder().encode(canonical(unsigned(value))))
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
  return isHandshake(value, 'host_bridge_ready', hasEndpoint ? [...IDENTITY_KEYS, 'bridgeEndpoint'] : IDENTITY_KEYS) &&
    (!hasEndpoint || isBridgeEndpoint(value.bridgeEndpoint)) &&
    IDENTITY_KEYS.every((key) => typeof value[key] === 'string' && UUID.test(value[key]))
}

function isUnbound(value) {
  return isHandshake(value, 'host_bridge_unbound', ['drawingRevision']) &&
    value.drawingRevision === '00000000-0000-0000-0000-000000000000'
}

/** Signed WebView2 channel. Session keys stay in memory and are used only for HMAC. */
export class LeafHostBridge {
  constructor({ channel, location } = {}) {
    this.injectedChannel = channel
    this.location = location
    this.channel = null
    this.state = unavailable()
    this.listeners = new Set()
    this.seenHostMessages = new Set()
  }

  start() {
    if (this.channel) return this.state
    this.channel = this.injectedChannel === undefined ? globalThis.window?.chrome?.webview ?? null : this.injectedChannel
    if (!this.channel) return this.state
    this.state = { status: 'connecting', ready: null, selectedObjectId: null, selectedHandles: null }
    this.channel.addEventListener('message', this.onMessage)
    this.channel.postMessage({ kind: 'host_bridge_hello', contractVersion: LEAF_PLATFORM_CONTRACT_VERSION })
    this.publish()
    return this.state
  }

  stop() {
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

  async focusObject(target, action = 'focus') {
    if (this.state.status !== 'connected' || !this.channel) throw new Error('AutoCAD bridge unavailable')
    const objectHandles = isRecord(target) && Object.keys(target).length === 1 && Object.hasOwn(target, 'objectHandles')
      ? normalizeHandles(target.objectHandles) : null
    const payload = isObjectId(target) ? { objectId: target } : objectHandles ? { objectHandles } : null
    if (!payload) throw new Error('Invalid drawing object identity')
    if (!['select', 'focus'].includes(action)) throw new Error('Invalid drawing action')
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
    }
    const body = {
      protocolVersion: PROTOCOL_VERSION, sessionId: ready.sessionId,
      messageId: crypto.randomUUID(), issuedAt: dotNetTimestamp(now),
      expiresAt: dotNetTimestamp(expires), origin: ready.origin,
      verb: 'drawing.focus_objects', drawingFingerprint: ready.documentFingerprint,
      drawingRevision: ready.drawingVersionId, payload: command,
    }
    const signed = await signature(body, ready.sessionKey)
    if (this.channel !== channel || this.state.ready !== ready) throw new Error('AutoCAD connection changed')
    channel.postMessage({ ...body, signature: signed })
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
  }

  onMessage = (event) => { void this.receive(event.data).catch(() => {}) }

  async receive(value) {
    if (!this.channel) return
    const origin = (this.location ?? globalThis.window?.location)?.origin
    if (isReady(value) || isUnbound(value)) {
      if (value.origin !== origin) return
      this.seenHostMessages.clear()
      this.state = isReady(value)
        ? { status: 'connected', ready: value, selectedObjectId: null, selectedHandles: null }
        : { status: 'unbound', ready: value, selectedObjectId: null, selectedHandles: null, bindingResult: null }
      this.publish()
      return
    }
    const ready = this.state.ready
    const unbound = this.state.status === 'unbound'
    if ((!unbound && this.state.status !== 'connected') || !isRecord(value)) return
    const envelope = value
    if (envelope.protocolVersion !== PROTOCOL_VERSION ||
        envelope.sessionId !== ready.sessionId || envelope.origin !== origin ||
        envelope.drawingRevision !== (unbound ? ready.drawingRevision : ready.drawingVersionId) ||
        envelope.drawingFingerprint !== ready.documentFingerprint ||
        !(unbound ? ['drawing.bind_result'] : ['drawing.selection_changed', 'host.callback']).includes(envelope.verb) ||
        typeof envelope.messageId !== 'string' || this.seenHostMessages.has(envelope.messageId) ||
        typeof envelope.issuedAt !== 'string' || typeof envelope.expiresAt !== 'string' ||
        typeof envelope.signature !== 'string') return
    const now = Date.now()
    const issuedAt = Date.parse(envelope.issuedAt)
    const expiresAt = Date.parse(envelope.expiresAt)
    if (!Number.isFinite(issuedAt) || !Number.isFinite(expiresAt) ||
        issuedAt > now + 15_000 || expiresAt <= now || expiresAt <= issuedAt ||
        expiresAt - issuedAt > 120_000) return
    if (!await signatureIsValid(envelope, envelope.signature, ready.sessionKey)) return
    // Verification yields. A new session or concurrent duplicate must not cross it.
    if (this.state.ready !== ready || !this.channel || this.seenHostMessages.has(envelope.messageId)) return
    this.seenHostMessages.add(envelope.messageId)
    if (this.seenHostMessages.size > 1024) this.seenHostMessages.delete(this.seenHostMessages.values().next().value)
    if (unbound) {
      const accepted = isRecord(envelope.payload) && envelope.payload.accepted === true
      const reason = isRecord(envelope.payload) && typeof envelope.payload.reason === 'string'
        ? envelope.payload.reason : 'invalid_result'
      this.state = { ...this.state, bindingResult: accepted
        ? 'DWG connected. Starting the signed cross-probe session.'
        : `DWG connection was not changed (${reason.replaceAll('_', ' ')}).` }
      this.publish()
    } else if (envelope.verb === 'drawing.selection_changed' &&
        isRecord(envelope.payload) && envelope.payload.kind === 'selection_event') {
      const selectedObjectId = isObjectId(envelope.payload.payload?.objectId)
        ? envelope.payload.payload.objectId : null
      const selectedHandles = selectedObjectId ? null : normalizeHandles(envelope.payload.payload?.objectHandles)
      this.state = { ...this.state, selectedObjectId, selectedHandles }
      this.publish()
    }
  }

  publish() { for (const listener of this.listeners) listener(this.state) }
}

export function createLeafHostBridge(options = {}) { return new LeafHostBridge(options) }

let defaultBridge
export function getLeafHostBridge() {
  defaultBridge ??= createLeafHostBridge()
  return defaultBridge
}
