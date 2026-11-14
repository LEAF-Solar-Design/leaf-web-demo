import { authHeaders, config } from '../../api.js'
import { fetchWithBudget } from '../../fetchBudget.js'
import { humanizeError } from '../../errorHumanize.js'
import { canonicalJSON } from '../contract.js'
import { sha256Hex } from '../runtime/sha256.js'

export const SAVED_REVISION_EVENT = 'leaf.lab.saved-revision'
export const GRAPH_DOC_KEY = 'leaf.lab.graph-doc.v1'
// Object associations carry display state through GraphView without graph fields.
export const graphLayouts = new WeakMap()
export const MSG_LAB_TOO_LARGE = 'This document is too large to save.'
export const MSG_LAB_STORAGE_FULL = 'This save is over the Lab storage limit.'
export function isValidDocId(id) {
  return typeof id === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}(?![\s\S])/.test(id)
}
function identifier(id) {
  if (!isValidDocId(id)) throw new Error('Enter a valid Document ID.')
}
const initial = () => ({ head: null, published: null, revisions: [], lastSaved: null, conflict: null,
  events: [], saveTimes: [], requestTimes: [], restoredGraph: null, restoredKey: 0, headLoaded: false, headLookupFailed: false, openedBundle: null })

const bundleParts = ['evaluations', 'graph', 'identity', 'layout', 'policy_reference']
const bundleHash = value => sha256Hex(canonicalJSON(value))
const sameKeys = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join(',') === [...keys].sort().join(',')
const bundleError = 'Bundle integrity check failed. No graph was opened.'
export function verifyLabBundle(bundle) {
  if (!sameKeys(bundle, ['schema', 'parts', 'manifest']) || bundle.schema !== 'leaf.lab-bundle.v1'
    || !sameKeys(bundle.parts, bundleParts) || !sameKeys(bundle.manifest, ['algorithm', 'parts'])
    || bundle.manifest.algorithm !== 'sha256' || !sameKeys(bundle.manifest.parts, bundleParts)) throw new Error(bundleError)
  for (const key of bundleParts) {
    if (bundle.manifest.parts[key] !== bundleHash(bundle.parts[key])) throw new Error(bundleError)
  }
  const { identity, graph, policy_reference: policy, evaluations } = bundle.parts
  if (!identity || !isValidDocId(identity.doc_id) || typeof identity.tenant_id !== 'string'
    || !Number.isSafeInteger(identity.revision) || identity.revision < 1 || identity.kind !== 'graph'
    || graph?.schema !== 'leaf.lab-graph.v1' || !Array.isArray(graph.blocks)
    || identity.sha256 !== bundleHash(graph) || policy?.graph_sha256 !== identity.sha256
    || !Array.isArray(policy.blocks) || !Array.isArray(policy.compiled) || !Array.isArray(evaluations)) throw new Error(bundleError)
  const blocks = graph.blocks.map(block => ({ id: block.id, registry: block.registry, settings_sha256: bundleHash(block.settings) }))
  if (canonicalJSON(blocks) !== canonicalJSON(policy.blocks)) throw new Error(bundleError)
  for (const evaluation of evaluations) {
    const run = evaluation?.run
    if (!run || run.tenant_id !== identity.tenant_id || run.doc_id !== identity.doc_id
      || run.graph_revision !== identity.revision || run.graph_sha256 !== identity.sha256
      || !Array.isArray(evaluation.events) || !evaluation.complete_results) throw new Error(bundleError)
    if (evaluation.events.some(event => event.tenant_id !== identity.tenant_id || event.run_id !== run.run_id)) throw new Error(bundleError)
  }
  return bundle
}

