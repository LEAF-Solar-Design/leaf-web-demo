import {
  confirmRunIntent,
  createCatalogToolSnapshot,
  createRunIntentState,
  dismissRunIntent,
  normalizeRunParams,
  stageRunIntent,
} from '../src/runIntent.js'

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

function staged(state, overrides = {}) {
  const tool = overrides.tool || {
    name: overrides.toolName || 'delete-marked-panel',
    version: '1.0.0',
    capabilities: ['drawing.write'],
    engine_op: 'delete_marked_panel',
  }
  return stageRunIntent(state, {
    intentId: overrides.intentId || 'intent-1',
    toolName: tool.name,
    params: overrides.params || { handle: 'A1', nested: { z: 2, a: 1 } },
    createdAt: overrides.createdAt ?? 1000,
    context: overrides.context || {
      tenantId: 'tenant-a', orgId: 'org-a', projectId: 'project-a',
      drawingId: 'drawing-a', drawingVersion: 3,
    },
    toolSnapshot: createCatalogToolSnapshot(tool),
  })
}

function requestFor(intent, overrides = {}) {
  return {
    intentId: intent.intentId,
    sessionId: intent.sessionId,
    toolName: overrides.toolName || intent.toolName,
    params: overrides.params || intent.params,
    context: overrides.context || intent.context,
    toolSnapshot: overrides.toolSnapshot || intent.toolSnapshot,
  }
}

const normalized = normalizeRunParams({ z: 1, a: { y: 2, x: -0 }, gone: undefined })
assert(JSON.stringify(normalized) === '{"a":{"x":0,"y":2},"z":1}', 'params were not normalized')
assert(Object.isFrozen(normalized) && Object.isFrozen(normalized.a), 'normalized params are mutable')

const base = createRunIntentState('session-a')
const first = staged(base)
assert(Object.isFrozen(first.intent) && Object.isFrozen(first.intent.params.nested), 'intent is mutable')

const exact = confirmRunIntent(first.state, requestFor(first.intent, {
  params: { nested: { a: 1, z: 2 }, handle: 'A1' },
}), { now: 1100 })
assert(exact.ok, 'exact confirmation was denied')
assert(exact.execution.toolName === 'delete-marked-panel', 'confirmed the wrong tool')

const replay = confirmRunIntent(exact.state, requestFor(first.intent), { now: 1101 })
assert(!replay.ok && replay.code === 'replayed', 'replay or double click did not fail closed')

for (const [label, request, options, code] of [
  ['changed tool', { toolName: 'other' }, { now: 1100 }, 'changed_tool'],
  ['changed params', { params: { handle: 'B2' } }, { now: 1100 }, 'changed_params'],
  ['cross session', { sessionId: 'session-b' }, { now: 1100 }, 'cross_session'],
  ['stale', {}, { now: 7001, maxAgeMs: 5000 }, 'stale'],
]) {
  const candidate = staged(base, { intentId: 'x', params: { handle: 'A1' } })
  const result = confirmRunIntent(candidate.state, { ...requestFor(candidate.intent), ...request }, options)
  assert(!result.ok && result.code === code, `${label} did not fail closed`)
}

for (const [field, value] of [
  ['tenantId', 'tenant-b'], ['orgId', 'org-b'], ['projectId', 'project-b'],
  ['drawingId', 'drawing-b'], ['drawingVersion', 4],
]) {
  const contextCandidate = staged(base, { intentId: `context-drift-${field}` })
  const changedContext = confirmRunIntent(contextCandidate.state, requestFor(contextCandidate.intent, {
    context: { ...contextCandidate.intent.context, [field]: value },
  }), { now: 1100 })
  assert(!changedContext.ok && changedContext.code === 'changed_context', `${field} drift did not fail closed`)
}

const originalTool = {
  name: 'delete-marked-panel', version: '1.0.0', capabilities: ['drawing.write'],
  engine_op: 'delete_marked_panel',
}
const replacedTool = { ...originalTool, version: '1.0.1', engine_op: 'delete_other_panel' }
const toolCandidate = staged(base, { intentId: 'tool-drift', tool: originalTool })
const changedSnapshot = confirmRunIntent(toolCandidate.state, requestFor(toolCandidate.intent, {
  toolSnapshot: createCatalogToolSnapshot(replacedTool),
}), { now: 1100 })
assert(!changedSnapshot.ok && changedSnapshot.code === 'changed_tool_snapshot', 'same-name tool drift did not fail closed')

const replaced = staged(first.state, { intentId: 'intent-2' })
const old = confirmRunIntent(replaced.state, requestFor(first.intent), { now: 1100 })
assert(!old.ok && old.code === 'changed_intent', 'replaced intent remained usable')

const escaped = dismissRunIntent(first.state, first.intent.intentId)
const afterEscape = confirmRunIntent(escaped, requestFor(first.intent), { now: 1100 })
assert(!afterEscape.ok && afterEscape.code === 'missing', 'dismissed intent remained usable')

console.log('RUN_INTENTS_OK')
