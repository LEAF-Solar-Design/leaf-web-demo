import { bridgeToIntake } from './bridgeIntake.js'

export function createLocalCadBridge({ fetchImpl = globalThis.fetch, ports = [8126], intervalMs = 400,
  maxBackoffMs = 5000, onUpdate, onStatus, setTimeoutImpl = setTimeout, clearTimeoutImpl = clearTimeout } = {}) {
  let running = false
  let generation = 0
  let timer = null
  let rev = 0
  let epoch = ''
  let delay = intervalMs
  let state = { status: 'stopped', drawing: null, summary: null, error: null }
  const report = next => {
    if (Object.keys(next).some(key => next[key] !== state[key])) {
      state = next
      onStatus?.({ ...state })
    }
  }
  const poll = async token => {
    const current = () => running && token === generation
    let payload = null
    let error = null
    for (const port of ports) {
      try {
        const response = await fetchImpl(`http://127.0.0.1:${port}/api/live/intake?since=${encodeURIComponent(rev)}&epoch=${encodeURIComponent(epoch)}`,
          { method: 'GET', cache: 'no-store' })
        if (!current()) return
        if (response.ok === false) throw new Error(`Bridge HTTP ${response.status}`)
        const candidate = await response.json()
        if (!current()) return
        if (!Number.isFinite(candidate?.rev) || candidate.epoch == null) throw new Error('Invalid bridge response')
        payload = candidate
        break
      } catch (failure) {
        if (!current()) return
        error = String(failure?.message || failure)
      }
    }
    if (!current()) return
    if (payload) {
      const restarted = payload.epoch !== epoch
      rev = payload.rev
      epoch = payload.epoch
      const summary = payload.change?.summary ?? (restarted || payload.drawing !== state.drawing ? null : state.summary)
      const connected = payload.status === 'connected' && !!payload.drawing
      report({ status: connected ? 'connected' : 'waiting', drawing: payload.drawing ?? null,
        summary, error: payload.error ?? null })
      if (!current()) return
      if ((!payload.unchanged || restarted) && payload.intake) {
        onUpdate?.({ intake: bridgeToIntake(payload.intake), rev, epoch, drawing: payload.drawing,
          summary: payload.change?.summary ?? null, payload })
      }
      delay = connected ? intervalMs : Math.min(maxBackoffMs, delay * 2)
    } else {
      report({ status: 'unreachable', drawing: null, summary: null, error })
      delay = Math.min(maxBackoffMs, delay * 2)
    }
    if (current()) timer = setTimeoutImpl(() => { timer = null; void poll(token) }, delay)
  }
  return {
    start() {
      if (running && state.status !== 'unreachable') return
      if (timer !== null) clearTimeoutImpl(timer)
      timer = null
      running = true
      const token = ++generation
      rev = 0; epoch = ''; delay = intervalMs
      report({ status: 'connecting', drawing: null, summary: null, error: null })
      if (running && token === generation) void poll(token)
    },
    stop() {
      running = false
      generation += 1
      if (timer !== null) clearTimeoutImpl(timer)
      timer = null
      report({ status: 'stopped', drawing: null, summary: null, error: null })
    },
    snapshot() { return { ...state, running, rev, epoch } },
  }
}
