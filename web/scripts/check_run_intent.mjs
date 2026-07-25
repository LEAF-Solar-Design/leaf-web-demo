import { readFileSync } from 'node:fs'
import {
  confirmRunIntent,
  createCatalogToolSnapshot,
  createRunIntentState,
  stageRunIntent,
} from '../src/runIntent.js'

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

const tool = {
  name: 'delete-marked-panel',
  version: '1.0.0',
  catalog_digest: `sha256:${'a'.repeat(64)}`,
  capabilities: ['drawing.read', 'drawing.write'],
  engine_op: 'delete_marked_panel',
  params: { type: 'object', properties: { handle: { type: 'string' } } },
}
const context = {
  tenantId: 'tenant-1', orgId: 'org-1', projectId: 'project-1',
  drawingId: 'version-1', drawingArtifactId: 'drawing-1', drawingVersion: null,
}
const staged = stageRunIntent(createRunIntentState('session-1'), {
  intentId: 'intent-1', toolName: tool.name, params: { handle: '9462' },
  context, toolSnapshot: createCatalogToolSnapshot(tool), createdAt: 1000,
})
const request = {
  intentId: staged.intent.intentId,
  sessionId: staged.intent.sessionId,
  toolName: tool.name,
  params: staged.intent.params,
  context: staged.intent.context,
  toolSnapshot: staged.intent.toolSnapshot,
}

assert(confirmRunIntent(staged.state, request, { now: 1100 }).ok,
  'the exact server-catalog intent must authorize')
for (const [label, change] of [
  ['params', { params: { handle: 'other' } }],
  ['session', { sessionId: 'session-2' }],
  ['context', { context: { ...context, drawingId: 'version-2' } }],
  ['catalog', { toolSnapshot: createCatalogToolSnapshot({ ...tool, catalog_digest: `sha256:${'b'.repeat(64)}` }) }],
]) {
  const result = confirmRunIntent(staged.state, { ...request, ...change }, { now: 1100 })
  assert(!result.ok, `${label} drift must fail closed`)
}
assert(!confirmRunIntent(staged.state, request, { now: 1000 + 5 * 60 * 1000 + 1 }).ok,
  'an expired confirmation must fail closed')

const app = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8')
const catalogController = readFileSync(
  new URL('../src/controllers/catalog/createCatalogController.js', import.meta.url),
  'utf8',
)
const routePanel = readFileSync(new URL('../src/components/RoutePanel.jsx', import.meta.url), 'utf8')
assert(app.includes('const armDecision = useCallback((decision) =>'),
  'all route decisions must cross the shared intent staging seam')
assert(app.includes('commitDecision: (decision) => catalogUiRef.current.armDecision?.(decision)'),
  'the catalog controller is not connected to the shared intent seam')
assert(catalogController.includes('commitDecision(decision)'),
  'NL routes do not use the shared intent seam')
assert(catalogController.includes('slash.decision ? commitDecision(slash.decision) : undefined'),
  'slash routes do not use the shared intent seam')
assert(app.includes('runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)'),
  'route dismissal does not invalidate the active intent')
assert(!app.includes('authorizeRunIntent') && !app.includes('createRunIntent('),
  'the legacy client-only intent implementation was reintroduced')
assert(!routePanel.includes('onRun(toolObj, params)'),
  'RoutePanel still bypasses confirmation for a low-confidence best guess')
assert(routePanel.includes('if (!running && !locked && !entBlocked) requestRun()'),
  'RoutePanel best guesses do not use the guarded requestRun seam')
assert(!app.includes('await onRun(toolObj, r.params || {})'),
  'the guided tour still bypasses the guarded intent seam')
assert(app.includes("onRequestCatalogRun(toolObj, r.params || {}, 'Guided tour selection."),
  'the guided tour does not stage the PR107 intent contract')

console.log('RUN_INTENT_OK')
