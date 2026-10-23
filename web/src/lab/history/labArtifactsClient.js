import { authHeaders, config } from '../../api.js'
import { fetchWithBudget } from '../../fetchBudget.js'

export const SAVED_REVISION_EVENT = 'leaf.lab.saved-revision'
function identifier(id) {
  if (typeof id !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}(?![\s\S])/.test(id)) throw new Error('Test: document id refused')
}
const initial = () => ({ head: null, published: null, revisions: [], lastSaved: null, conflict: null,
  events: [], saveTimes: [], requestTimes: [], restoredGraph: null, restoredKey: 0 })

export function createLabArtifactsClient({ fetchImpl = (...args) => globalThis.fetch(...args), now = Date.now } = {}) {
  let docId = 'strategy'
  let headEpoch = 0
  const state = initial()
  const listeners = new Set()
  const update = patch => { Object.assign(state, patch); listeners.forEach(listener => listener()) }
  async function request(id, path, body, resolveConflict = true) {
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
      if (response.status === 409 && resolveConflict) {
        const head = await request(id, 'head', undefined, false)
        return { ok: false, conflict: true, head: head.ok ? head.revision : null, status: 409, detail: data.detail }
      }
      return { ok: false, status: response.status, detail: data.detail }
    } catch { return { ok: false, status: 0, detail: 'Test: service unavailable' } }
    finally {
      if (id === docId) update({ requestTimes: [...state.requestTimes, Math.max(0, now() - started)].slice(-20) })
    }
  }
  function saved(id, row, kind) {
    headEpoch += 1
    const revision = kind === 'publish' ? row.new_head : row.revision
    const detail = { doc_id: id, revision, sha256: row.sha256, kind, at: now() }
    update({ events: [...state.events, detail].slice(-20), ...(id === docId ? {
      head: revision, lastSaved: { revision, sha256: row.sha256, at: detail.at },
      ...(kind === 'publish' ? { published: revision } : { conflict: null,
        revisions: [...state.revisions.filter(item => item.revision !== revision), row].sort((a, b) => a.revision - b.revision) }),
      ...(kind === 'restore' ? { restoredGraph: row.graph, restoredKey: state.restoredKey + 1 } : {}),
    } : {}),
    })
    if (globalThis.dispatchEvent && globalThis.CustomEvent) globalThis.dispatchEvent(new CustomEvent(SAVED_REVISION_EVENT, { detail }))
  }
  return {
    state, subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    currentDocId: () => docId,
    setDocId(id) { identifier(id); if (id !== docId) { headEpoch += 1; docId = id; update({ ...initial(), restoredKey: state.restoredKey + 1 }) } },
    takeGraph(graph) { update({ restoredGraph: graph, restoredKey: state.restoredKey + 1 }) },
    async save(id, graph, expectedHead) {
      identifier(id)
      const mine = JSON.parse(JSON.stringify(graph))
      const started = now()
      const result = await request(id, 'save', { graph: mine, expected_head: expectedHead })
      if (id === docId) update({ saveTimes: [...state.saveTimes, Math.max(0, now() - started)].slice(-20) })
      if (result.ok) saved(id, result, 'save')
      else if (result.conflict && id === docId) update({ head: result.head, conflict: { serverHead: result.head, mine } })
      return result
    },
    async head(id) {
      const epoch = headEpoch
      const result = await request(id, 'head')
      if (result.ok && id === docId && epoch === headEpoch) update({ head: result.revision,
        ...(result.published_revision == null ? {} : { published: result.published_revision }) })
      return result
    },
    async revisions(id, { limit, before } = {}) {
      const epoch = headEpoch
      const query = new URLSearchParams()
      if (limit != null) query.set('limit', limit)
      if (before != null) query.set('before', before)
      const result = await request(id, `revisions${query.size ? `?${query}` : ''}`)
      if (result.ok && id === docId && epoch === headEpoch) update({ revisions: result.revisions })
      return result
    },
    revision: (id, n) => request(id, `revisions/${encodeURIComponent(n)}`),
    async diff(id, before, after, { graphs = true } = {}) {
      const result = await request(id, `diff?before=${encodeURIComponent(before)}&after=${encodeURIComponent(after)}`)
      if (!result.ok || !graphs) return result
      // List rows contain metadata only. Load the compared documents to name
      // blocks in settings changes without rendering any document payload.
      const [left, right] = await Promise.all([request(id, `revisions/${encodeURIComponent(before)}`), request(id, `revisions/${encodeURIComponent(after)}`)])
      if (!left.ok || !right.ok) return !left.ok ? left : right
      return { ...result, beforeGraph: left.graph, afterGraph: right.graph }
    },
    async restore(id, revision, expectedHead) {
      const result = await request(id, 'restore', { revision, expected_head: expectedHead })
      if (result.ok) saved(id, result, 'restore')
      return result
    },
    async publish(id, expectedHead) {
      const result = await request(id, 'publish', { expected_head: expectedHead })
      if (result.ok) saved(id, result, 'publish')
      return result
    },
  }
}

export const labArtifactsClient = createLabArtifactsClient()
