const timestamp = value => typeof value === 'number' ? value : Date.parse(value)

// Host values stay in this read model and never enter a graph or a request.
export function createAfterSaveClient({ provider, artifacts, now = Date.now }) {
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
      const previous = saved ? [...(artifacts.state.revisions || [])]
        .filter(row => row.revision < saved.revision).sort((a, b) => b.revision - a.revision)[0] : null
      return { saved, match, elapsed_ms: saved ? Math.max(0, now() - timestamp(saved.at)) : 0,
        status, previousDocument: previous?.graph || null }
    },
    // Providers refresh through the mount's revision prop; read() always reads
    // the latest rows. Subscribe lazily so abandoned renders retain no listeners.
    subscribe(listener) { return artifacts.subscribe(listener) },
  }
}
