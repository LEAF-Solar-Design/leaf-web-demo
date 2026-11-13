import { authHeaders, config, noteUnauthorized } from '../../api.js'
import { fetchWithBudget } from '../../fetchBudget.js'
import { labValueStore, terminalState } from './valueStore.js'

const headersFor = accept => ({ accept, 'X-Tenant-Id': config.tenant, ...authHeaders() })
async function request(path, { fetchImpl = globalThis.fetch, body, signal, onUnauthorized } = {}) {
  const headers = { ...headersFor('application/json'), 'content-type': 'application/json' }
  try {
    const response = await fetchWithBudget(fetchImpl, `${config.apiBase}/api/lab/${path}`, {
      method: body === undefined ? 'GET' : 'POST', headers, signal, credentials: 'same-origin', redirect: 'error',
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    noteUnauthorized(response, 'lab-runs', headers.Authorization)
    if (response.status === 401) onUnauthorized?.(headers.Authorization)
    const data = await response.json().catch(() => ({}))
    return response.ok ? { ...data, ok: true } : { ok: false, status: response.status, detail: data.detail }
  } catch { return { ok: false, status: 0, detail: 'Run service unavailable' } }
}
export function newSubmissionId() {
  return `v1.${Date.now()}.${crypto.randomUUID().replaceAll('-', '')}`
}
export async function submitRun({ docId, graphRevision, submissionId, reconcileOnly = false, projectId = '', fetchImpl }) {
  const result = await request('runs', { fetchImpl, body: { doc_id: docId, graph_revision: graphRevision,
    ...(projectId ? { project_id: projectId } : {}),
    ...(submissionId === undefined ? {} : { submission_id: submissionId, reconcile_only: reconcileOnly }) } })
  if (result.ok && typeof result.run_id === 'string' && typeof result.state === 'string') {
    return { ok: true, runId: result.run_id, state: result.state }
  }
  return { ...result, ok: false, outcomeUnknown: result.ok || result.status === 0 || result.status >= 500 }
}
export async function discoverRuns({ docId, projectId = '', fetchImpl }) {
  const query = new URLSearchParams({ doc_id: docId, project_id: projectId })
  const result = await request(`runs?${query}`, { fetchImpl })
  if (!result.ok || !Array.isArray(result.runs) || result.runs.length > 20) return { ...result, ok: false }
  if (result.runs.some(row => !row || row.doc_id !== docId || row.project_id !== projectId
    || !/^v1\.[0-9]{13}\.[0-9a-f]{32}$/.test(row.submission_id)
    || !Number.isInteger(row.graph_revision) || row.graph_revision < 1
    || typeof row.run_id !== 'string' || !row.run_id
    || !['queued', 'running', 'finished', 'failed', 'cancelled'].includes(row.state))) return { ok: false }
  return result
}
export const cancelRun = ({ runId, fetchImpl }) => request(`runs/${encodeURIComponent(runId)}/cancel`, { fetchImpl, body: {} })

const defaultSleep = (ms, signal) => new Promise(resolve => {
  const finish = () => { clearTimeout(timer); signal?.removeEventListener('abort', finish); resolve() }
  const timer = setTimeout(finish, ms)
  signal?.addEventListener('abort', finish, { once: true })
  if (signal?.aborted) finish()
})

export function openRunStream({ runIds, store = labValueStore, fetchImpl = globalThis.fetch, signal,
  now = Date.now, sleep = defaultSleep, random = Math.random }) {
  if (!runIds.length || runIds.length > 8) throw new Error('A stream requires one to eight runs')
  let stopped = false, paused = false, running = false, controller, waiting, currentReader, restartRequested = false, generation = 0
  let authExpired = false, expiredAuthorization
  const rememberUnauthorized = authorization => { authExpired = true; expiredAuthorization = authorization }
  const pendingSnapshots = new Set(runIds.filter(id => {
    const run = store.getRun(id)
    return terminalState(run?.state) && run.state !== 'missing' && run.transport.status !== 'finished'
  }))
  const active = new Set(runIds.filter(id => !terminalState(store.getRun(id)?.state) || pendingSnapshots.has(id)))
  // Keep terminal reconciliation resumable until its retained values are read.
  pendingSnapshots.forEach(id => store.resetSnapshot(id, {}))
  const transport = patch => active.forEach(id => store.setTransport(id, patch))
  async function snapshot(id, connectionSignal) {
    pendingSnapshots.add(id)
    const row = await request(`runs/${encodeURIComponent(id)}`, { fetchImpl, signal: connectionSignal, onUnauthorized: rememberUnauthorized })
    if (stopped || connectionSignal.aborted) return
    if (!row.ok) { if (row.status === 401) { paused = true; transport({ status: 'auth_expired' }) }; throw new Error('Snapshot unavailable') }
    store.resetSnapshot(id, { state: row.state, graph_revision: row.graph_revision })
    let after = -1
    for (;;) {
      const page = await request(`runs/${encodeURIComponent(id)}/events?after_seq=${after}&limit=500`, { fetchImpl, signal: connectionSignal, onUnauthorized: rememberUnauthorized })
      if (stopped || connectionSignal.aborted) return
      if (!page.ok) { if (page.status === 401) { paused = true; transport({ status: 'auth_expired' }) }; throw new Error('Snapshot events unavailable') }
      for (const event of page.events || []) store.apply(id, event)
      const next = store.getRun(id)?.lastSeq ?? -1
      if ((page.events || []).length < 500) break
      if (next <= after) throw new Error('Snapshot cursor did not advance')
      after = next
    }
    store.addGap(id, { recovered: true })
    if (terminalState(store.getRun(id)?.state)) {
      store.setTransport(id, { status: 'finished' }); active.delete(id)
    }
    pendingSnapshots.delete(id)
    store.finishSnapshot(id)
  }
  async function loop(epoch) {
    running = true
    let failures = 0
    try {
      while (!stopped && !paused && active.size && epoch === generation) {
        controller = new AbortController()
        let reader, delay = null, immediate = false
        try {
          for (const id of pendingSnapshots) await snapshot(id, controller.signal)
          if (!active.size || stopped || paused) break
          const ids = [...active]
          const headers = headersFor('text/event-stream')
          const cursors = ids.filter(id => (store.getRun(id)?.lastSeq ?? -1) >= 0)
            .map(id => `${id}:${store.getRun(id).lastSeq}`).join(',')
          if (cursors) headers['Last-Event-ID'] = cursors
          transport({ status: 'connecting' })
          const response = await fetchImpl(`${config.apiBase}/api/lab/stream/runs?runs=${ids.map(encodeURIComponent).join(',')}&after_seq=-1`, {
            headers, signal: controller.signal, credentials: 'same-origin', redirect: 'error',
          })
          if (stopped || epoch !== generation) break
          if (response.status === 401) {
            noteUnauthorized(response, 'lab-runs', headers.Authorization)
            rememberUnauthorized(headers.Authorization)
            paused = true; transport({ status: 'auth_expired' }); break
          }
          if (response.status === 404) {
            active.forEach(id => { store.upsertRun(id, { state: 'missing' }); store.setTransport(id, { status: 'missing' }) })
            active.clear(); break
          }
          if (response.status === 409) { transport({ status: 'stream_limit' }); delay = 15000 }
          else {
            if (!response.ok || !response.body) throw new Error('Stream unavailable')
            transport({ status: 'connected' })
            reader = response.body.getReader()
            currentReader = reader
            const decoder = new TextDecoder()
            let line = '', lineBytes = [], frameBytes = 0, type = 'message', data = [], frameId = '', pendingCR = false
            const frame = async () => {
              if (!data.length) { type = 'message'; frameId = ''; frameBytes = 0; return }
              const value = JSON.parse(data.join('\n')), kind = type
              // The data envelope is authoritative; an id must agree with it.
              if (kind === 'run' && frameId && frameId !== `${value.run_id}:${value.seq}`) throw new Error('Invalid event cursor')
              type = 'message'; data = []; frameId = ''; frameBytes = 0; failures = 0
              if (kind === 'run' && active.has(value.run_id)) {
                store.apply(value.run_id, value)
                if (terminalState(store.getRun(value.run_id)?.state) || (!value.block_id && terminalState(value.kind))) {
                  if (store.getRun(value.run_id)) store.setTransport(value.run_id, { status: 'finished' })
                  active.delete(value.run_id)
                  immediate = true
                }
              } else if (kind === 'health') transport({ lagMs: value.lag_ms, lastEventAt: now() })
              else if (kind === 'gap' && active.has(value.run_id)) {
                store.addGap(value.run_id, { from: value.from, to: value.to, recovered: false })
                await snapshot(value.run_id, controller.signal)
                immediate = true
              } else if (kind === 'close') {
                if (['max_age', 'drain'].includes(value.reason)) immediate = true
                else if (['binding_changed', 'binding_unavailable'].includes(value.reason)) {
                  paused = true; transport({ status: value.reason })
                } else if (value.reason === 'run_missing' && active.has(value.run_id)) {
                  store.upsertRun(value.run_id, { state: 'missing' }); store.setTransport(value.run_id, { status: 'missing' }); active.delete(value.run_id)
                  immediate = true
                }
              }
            }
            const consumeLine = async () => {
              line = decoder.decode(Uint8Array.from(lineBytes))
              lineBytes = []
              if (!line) await frame()
              else if (!line.startsWith(':')) {
                const colon = line.indexOf(':'), name = colon < 0 ? line : line.slice(0, colon)
                let value = colon < 0 ? '' : line.slice(colon + 1)
                if (value.startsWith(' ')) value = value.slice(1)
                if (name === 'event') type = value
                if (name === 'data') data.push(value)
                if (name === 'id') frameId = value
              }
              line = ''
            }
            while (!stopped && !paused && active.size && !immediate) {
              const chunk = await reader.read()
              if (stopped || controller.signal.aborted) break
              if (chunk.done) { if (pendingCR) await consumeLine(); break }
              // Delay CR line splitting so a following LF is counted in the
              // same frame, even when the pair straddles chunks.
              for (const byte of chunk.value) {
                if (pendingCR && byte !== 10) { pendingCR = false; await consumeLine() }
                if (stopped || paused || !active.size || immediate) break
                frameBytes += 1
                if (frameBytes > 256 * 1024) { transport({ status: 'protocol_error' }); throw new Error('Frame too large') }
                if (byte === 13) pendingCR = true
                else if (byte === 10) { pendingCR = false; await consumeLine() }
                else lineBytes.push(byte)
                if (stopped || paused || !active.size || immediate) break
              }
            }
          }
        } catch {
          if (!stopped && !paused) transport({ status: 'reconnecting' })
        } finally {
          controller.abort()
          if (reader) { try { await reader.cancel() } catch { /* disconnected */ } reader.releaseLock() }
          currentReader = undefined
        }
        if (stopped || paused || !active.size || epoch !== generation) break
        if (!immediate) {
          const base = Math.min(30000, 1000 * 2 ** Math.min(failures++, 5))
          waiting = new AbortController()
          await sleep(delay ?? base * (1 + .2 * random()), waiting.signal)
        }
      }
    } finally {
      running = false
      if (!active.size) signal?.removeEventListener('abort', stop)
      if (restartRequested && !stopped && active.size) { restartRequested = false; paused = false; void loop(++generation) }
    }
  }
  function stop() {
    stopped = true; generation += 1; controller?.abort(); waiting?.abort()
    void currentReader?.cancel().catch(() => {})
    transport({ status: 'stopped' }); signal?.removeEventListener('abort', stop)
  }
  const handle = { stop, retry() {
    if (stopped || signal?.aborted || !active.size) return
    authExpired = false
    if (running) { if (paused) restartRequested = true; return }
    paused = false; void loop(++generation)
  }, retryIfAuthChanged() {
    if (stopped || signal?.aborted || !active.size || !paused || !authExpired
      || authHeaders().Authorization === expiredAuthorization) return false
    handle.retry()
    return true
  } }
  if (signal?.aborted) stop()
  else { signal?.addEventListener('abort', stop, { once: true }); void loop(generation) }
  return handle
}