export function createLabArtifactsClient({ fetchImpl = (...args) => globalThis.fetch(...args), now = Date.now, storage = null } = {}) {
  let remembered = null
  if (storage) try {
    const value = storage.getItem(GRAPH_DOC_KEY)
    if (typeof value === 'string' && value.length <= 512) {
      const parsed = JSON.parse(value)
      if (parsed && typeof parsed === 'object' && isValidDocId(parsed.doc_id)) remembered = parsed.doc_id
    }
  } catch {}
  let docId = remembered || 'strategy'
  let reopened = false
  function remember(id) {
    if (storage) try { storage.setItem(GRAPH_DOC_KEY, JSON.stringify({ doc_id: id })) } catch {}
  }
  let generation = 0, writeSeq = 0, readSeq = 0, operation = 0
  let applied = { head: 0, revisions: 0 }
  const state = initial()
  const listeners = new Set()
  const update = patch => { Object.assign(state, patch); listeners.forEach(listener => listener()) }
  const capture = id => ({ id, generation, writeSeq, readSeq })
  const current = token => token.id === docId && token.generation === generation
  const fresh = token => current(token) && token.writeSeq === writeSeq
  const authority = () => canonicalJSON([config.apiBase, config.tenant, authHeaders()])
  const maximum = (left, right) => right == null ? left : left == null ? right : Math.max(left, right)
  const merge = rows => [...new Map([...state.revisions, ...rows].map(row => [row.revision, row])).values()].sort((a, b) => a.revision - b.revision)
  async function request(id, path, body, resolveConflict = true, token = capture(id), method = body === undefined ? 'GET' : 'POST') {
    identifier(id)
    const started = now()
    try {
      const response = await fetchWithBudget(fetchImpl, `${config.apiBase}/api/lab/docs/${encodeURIComponent(id)}/${path}`, {
        method, credentials: 'same-origin', redirect: 'error',
        headers: { accept: 'application/json', 'content-type': 'application/json', 'X-Tenant-Id': config.tenant, ...authHeaders() },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      })
      const data = await response.json().catch(() => ({}))
      if (response.ok) {
        if (data.graph && data.sha256) graphLayouts.set(data.graph, { load: () => getLayout(id, data.sha256) })
        return { ...data, ok: true }
      }
      const code = typeof data?.detail === 'string' ? data.detail : data?.detail?.code
      const detail = path.endsWith('/export') && [401, 403, 413].includes(response.status)
        ? response.status === 401 ? 'Sign in again to access this revision.'
          : response.status === 403 ? 'You no longer have access to this revision.' : 'This revision is too large to export.'
        : response.status === 413
        ? (code === 'lab_capacity_exceeded' ? MSG_LAB_STORAGE_FULL : MSG_LAB_TOO_LARGE)
        : humanizeError(data?.error?.message || (typeof data?.detail === 'string' ? data.detail : data?.detail?.message))
      if (response.status === 409 && resolveConflict) {
        if (fresh(token)) { readSeq += 1; applied = { head: readSeq, revisions: readSeq } }
        const head = await request(id, 'head', undefined, false, token)
        return { ok: false, conflict: true, head: head.ok ? head.revision : data.head ?? (current(token) ? state.conflict?.serverHead ?? state.head : null),
          ...(!head.ok ? { headLookupFailed: true } : {}), status: 409, detail }
      }
      return { ok: false, status: response.status, detail }
    } catch (error) { return { ok: false, status: 0, detail: humanizeError(error) } }
    finally {
      if (current(token)) update({ requestTimes: [...state.requestTimes, Math.max(0, now() - started)].slice(-20) })
    }
  }
  async function getLayout(id, sha256) {
    try {
      const result = await request(id, `layout/${encodeURIComponent(sha256)}`, undefined, false)
      return result.ok ? result.layout ?? null : null
    } catch { return null }
  }
  async function putLayout(id, sha256, layout) {
    try {
      return (await request(id, `layout/${encodeURIComponent(sha256)}`, layout, false, capture(id), 'PUT')).ok
    } catch { return false }
  }
  function saved(id, row, kind, token, op) {
    const revision = kind === 'publish' ? row.new_head : row.revision
    const detail = { doc_id: id, revision, sha256: row.sha256, kind, at: now() }
    if (current(token)) {
      if (kind === 'save' || kind === 'restore') remember(id)
      writeSeq += 1
      update({ events: [...state.events, detail].slice(-20), head: maximum(state.head, revision),
        ...(state.lastSaved && state.lastSaved.revision > revision ? {} : { lastSaved: { revision, sha256: row.sha256, at: detail.at, graph: row.graph ?? state.lastSaved?.graph } }),
        ...(kind === 'publish' ? { published: maximum(state.published, revision) } : { conflict: null, headLookupFailed: false, revisions: merge([row]) }),
        ...(kind === 'restore' && op === operation ? { restoredGraph: row.graph, restoredKey: state.restoredKey + 1 } : {}) })
    }
    if (globalThis.dispatchEvent && globalThis.CustomEvent) globalThis.dispatchEvent(new CustomEvent(SAVED_REVISION_EVENT, { detail }))
  }
  function reconcile(result, token, mine) {
    if (!result.conflict || !fresh(token)) return false
    readSeq += 1
    applied = { head: readSeq, revisions: readSeq }
    const serverHead = maximum(state.head, result.head)
    const head = result.headLookupFailed ? state.head : serverHead
    update({ head, headLookupFailed: !!result.headLookupFailed,
      ...(mine ? { conflict: { serverHead, mine } } : {}) })
    return true
  }
  return {
    getLayout, putLayout,
    state, subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    currentDocId: () => docId,
    rememberedDocId: () => remembered,
    async reopenRemembered() {
      if (remembered === null || reopened || docId !== remembered) return { state: 'none' }
      reopened = true
      const token = this.beginGraphOperation()
      const head = await this.head(remembered)
      if (!this.isCurrentOperation(token)) return { state: 'superseded' }
      if ((head.ok && head.revision == null) || head.status === 404) return { state: 'empty' }
      if (!head.ok) return { state: 'failed', detail: head.detail }
      const row = await this.revision(remembered, head.revision)
      if (!this.isCurrentOperation(token)) return { state: 'superseded' }
      if (!row.ok || !row.graph) return { state: 'failed', detail: row.detail || 'No saved graph.' }
      return { state: 'ready', docId: remembered, revision: head.revision, graph: row.graph, token }
    },
    currentGeneration: () => generation,
    beginGraphOperation: () => ({ ...capture(docId), operation: ++operation }),
    isCurrentOperation: token => current(token) && token.operation === operation,
    captureMine(graph) { if (state.conflict && !state.conflict.captured) update({ conflict: { ...state.conflict, mine: JSON.parse(JSON.stringify(graph)), captured: true } }) },
    setDocId(id) { identifier(id); if (id !== docId) { generation += 1; operation += 1; writeSeq = 0; readSeq = 0; applied = { head: 0, revisions: 0 }; docId = id; remember(id); update({ ...initial(), restoredKey: state.restoredKey + 1 }) } },
    takeGraph(graph) { operation += 1; update({ restoredGraph: graph, restoredKey: state.restoredKey + 1 }) },
    async save(id, graph, expectedHead) {
      identifier(id)
      const token = capture(id), op = id === docId ? ++operation : operation
      const mine = JSON.parse(JSON.stringify(graph))
      const positions = graphLayouts.get(graph)?.positions?.()
      const started = now()
      const result = await request(id, 'save', { graph: mine, expected_head: expectedHead }, true, token)
      if (current(token)) update({ saveTimes: [...state.saveTimes, Math.max(0, now() - started)].slice(-20) })
      if (result.ok) {
        saved(id, { ...result, graph: mine }, 'save', token, op)
        if (positions) await this.putLayout(id, result.sha256, { schema: 'leaf.lab-layout.v1', positions })
      }
      else reconcile(result, token, mine)
      return result
    },
    async head(id) {
      const token = { ...capture(id), readSeq: ++readSeq }
      const result = await request(id, 'head', undefined, true, token)
      if (fresh(token) && token.readSeq >= applied.head) {
        applied.head = token.readSeq
        if (result.ok || result.status === 404) update({ head: maximum(state.head, result.ok ? result.revision : null), headLoaded: true, headLookupFailed: false,
          ...(result.ok && state.conflict ? { conflict: { ...state.conflict, serverHead: maximum(state.conflict.serverHead, result.revision) } } : {}),
          ...(result.published_revision == null ? {} : { published: maximum(state.published, result.published_revision) }) })
        else update({ headLookupFailed: true })
      }
      return result
    },
    async revisions(id, { limit, before } = {}) {
      const token = { ...capture(id), readSeq: ++readSeq }
      const query = new URLSearchParams()
      if (limit != null) query.set('limit', limit)
      if (before != null) query.set('before', before)
      const result = await request(id, `revisions${query.size ? `?${query}` : ''}`, undefined, true, token)
      if (result.ok && fresh(token) && token.readSeq >= applied.revisions) { applied.revisions = token.readSeq; update({ revisions: merge(result.revisions) }) }
      return result
    },
    revision: (id, n) => request(id, `revisions/${encodeURIComponent(n)}`),
    async exportBundle(id, revision) {
      const token = capture(id), scope = authority()
      const result = await request(id, `revisions/${encodeURIComponent(revision)}/export`, undefined, false, token)
      if (!result.ok) return result
      if (!current(token) || scope !== authority()) return { ok: false, detail: 'Document or account changed. Try exporting again.' }
      try {
        const { ok: unused, ...bundle } = result
        verifyLabBundle(bundle)
        if (bundle.parts.identity.doc_id !== id || bundle.parts.identity.revision !== Number(revision)) throw new Error(bundleError)
        return { ok: true, bundle }
      } catch { return { ok: false, detail: bundleError } }
    },
    async reopenBundle(text) {
      const token = this.beginGraphOperation(), scope = authority()
      let bundle
      try {
        if (typeof text !== 'string' || new TextEncoder().encode(text).length > 64 * 1024 * 1024) throw new Error(bundleError)
        bundle = verifyLabBundle(JSON.parse(text))
      } catch { return { ok: false, detail: bundleError } }
      const { identity, graph, layout } = bundle.parts
      // Digests establish content integrity, not authority. Reauthorize against
      // the server's pinned revision before putting imported content on canvas.
      const allowed = await request(identity.doc_id, `revisions/${identity.revision}/export`, undefined, false, token)
      if (!allowed.ok) return allowed
      if (!this.isCurrentOperation(token) || scope !== authority()) return { ok: false, detail: 'Document or account changed. Try reopening again.' }
      const head = await request(identity.doc_id, 'head', undefined, false, token)
      if (!head.ok) return head
      if (!this.isCurrentOperation(token) || scope !== authority()) return { ok: false, detail: 'Document or account changed. Try reopening again.' }
      try {
        const { ok: unused, ...authorized } = allowed
        verifyLabBundle(authorized)
        if (canonicalJSON(authorized.parts.identity) !== canonicalJSON(identity)) throw new Error(bundleError)
      } catch { return { ok: false, detail: bundleError } }
      this.setDocId(identity.doc_id)
      operation += 1
      writeSeq += 1
      if (layout) graphLayouts.set(graph, { load: async () => layout })
      update({ openedBundle: bundle, restoredGraph: graph, restoredKey: state.restoredKey + 1,
        head: head.revision, published: head.published_revision ?? null, headLoaded: true, headLookupFailed: false,
        conflict: null, revisions: merge([identity]), lastSaved: { ...identity, graph } })
      remember(identity.doc_id)
      return { ok: true, bundle, revision: identity.revision }
    },
    async diff(id, before, after, { graphs = true } = {}) {
      const token = capture(id)
      const result = await request(id, `diff?before=${encodeURIComponent(before)}&after=${encodeURIComponent(after)}`, undefined, true, token)
      if (!result.ok || !graphs) return result
      // List rows contain metadata only. Load the compared documents to name
      // blocks in settings changes without rendering any document payload.
      const [left, right] = await Promise.all([request(id, `revisions/${encodeURIComponent(before)}`, undefined, true, token), request(id, `revisions/${encodeURIComponent(after)}`, undefined, true, token)])
      if (!left.ok || !right.ok) return !left.ok ? left : right
      return { ...result, beforeGraph: left.graph, afterGraph: right.graph }
    },
    async restore(id, revision, expectedHead) {
      const token = capture(id), op = id === docId ? ++operation : operation
      const result = await request(id, 'restore', { revision, expected_head: expectedHead }, true, token)
      if (result.ok) saved(id, result, 'restore', token, op)
      else if (reconcile(result, token)) await this.revisions(id)
      return result
    },
    async publish(id, expectedHead) {
      const token = capture(id), op = id === docId ? ++operation : operation
      const result = await request(id, 'publish', { expected_head: expectedHead }, true, token)
      if (result.ok) saved(id, result, 'publish', token, op)
      else if (reconcile(result, token)) await this.revisions(id)
      return result
    },
  }
}

function browserStorage() {
  try { return globalThis.localStorage || null } catch { return null }
}

export const labArtifactsClient = createLabArtifactsClient({ storage: browserStorage() })
