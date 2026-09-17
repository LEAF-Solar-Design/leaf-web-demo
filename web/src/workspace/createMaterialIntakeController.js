const idle = () => ({ phase: 'idle', target: null, drawing: null, error: null, attached: false, replayed: false, beginRefused: null })

export function createMaterialIntakeController({ services: { importUpload }, isUploadInFlight = () => false } = {}) {
  let state = idle()
  let generation = 0
  let attachment = null
  const ledger = new Map()
  const listeners = new Set()
  const publish = (patch) => {
    state = { ...state, ...patch }
    listeners.forEach((listener) => listener())
  }
  const attach = (entry) => {
    let record = ledger.get(entry.key)
    if (!record) {
      record = { key: entry.key, status: 'pending', result: null, error: null, request: null }
      ledger.set(entry.key, record)
      record.request = Promise.resolve().then(() => importUpload(entry.projectId, entry.source, { idempotencyKey: entry.key }))
        .then((result) => {
          record.status = 'attached'
          record.result = result
          return result
        }, (error) => {
          record.status = 'failed'
          record.error = error
          if (ledger.get(entry.key) === record) ledger.delete(entry.key)
          throw error
        })
    }
    const run = generation
    publish({ phase: 'attaching', error: null })
    return record.request
      .then((result) => {
        if (run === generation) publish({ phase: 'attached', drawing: result.drawingVersion, attached: true, replayed: result.replayed, error: null })
        return result
      }, (error) => {
        if (run === generation) publish({ phase: 'attach-failed', error: String(error?.message || error) })
        return null
      })
  }
  const reset = () => { generation += 1; attachment = null; publish(idle()) }
  const clearRefusal = () => { if (state.beginRefused) publish({ beginRefused: null }) }
  return {
    getSnapshot: () => state,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    begin({ projectId, projectName, fileName }) {
      if (isUploadInFlight()) {
        publish({ beginRefused: 'Wait for the current upload to finish.' })
        return false
      }
      generation += 1
      attachment = null
      publish({ ...idle(), phase: 'pending', target: { projectId, projectName, fileName } })
      return true
    },
    onUploadReady({ receipt, status }) {
      clearRefusal()
      if (!state.target) return
      if (attachment) return attach(attachment)
      const drawingId = receipt?.drawing_id
      const version = status?.extracted_version
      if (!drawingId || !Number.isInteger(version) || version <= 0) {
        publish({ phase: 'attach-failed', error: 'The upload did not report an extracted version.' })
        return
      }
      const { projectId, fileName } = state.target
      attachment = { projectId, source: { drawingId, version, name: fileName }, key: `b3:${projectId}:${drawingId}:${version}` }
      return attach(attachment)
    },
    retry() { if (state.phase === 'attach-failed' && attachment) return attach(attachment) },
    reset,
    clearRefusal,
    dispose() { reset(); ledger.clear(); listeners.clear() },
  }
}

export default createMaterialIntakeController
