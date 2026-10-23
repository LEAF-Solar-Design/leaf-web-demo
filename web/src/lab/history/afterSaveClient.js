import { canonicalJSON } from '../contract.js'

const timestamp = value => typeof value === 'number' ? value : Date.parse(value)

function changedBlockIds(graph, changes) {
  const ids = new Set()
  const pipeKey = pipe => `${pipe.from.node}/${pipe.from.port}>${pipe.to.node}/${pipe.to.port}`
  const touchPipe = pipe => { if (pipe) { ids.add(pipe.from.node); ids.add(pipe.to.node) } }
  for (const { op, path, before, after } of changes) {
    const [group, index, field] = path.split('/').slice(1).map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'))
    if (group === 'blocks') {
      if (index === undefined) {
        for (const block of after || []) {
          const previous = (before || []).find(item => item.id === block.id)
          if (!previous || canonicalJSON(previous.settings || {}) !== canonicalJSON(block.settings || {})) ids.add(block.id)
        }
      } else if (field === 'settings' || (field === undefined && op === 'add')) {
        ids.add(graph.blocks[index]?.id || after?.id)
      }
    }
    if (group === 'pipes') {
      if (index === undefined) {
        const old = before || [], next = after || []
        old.filter(pipe => !next.some(item => pipeKey(item) === pipeKey(pipe))).forEach(touchPipe)
        next.filter(pipe => !old.some(item => pipeKey(item) === pipeKey(pipe))).forEach(touchPipe)
      } else if (field === undefined && ['add', 'remove'].includes(op)) {
        touchPipe(op === 'add' ? after || graph.pipes?.[index] : before)
      }
    }
  }
  return graph.blocks.filter(block => ids.has(block.id)).map(block => block.id)
}

// Host values stay in this read model and never enter a graph or a request.
export function createAfterSaveClient({ provider, artifacts, now = Date.now }) {
  const revisions = new Map()
  const listeners = new Set()
  function savedChanges() {
    const saved = artifacts.state.lastSaved
    if (!saved) return null
    const docId = artifacts.currentDocId()
    const key = JSON.stringify([docId, saved.revision])
    if (!revisions.has(key)) {
      const entry = { changed: null, document: null }
      revisions.set(key, entry)
      // Defer requests until after read() so a request's state updates cannot
      // re-enter a render. Cache failures too: each revision is requested once.
      Promise.resolve().then(async () => {
        const result = await (saved.revision === 1 ? artifacts.revision(docId, 1)
          : artifacts.diff(docId, saved.revision - 1, saved.revision))
        const graph = saved.revision === 1 ? result?.graph : result?.afterGraph
        if (result?.ok === false || !graph || (saved.revision > 1 && !Array.isArray(result?.changes))) return
        entry.document = graph
        entry.changed = { revision: saved.revision, blockIds: saved.revision === 1
          ? graph.blocks.map(block => block.id) : changedBlockIds(graph, result.changes) }
      }).catch(() => {}).finally(() => listeners.forEach(listener => listener()))
    }
    return revisions.get(key)
  }
  return {
    read() {
      const saved = artifacts.state.lastSaved || null
      const match = saved?.sha256 ? (provider.rowsOf?.('practice') || [])
        .filter(row => row.data.document_sha256 === saved.sha256)
        .reduce((latest, row) => !latest || (timestamp(row.data.created_at) || 0)
          >= (timestamp(latest.data.created_at) || 0) ? row : latest, null) : null
      const state = match?.data.state
      const status = !saved ? 'none' : !match || state === 'queued' ? 'pending'
        : ['running', 'finished', 'failed', 'unavailable', 'expired'].includes(state) ? state : 'pending'
      const changes = savedChanges()
      return { saved, match, elapsed_ms: saved ? Math.max(0, now() - timestamp(saved.at)) : 0,
        status, changed: changes?.changed || null, savedDocument: changes?.document || null }
    },
    // Providers refresh through the mount's revision prop; read() always reads
    // the latest rows. Subscribe lazily so abandoned renders retain no listeners.
    subscribe(listener) {
      listeners.add(listener)
      const unsubscribe = artifacts.subscribe(() => { savedChanges(); listener() })
      savedChanges()
      return () => { listeners.delete(listener); unsubscribe() }
    },
  }
}
