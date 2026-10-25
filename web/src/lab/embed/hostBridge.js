import { canonicalJSON } from '../contract.js'
import { hostOrigins } from './hostOrigins.js'
import { validateWidgetDoc } from '../widgets/widgetDoc.js'
import { RENDERER_VERSION } from '../widgets/widgetCatalog.js'

const HASH = /^[0-9a-f]{64}$/
const GIT_SHA = /^(?:[0-9a-f]{7,40}|[0-9a-f]{64})$/
const emptyDocument = {}
const copy = (value) => JSON.parse(canonicalJSON(value))
const hex = (bytes) => Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
let healthSource
const buildSources = new Map()

export function mintChannel(cryptoImpl = globalThis.crypto) {
  return hex(cryptoImpl.getRandomValues(new Uint8Array(16)))
}

async function sha256(text, cryptoImpl) {
  return hex(new Uint8Array(await cryptoImpl.subtle.digest('SHA-256', new TextEncoder().encode(text))))
}

// Decoding identifies the already authenticated session; it never authenticates it.
// The rail's server response remains the authority to start this bridge.
export async function sessionPrincipalProof({ storage = globalThis.localStorage, cryptoImpl = globalThis.crypto } = {}) {
  const token = storage?.getItem('leaf.jwt')
  const part = token?.split('.')[1]
  if (!part) throw new Error('Lab session subject is unavailable')
  const base64 = part.replace(/-/g, '+').replace(/_/g, '/')
  const bytes = Uint8Array.from(atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, '=')), (char) => char.charCodeAt(0))
  const subject = JSON.parse(new TextDecoder().decode(bytes)).sub
  if (typeof subject !== 'string' || !subject) throw new Error('Lab session subject is unavailable')
  return sha256(subject, cryptoImpl)
}

export function sourceShaFromGit(gitSha, cryptoImpl = globalThis.crypto) {
  if (typeof gitSha !== 'string' || !GIT_SHA.test(gitSha)) throw new Error('Studio source identity is unavailable')
  return sha256(gitSha, cryptoImpl)
}

export async function studioSourceSha({
  fetchImpl = globalThis.fetch, cryptoImpl = globalThis.crypto,
  gitSha = typeof __BUILD_HASH__ === 'undefined' ? null : __BUILD_HASH__,
} = {}) {
  // Vite exposes an abbreviated Git SHA; health exposes the full revision.
  // Hash the build marker as a string, once, to fit the protocol's sha256 field.
  if (typeof gitSha === 'string' && GIT_SHA.test(gitSha)) {
    if (!buildSources.has(gitSha)) buildSources.set(gitSha, sourceShaFromGit(gitSha, cryptoImpl))
    return buildSources.get(gitSha)
  }
  healthSource ??= Promise.resolve().then(async () => {
    const response = await fetchImpl('/api/health')
    if (!response.ok) throw new Error('Studio source identity is unavailable')
    const { source_sha: source } = await response.json()
    return sourceShaFromGit(source, cryptoImpl)
  })
  return healthSource
}

