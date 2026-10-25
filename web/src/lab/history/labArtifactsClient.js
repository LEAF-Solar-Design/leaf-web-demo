import { authHeaders, config } from '../../api.js'
import { fetchWithBudget } from '../../fetchBudget.js'
import { humanizeError } from '../../errorHumanize.js'

export const SAVED_REVISION_EVENT = 'leaf.lab.saved-revision'
function identifier(id) {
  if (typeof id !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}(?![\s\S])/.test(id)) throw new Error('Enter a valid Document ID.')
}
const initial = () => ({ head: null, published: null, revisions: [], lastSaved: null, conflict: null,
  events: [], saveTimes: [], requestTimes: [], restoredGraph: null, restoredKey: 0, headLoaded: false, headLookupFailed: false })

export function createLabArtifactsClient({ fetchImpl = (...args) => globalThis.fetch(...args), now = Date.now } = {}) {
  let docId = 'strategy'
  let generation = 0, writeSeq = 0, readSeq = 0, operation = 0
  let applied = { head: 0, revisions: 0 }
  const state = initial()
  const listeners = new Set()
  const update = patch => { Object.assign(state, patch); listeners.forEach(listener => listener()) }
  const capture = id => ({ id, generation, writeSeq, readSeq })
  const current = token => token.id === docId && token.generation === generation
  const fresh = token => current(token) && token.writeSeq === writeSeq
  const maximum = (left, right) => right == null ? left : left == null ? right : Math.max(left, right)
  const merge = rows => [...new Map([...state.revisions, ...rows].map(row => [row.revision, row])).values()].sort((a, b) => a.revision - b.revision)
  async function request(id, path, body, resolveConflict = true, token = capture(id)) {
    identifier(id)
    const started = now()
    try {
      const response = await fetchWithBudget(fetchImpl, `${config.apiBase}/api/lab/docs/${encodeURIComponent(id)}/${path}`, {
        method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', redirect: 'error',
        headers: { accept: 'application/json', 'content-type': 'application/json', 'X-Tenant-Id': config.tenant, ...authHeaders() },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      })
      const data = await response.json().catch(() => ({}))
      if (response.ok) return { ...data, ok: true }
      const detail = humanizeError(data?.error?.message || (typeof data?.detail === 'string' ? data.detail : data?.detail?.message))
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
  function saved(id, row, kind, token, op) {
    const revision = kind === 'publish' ? row.new_head : row.revision
    const detail = { doc_id: id, revision, sha256: row.sha256, kind, at: now() }
    if (current(token)) {
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
    state, subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    currentDocId: () => docId,
    currentGeneration: () => generation,
    beginGraphOperation: () => ({ ...capture(docId), operation: ++operation }),
    isCurrentOperation: token => current(token) && token.operation === operation,
    captureMine(graph) { if (state.conflict && !state.conflict.captured) update({ conflict: { ...state.conflict, mine: JSON.parse(JSON.stringify(graph)), captured: true } }) },
    setDocId(id) { identifier(id); if (id !== docId) { generation += 1; operation += 1; writeSeq = 0; readSeq = 0; applied = { head: 0, revisions: 0 }; docId = id; update({ ...initial(), restoredKey: state.restoredKey + 1 }) } },
    takeGraph(graph) { operation += 1; update({ restoredGraph: graph, restoredKey: state.restoredKey + 1 }) },
    async save(id, graph, expectedHead) {
      identifier(id)
      const token = capture(id), op = id === docId ? ++operation : operation
      const mine = JSON.parse(JSON.stringify(graph))
      const started = now()
      const result = await request(id, 'save', { graph: mine, expected_head: expectedHead }, true, token)
      if (current(token)) update({ saveTimes: [...state.saveTimes, Math.max(0, now() - started)].slice(-20) })
      if (result.ok) saved(id, { ...result, graph: mine }, 'save', token, op)
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

export const labArtifactsClient = createLabArtifactsClient()
