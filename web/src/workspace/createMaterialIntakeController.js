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
    const existing = ledger.get(entry.key)
    if (existing) return existing.promise
    const run = generation
    entry.status = 'pending'
    ledger.set(entry.key, entry)
    publish({ phase: 'attaching', error: null })
    entry.promise = Promise.resolve().then(() => importUpload(entry.projectId, entry.source, { idempotencyKey: entry.key }))
      .then((result) => {
        entry.status = 'attached'
        if (run === generation) publish({ phase: 'attached', drawing: result.drawingVersion, attached: true, replayed: result.replayed, error: null })
        return result
      }, (error) => {
        if (ledger.get(entry.key) === entry) ledger.delete(entry.key)
        if (run === generation) publish({ phase: 'attach-failed', error: String(error?.message || error) })
        return null
      }).finally(() => { entry.promise = null })
    return entry.promise
  }
  const reset = () => { generation += 1; attachment = null; publish(idle()) }
  return {
    getSnapshot: () => state,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    begin({ projectId, projectName, fileName }) {
      if (state.target && isUploadInFlight()) {
        publish({ beginRefused: 'Wait for the current upload to finish.' })
        return false
      }
      generation += 1
      attachment = null
      publish({ ...idle(), phase: 'pending', target: { projectId, projectName, fileName } })
      return true
    },
    onUploadReady({ receipt, status }) {
      if (!state.target) return
      if (attachment) return attachment.promise
      const drawingId = receipt?.drawing_id
      const version = status?.extracted_version
      if (!drawingId || !Number.isInteger(version) || version <= 0) {
        publish({ phase: 'attach-failed', error: 'The upload did not report an extracted version.' })
        return
      }
      const { projectId, fileName } = state.target
      attachment = { projectId, source: { drawingId, version, name: fileName }, key: `b3:${projectId}:${drawingId}:${version}`, promise: null }
      return attach(attachment)
    },
    retry() { if (state.phase === 'attach-failed' && attachment) return attach(attachment) },
    reset,
    dispose() { reset(); ledger.clear(); listeners.clear() },
  }
}

export default createMaterialIntakeController
