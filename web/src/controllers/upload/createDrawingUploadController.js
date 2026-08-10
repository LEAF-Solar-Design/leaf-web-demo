const DEFAULT_POLICY = { enabled: false, accepted: ['.dwg', '.dxf'], max_bytes: 25 * 1024 * 1024 }

function message(error) {
  return String(error?.message || error || 'Drawing upload failed.')
}

// pollMs * maxPolls is the client's patience for a server-side extraction that
// legitimately runs for minutes (the WorkItem ceiling is 600s, the server's own
// budget 900s). 500ms * 1800 = 900s: giving up sooner reports a failure the
// server never declared — the 60s default did exactly that on 2026-08-10.
export function createDrawingUploadController({ services, onReady, pollMs = 500, maxPolls = 1800 } = {}) {
  if (!services) throw new TypeError('createDrawingUploadController requires services')
  let state = { policy: null, policyLoading: false, busy: false, phase: 'idle', error: null, receipt: null }
  let snapshot = state
  let sequence = 0
  let disposed = false
  const listeners = new Set()
  const publish = (patch) => {
    if (disposed) return
    state = { ...state, ...patch }
    snapshot = state
    listeners.forEach((listener) => listener())
  }

  const loadPolicy = async () => {
    const run = ++sequence
    publish({ policyLoading: true, error: null })
    try {
      const policy = await services.policy()
      if (run === sequence) publish({ policy: { ...DEFAULT_POLICY, ...policy }, policyLoading: false })
      return policy
    } catch (error) {
      if (run === sequence) publish({ policy: DEFAULT_POLICY, policyLoading: false, error: message(error) })
      return null
    }
  }

  const upload = async (file) => {
    if (!file || state.busy) return null
    const policy = state.policy || DEFAULT_POLICY
    const ext = `.${String(file.name || '').split('.').pop().toLowerCase()}`
    if (!policy.enabled) { publish({ error: 'Drawing uploads are not available on this deployment.' }); return null }
    if (!(policy.accepted || []).map((item) => item.toLowerCase()).includes(ext)) {
      publish({ error: `Choose ${policy.accepted.join(' or ')}.` }); return null
    }
    if (file.size > policy.max_bytes) {
      publish({ error: `The file exceeds the ${Math.ceil(policy.max_bytes / 1024 / 1024)} MB upload limit.` }); return null
    }
    const run = ++sequence
    publish({ busy: true, phase: 'uploading', error: null, receipt: null })
    try {
      const receipt = await services.upload(file)
      if (run !== sequence) return null
      publish({ receipt, phase: receipt.status === 'ready' ? 'loading' : 'extracting' })
      let status = receipt
      for (let attempt = 0; status.status !== 'ready' && attempt < maxPolls; attempt += 1) {
        if (status.status === 'failed') throw new Error(status.error?.message || status.error || 'Drawing extraction failed.')
        await services.wait(pollMs)
        if (run !== sequence) return null
        status = await services.status(receipt.drawing_id, receipt.guest_session, receipt.tenant_id)
      }
      if (status.status !== 'ready') {
        throw new Error('Extraction is still running on the server. It has not failed, so reopen the drawing in a few minutes.')
      }
      publish({ phase: 'loading' })
      const view = await services.intake(receipt.drawing_id, receipt.guest_session, receipt.tenant_id)
      if (run !== sequence) return null
      await onReady?.({ receipt, status, view })
      publish({ busy: false, phase: 'ready', error: null })
      return { receipt, status, view }
    } catch (error) {
      if (run === sequence) publish({ busy: false, phase: 'failed', error: message(error) })
      return null
    }
  }

  return {
    getSnapshot: () => snapshot,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    start() { disposed = false },
    dispose() { disposed = true; sequence += 1 },
    loadPolicy,
    upload,
    cancel() { sequence += 1; publish({ busy: false, phase: 'idle', error: null }) },
  }
}

export default createDrawingUploadController
