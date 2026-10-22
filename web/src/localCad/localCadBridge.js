import { bridgeToIntake } from './bridgeIntake.js'

export function createLocalCadBridge({ fetchImpl = globalThis.fetch, ports = [8126], intervalMs = 400,
  maxBackoffMs = 5000, onUpdate, onStatus, setTimeoutImpl = setTimeout, clearTimeoutImpl = clearTimeout,
  loadTextGlyphs = () => import('./bridgeText.js') } = {}) {
  let running = false
  let generation = 0
  let timer = null
  let request = null
  let rev = 0
  let epoch = ''
  let delay = intervalMs
  let textGlyphs
  let textPromise = null
  let textError = null
  let textRetryAt = 0
  const ensureTextGlyphs = () => {
    if (textGlyphs || Date.now() < textRetryAt) return textPromise
    if (!textPromise) {
      textPromise = Promise.resolve().then(() => loadTextGlyphs()).then(module => {
        if (typeof module.textGlyphs !== 'function') throw new Error('Invalid bridge text helper')
        textGlyphs = module.textGlyphs
        textError = null
      }).catch(failure => {
        textError = String(failure?.message || failure)
        textRetryAt = Date.now() + 30000
      }).finally(() => { textPromise = null })
    }
    return textPromise
  }
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
      const controller = new AbortController()
      request = controller
      const deadline = setTimeoutImpl(() => controller.abort(new Error('Bridge request timed out')), 4000)
      let abortListener
      try {
        const candidate = await Promise.race([
          (async () => {
            const response = await fetchImpl(`http://127.0.0.1:${port}/api/live/intake?since=${encodeURIComponent(rev)}&epoch=${encodeURIComponent(epoch)}`,
              { method: 'GET', cache: 'no-store', signal: controller.signal })
            if (response.ok === false) throw new Error(`Bridge HTTP ${response.status}`)
            return response.json()
          })(),
          new Promise((_, reject) => {
            abortListener = () => reject(controller.signal.reason || new Error('Bridge request aborted'))
            controller.signal.addEventListener('abort', abortListener, { once: true })
            if (controller.signal.aborted) abortListener()
          }),
        ])
        if (!current()) return
        if (!Number.isFinite(candidate?.rev) || candidate.rev < 0 || typeof candidate.epoch !== 'string'
          || typeof candidate.status !== 'string'
          || (candidate.unchanged != null && typeof candidate.unchanged !== 'boolean')
          || (candidate.drawing != null && typeof candidate.drawing !== 'string')
          || (candidate.error != null && typeof candidate.error !== 'string')
          || (candidate.change != null && (typeof candidate.change !== 'object' || Array.isArray(candidate.change)))
          || (candidate.change?.summary != null && typeof candidate.change.summary !== 'string')
          || (candidate.intake != null && (typeof candidate.intake !== 'object' || Array.isArray(candidate.intake)))) {
          throw new Error('Invalid bridge response')
        }
        payload = candidate
        break
      } catch (failure) {
        if (!current()) return
        error = String(failure?.message || failure)
      } finally {
        clearTimeoutImpl(deadline)
        controller.signal.removeEventListener('abort', abortListener)
        if (request === controller) request = null
      }
    }
    if (!current()) return
    if (payload) {
      const restarted = payload.epoch !== epoch
      if (!restarted && payload.rev < rev) {
        timer = setTimeoutImpl(() => { timer = null; void poll(token) }, intervalMs)
        return
      }
      let intake
      let textLoadError = null
      try {
        if ((!payload.unchanged || restarted) && payload.intake) {
          if (Array.isArray(payload.intake.texts) && payload.intake.texts.length) {
            await ensureTextGlyphs()
            if (!current()) return
            textLoadError = textError
            if (textLoadError) report({ ...state, error: textLoadError })
            if (!current()) return
          }
          intake = bridgeToIntake(payload.intake, { textGlyphs })
        }
      } catch (failure) {
        report({ ...state, error: String(failure?.message || failure) })
        if (current()) timer = setTimeoutImpl(() => { timer = null; void poll(token) }, delay)
        return
      }
      rev = payload.rev
      epoch = payload.epoch
      const summary = payload.change?.summary ?? (restarted || payload.drawing !== state.drawing ? null : state.summary)
      const connected = payload.status === 'connected' && !!payload.drawing
      report({ status: connected ? 'connected' : 'waiting', drawing: payload.drawing ?? null,
        summary, error: textLoadError ?? payload.error ?? null })
      if (!current()) return
      if (intake) {
        onUpdate?.({ intake, rev, epoch, drawing: payload.drawing,
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
      if (running) return
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
      request?.abort()
      if (timer !== null) clearTimeoutImpl(timer)
      timer = null
      report({ status: 'stopped', drawing: null, summary: null, error: null })
    },
    snapshot() { return { ...state, running, rev, epoch } },
  }
}
