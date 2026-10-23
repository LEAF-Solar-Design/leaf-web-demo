import { canonicalJSON } from '../contract.js'

const timestamp = value => typeof value === 'number' ? value : Date.parse(value)
const artifactCaches = new WeakMap()

function changedBlockIds(graph, changes) {
  const ids = new Set()
  const pipeKey = pipe => canonicalJSON([pipe.from.node, pipe.from.port, pipe.to.node, pipe.to.port])
  const touchPipe = pipe => { if (pipe) { ids.add(pipe.from.node); ids.add(pipe.to.node) } }
  for (const { path, before, after } of changes) {
    const [group, index, field, leaf] = path.split('/').slice(1).map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'))
    if (group === 'blocks') {
      if (index === undefined) {
        for (const block of after || []) {
          const previous = (before || []).find(item => item.id === block.id)
          if (!previous || canonicalJSON(previous) !== canonicalJSON(block)) ids.add(block.id)
        }
      } else {
        ids.add(graph.blocks[index]?.id)
      }
    }
    if (group === 'pipes') {
      if (index === undefined) {
        const old = before || [], next = after || []
        old.filter(pipe => !next.some(item => pipeKey(item) === pipeKey(pipe))).forEach(touchPipe)
        next.filter(pipe => !old.some(item => pipeKey(item) === pipeKey(pipe))).forEach(touchPipe)
      } else {
        touchPipe(graph.pipes?.[index])
        if (field === undefined) {
          touchPipe(before)
          touchPipe(after)
        } else if (['from', 'to'].includes(field) && leaf === 'node') {
          ids.add(before)
        }
      }
    }
  }
  return graph.blocks.filter(block => ids.has(block.id)).map(block => block.id)
}

// Host values stay in this read model and never enter a graph or a request.
export function createAfterSaveClient({ provider, artifacts, now = Date.now }) {
  if (!artifactCaches.has(artifacts)) artifactCaches.set(artifacts, { revisions: new Map(), listeners: new Set() })
  const { revisions, listeners } = artifactCaches.get(artifacts)
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
        const [diffResult, revisionResult] = await Promise.allSettled([
          Promise.resolve().then(() => saved.revision === 1 ? null
            : artifacts.diff(docId, saved.revision - 1, saved.revision, { graphs: false })),
          Promise.resolve().then(() => artifacts.revision(docId, saved.revision)),
        ])
        const result = diffResult.value
        const revision = revisionResult.value
        const graph = revision?.graph
        if (revision?.ok === false || !graph) return
        entry.document = graph
        if (result?.ok === false || (saved.revision > 1 && !Array.isArray(result?.changes))) return
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