export function createLabHostBridge({
  win = globalThis.window, origins = hostOrigins(), cryptoImpl = globalThis.crypto,
  principalProof, sourceSha, now = Date.now, clock = () => performance.now(),
  onRestore = () => {}, onSource = () => {}, onReload = () => {}, onAdmitted = () => {},
  onChange = () => {},
} = {}) {
  const allowed = hostOrigins(origins.join(','))
  const channel = mintChannel(cryptoImpl)
  let stopped = false
  let started = false
  let admitted = false
  let origin = null
  let proof
  let source
  let emptyHash
  let revision = 0
  let proposalHash = null
  let proposalState = null
  let sourceRows = null
  let outbound = -1
  let inbound = -1
  let rejected = 0
  let acceptedTimes = []
  let queue = Promise.resolve()
  const enqueue = (action) => {
    const next = queue.then(() => stopped ? false : action())
    queue = next.catch(() => false)
    return next
  }
  const hashDocument = (document) => sha256(canonicalJSON(document), cryptoImpl)
  const reject = () => { rejected += 1; return false }
  function envelope(kind, payload, triggeredAt, proposal = false) {
    const sent = now()
    return {
      schema: 'leaf.lab-embed.v1', version: 1, kind, channel,
      principal_proof: proof, source_sha: source,
      proposal_revision: proposal ? revision : 0,
      proposal_hash: proposal ? proposalHash : emptyHash,
      sequence: ++outbound, sent_at_ms: sent, expires_at_ms: sent + 30000,
      latency_us: triggeredAt === null ? 0 : Math.max(0, Math.round((clock() - triggeredAt) * 1000)),
      payload,
    }
  }
  function post(kind, payload, triggeredAt, proposal = false) {
    if (stopped || !admitted) return false
    const message = envelope(kind, payload, triggeredAt, proposal)
    if (kind === 'widget.publish-request' && new TextEncoder().encode(canonicalJSON(message)).length > 40960) return false
    if (kind === 'proposal') {
      const bytes = new TextEncoder().encode(canonicalJSON(message)).length
      if (bytes > 4096) {
        proposalState = { status: 'too_large', bytes }
        onChange({ ...copy(payload), ready: false,
          problems: [...payload.problems, `proposal exceeds the 4096-byte wire limit (${bytes} bytes)`] })
        return false
      }
      const wasTooLarge = proposalState !== null
      proposalState = null
      if (wasTooLarge) onChange(copy(payload))
    }
    win.parent.postMessage(message, origin)
    return true
  }
  async function receive(event) {
    if (!started || stopped || !source || event.source !== win.parent || !allowed.includes(event.origin)
      || (origin && event.origin !== origin)) return reject()
    const message = event.data
    // The host enforces payload schemas. Check the channel envelope and the
    // fields consumed here without loading a schema bundle into the client.
    if (!message || message.schema !== 'leaf.lab-embed.v1' || message.version !== 1
      || !message.payload || typeof message.payload !== 'object' || Array.isArray(message.payload)
      || !['shell.ready', 'proposal', 'source.value'].includes(message.kind)
      || !HASH.test(message.source_sha) || !HASH.test(message.proposal_hash)
      || ![message.sequence, message.proposal_revision, message.sent_at_ms, message.expires_at_ms, message.latency_us]
        .every((value) => Number.isSafeInteger(value) && value >= 0)) return reject()
    if (message.kind === 'source.value' && (!Array.isArray(message.payload.rows)
      || message.payload.rows.length > 200
      || message.payload.rows.some((row) => !row || typeof row !== 'object' || Array.isArray(row)))) return reject()
    if (message.channel !== channel || message.principal_proof !== proof) return reject()
    if (!admitted && !(message.kind === 'shell.ready' && message.payload.accepted === true)) return reject()
    const time = now()
    if (message.sent_at_ms > time || message.expires_at_ms <= time
      || message.expires_at_ms <= message.sent_at_ms || message.expires_at_ms - message.sent_at_ms > 30000
      || message.sequence <= inbound) return reject()
    const recent = acceptedTimes.filter((stamp) => stamp > time - 1000)
    try {
      if (recent.length >= 20 || new TextEncoder().encode(canonicalJSON(message)).length > 262144) return reject()
    } catch { return reject() }
    if (message.source_sha !== source) {
      stopped = true
      win.removeEventListener('message', listen)
      onReload()
      return false
    }
    const triggeredAt = clock()
    if (message.kind === 'proposal') {
      if (message.payload.restore !== true || await hashDocument(message.payload.document) !== message.proposal_hash) return reject()
      if (stopped) return false
      await onRestore(copy(message.payload))
      if (stopped) return false
      proposalHash = message.proposal_hash
    } else if (message.kind !== 'shell.ready' && message.kind !== 'source.value') return reject()
    inbound = message.sequence
    acceptedTimes = [...recent, time]
    if (!admitted) {
      origin = event.origin
      admitted = true
      onAdmitted()
    } else if (message.kind === 'proposal') {
      post('shell.ready', { restored: true }, triggeredAt)
    } else if (message.kind === 'source.value') {
      sourceRows = copy(message.payload.rows)
      onSource(copy(sourceRows))
    }
    return true
  }
  const listen = (event) => { void enqueue(() => receive(event)).catch(() => {}) }
  return {
    channel,
    get admitted() { return admitted && !stopped },
    get rejectedCount() { return rejected },
    get lockedOrigin() { return origin },
    get sourceRows() { return sourceRows === null ? null : copy(sourceRows) },
    get state() { return proposalState === null ? null : { ...proposalState } },
    start() {
      if (started || stopped || !win || win.self === win.top || !allowed.length) return Promise.resolve(false)
      started = true
      win.addEventListener('message', listen)
      return enqueue(async () => {
        const [nextProof, nextSource, nextEmptyHash] = await Promise.all([
          principalProof ?? sessionPrincipalProof({ cryptoImpl }),
          sourceSha ?? studioSourceSha({ cryptoImpl }), hashDocument(emptyDocument),
        ])
        proof = nextProof
        source = nextSource
        emptyHash = nextEmptyHash
        if (stopped) return false
        if (!HASH.test(proof) || !HASH.test(source)) { this.stop(); return false }
        const hello = envelope('shell.ready', {}, null)
        for (const target of allowed) win.parent.postMessage(hello, target)
        return true
      }).catch(() => { this.stop(); return false })
    },
    stop() {
      stopped = true
      admitted = false
      win?.removeEventListener('message', listen)
    },
    proposal(draft, triggeredAt = clock()) {
      if (!admitted || stopped) return Promise.resolve(false)
      const payload = copy({ document: draft.document, ready: draft.ready, problems: draft.problems })
      return enqueue(async () => {
        const hash = await hashDocument(payload.document)
        if (stopped) return false
        proposalHash = hash
        revision += 1
        return post('proposal', payload, triggeredAt, true)
      })
    },
    publishWidget({ doc, source } = {}) {
      if (!admitted || stopped) return Promise.resolve({ ok: false, reason: 'not_admitted' })
      let validated
      try {
        validated = validateWidgetDoc(doc)
        canonicalJSON(validated)
      }
      catch { return Promise.resolve({ ok: false, reason: 'invalid_doc' }) }
      if (!source || typeof source !== 'object' || Array.isArray(source)
        || Reflect.ownKeys(source).length !== 3
        || !['artifact_id', 'revision', 'digest'].every((key) => Object.hasOwn(source, key))
        || typeof source.artifact_id !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}(?![\s\S])/.test(source.artifact_id)
        || !Number.isSafeInteger(source.revision) || source.revision < 1
        || typeof source.digest !== 'string' || !/^[0-9a-f]{64}(?![\s\S])/.test(source.digest)) {
        return Promise.resolve({ ok: false, reason: 'invalid_source' })
      }
      const payload = { doc: validated, source: { artifact_id: source.artifact_id, revision: source.revision, digest: source.digest }, renderer_version: RENDERER_VERSION }
      return enqueue(() => {
        if (!admitted || stopped) return { ok: false, reason: 'not_admitted' }
        return post('widget.publish-request', payload, null)
          ? { ok: true } : { ok: false, reason: 'too_large' }
      }).then((result) => result || { ok: false, reason: 'not_admitted' })
    },
    run(triggeredAt = clock()) {
      if (!admitted || stopped) return Promise.resolve(false)
      return enqueue(() => proposalHash && proposalState === null ? post('signed-request', { request_hash: proposalHash, handoff: 'server-sign' }, triggeredAt) : false)
    },
    receive(event) { return enqueue(() => receive(event)) },
  }
}
