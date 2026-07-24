import {
  confirmRunIntent,
  createRunIntentState,
  dismissRunIntent,
  normalizeRunParams,
  stageRunIntent,
} from '../src/runIntent.js'

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

function staged(state, overrides = {}) {
  return stageRunIntent(state, {
    intentId: overrides.intentId || 'intent-1',
    toolName: overrides.toolName || 'delete-marked-panel',
    params: overrides.params || { handle: 'A1', nested: { z: 2, a: 1 } },
    createdAt: overrides.createdAt ?? 1000,
  })
}

const normalized = normalizeRunParams({ z: 1, a: { y: 2, x: -0 }, gone: undefined })
assert(JSON.stringify(normalized) === '{"a":{"x":0,"y":2},"z":1}', 'params were not normalized')
assert(Object.isFrozen(normalized) && Object.isFrozen(normalized.a), 'normalized params are mutable')

const base = createRunIntentState('session-a')
const first = staged(base)
assert(Object.isFrozen(first.intent) && Object.isFrozen(first.intent.params.nested), 'intent is mutable')

const exact = confirmRunIntent(first.state, {
  intentId: 'intent-1', sessionId: 'session-a', toolName: 'delete-marked-panel',
  params: { nested: { a: 1, z: 2 }, handle: 'A1' },
}, { now: 1100 })
assert(exact.ok, 'exact confirmation was denied')
assert(exact.execution.toolName === 'delete-marked-panel', 'confirmed the wrong tool')

const replay = confirmRunIntent(exact.state, {
  intentId: 'intent-1', sessionId: 'session-a', toolName: 'delete-marked-panel',
  params: { handle: 'A1', nested: { a: 1, z: 2 } },
}, { now: 1101 })
assert(!replay.ok && replay.code === 'replayed', 'replay or double click did not fail closed')

for (const [label, request, options, code] of [
  ['changed tool', { intentId: 'x', sessionId: 'session-a', toolName: 'other', params: { handle: 'A1' } }, { now: 1100 }, 'changed_tool'],
  ['changed params', { intentId: 'x', sessionId: 'session-a', toolName: 'delete-marked-panel', params: { handle: 'B2' } }, { now: 1100 }, 'changed_params'],
  ['cross session', { intentId: 'x', sessionId: 'session-b', toolName: 'delete-marked-panel', params: { handle: 'A1' } }, { now: 1100 }, 'cross_session'],
  ['stale', { intentId: 'x', sessionId: 'session-a', toolName: 'delete-marked-panel', params: { handle: 'A1' } }, { now: 7001, maxAgeMs: 5000 }, 'stale'],
]) {
  const candidate = staged(base, { intentId: 'x', params: { handle: 'A1' } })
  const result = confirmRunIntent(candidate.state, request, options)
  assert(!result.ok && result.code === code, `${label} did not fail closed`)
}

const replaced = staged(first.state, { intentId: 'intent-2' })
const old = confirmRunIntent(replaced.state, {
  intentId: 'intent-1', sessionId: 'session-a', toolName: 'delete-marked-panel',
  params: first.intent.params,
}, { now: 1100 })
assert(!old.ok && old.code === 'changed_intent', 'replaced intent remained usable')

const escaped = dismissRunIntent(first.state, first.intent.intentId)
const afterEscape = confirmRunIntent(escaped, {
  intentId: 'intent-1', sessionId: 'session-a', toolName: 'delete-marked-panel',
  params: first.intent.params,
}, { now: 1100 })
assert(!afterEscape.ok && afterEscape.code === 'missing', 'dismissed intent remained usable')

console.log('RUN_INTENTS_OK')
