const DEFAULT_MAX_AGE_MS = 5 * 60 * 1000

function freeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value
  Object.freeze(value)
  for (const child of Object.values(value)) freeze(child)
  return value
}

function normalize(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new TypeError('run intent params must contain finite numbers')
    return Object.is(value, -0) ? 0 : value
  }
  if (Array.isArray(value)) return value.map(normalize)
  if (value && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype) {
    const out = {}
    for (const key of Object.keys(value).sort()) {
      if (value[key] !== undefined) out[key] = normalize(value[key])
    }
    return out
  }
  throw new TypeError('run intent params must be JSON values')
}

export function normalizeRunParams(params) {
  return freeze(normalize(params || {}))
}

export function createRunIntentState(sessionId) {
  if (typeof sessionId !== 'string' || !sessionId) throw new TypeError('sessionId is required')
  return freeze({ sessionId, pending: null, consumedIds: [] })
}

export function stageRunIntent(state, { intentId, toolName, params, createdAt = Date.now() }) {
  if (!state?.sessionId) throw new TypeError('valid run intent state is required')
  if (typeof intentId !== 'string' || !intentId) throw new TypeError('intentId is required')
  if (typeof toolName !== 'string' || !toolName) throw new TypeError('toolName is required')
  if (!Number.isFinite(createdAt)) throw new TypeError('createdAt must be finite')
  if (state.pending?.intentId === intentId || state.consumedIds.includes(intentId)) {
    throw new Error('run intent id must be unique within its session')
  }
  const intent = freeze({
    intentId,
    sessionId: state.sessionId,
    toolName,
    params: normalizeRunParams(params),
    createdAt,
  })
  return freeze({
    state: { ...state, pending: intent },
    intent,
  })
}
export function dismissRunIntent(state, intentId = null) {
  if (!state?.pending) return state
  if (intentId && state.pending.intentId !== intentId) return state
  return freeze({ ...state, pending: null })
}

function denied(state, code) {
  return freeze({ ok: false, code, state: freeze({ ...state, pending: null }) })
}

export function confirmRunIntent(state, request, {
  now = Date.now(),
  maxAgeMs = DEFAULT_MAX_AGE_MS,
} = {}) {
  const id = request?.intentId
  if (id && state?.consumedIds?.includes(id)) return denied(state, 'replayed')
  const intent = state?.pending
  if (!intent) return denied(state, 'missing')
  if (request?.sessionId !== state.sessionId || intent.sessionId !== state.sessionId) {
    return denied(state, 'cross_session')
  }
  if (id !== intent.intentId) return denied(state, 'changed_intent')
  if (!Number.isFinite(now) || now < intent.createdAt || now - intent.createdAt > maxAgeMs) {
    return denied(state, 'stale')
  }
  if (request?.toolName !== intent.toolName) return denied(state, 'changed_tool')
  let params
  try {
    params = normalizeRunParams(request?.params)
  } catch {
    return denied(state, 'changed_params')
  }
  if (JSON.stringify(params) !== JSON.stringify(intent.params)) return denied(state, 'changed_params')

  const execution = freeze({ toolName: intent.toolName, params: intent.params })
  return freeze({
    ok: true,
    state: freeze({
      ...state,
      pending: null,
      consumedIds: [...state.consumedIds, intent.intentId],
    }),
    execution,
  })
}
