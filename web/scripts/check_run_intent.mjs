import { authorizeRunIntent, createRunIntent } from '../src/runIntent.js'

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

const writeTool = {
  name: 'delete-marked-panel',
  version: '1.0.0',
  capabilities: ['drawing.read', 'drawing.write'],
  engine_op: 'delete_marked_panel',
  params: {
    type: 'object',
    properties: { handle: { type: 'string', default: '' } },
  },
}
const context = {
  mode: 'live', tenant_id: 'tenant-1', org_id: 'org-1', project_id: 'project-1',
  drawing_id: 'demo', drawing_version: 3,
}
const intent = createRunIntent(writeTool, { handle: '9462' }, context, 'intent-1')

assert(Object.isFrozen(intent), 'run intent must be immutable')
assert(Object.isFrozen(intent.params), 'run intent params must be immutable')
assert(
  authorizeRunIntent(intent, {
    id: 'intent-1', tool: writeTool, params: { handle: '9462' }, context,
  }).ok,
  'the exact displayed intent must authorize',
)
assert(
  !authorizeRunIntent(intent, {
    id: 'intent-1', tool: writeTool, params: { handle: 'other' }, context,
  }).ok,
  'changed parameters must fail closed',
)
assert(
  !authorizeRunIntent(intent, {
    id: 'stale', tool: writeTool, params: { handle: '9462' }, context,
  }).ok,
  'a stale intent id must fail closed',
)
assert(
  !authorizeRunIntent(intent, {
    id: 'intent-1',
    tool: { ...writeTool, name: 'different-tool' },
    params: { handle: '9462' },
    context,
  }).ok,
  'a different tool must fail closed',
)

for (const changedTool of [
  { ...writeTool, engine_op: 'count_by_layer' },
  { ...writeTool, params: { type: 'object', properties: {} } },
  { ...writeTool, provenance: { author: 'different-source' } },
]) {
  assert(
    !authorizeRunIntent(intent, {
      id: 'intent-1', tool: changedTool, params: { handle: '9462' }, context,
    }).ok,
    'same-name executable tool substitution must fail closed',
  )
}

for (const changedContext of [
  { ...context, mode: 'mock' },
  { ...context, tenant_id: 'tenant-2' },
  { ...context, org_id: 'org-2' },
  { ...context, project_id: 'project-2' },
  { ...context, drawing_id: 'other' },
  { ...context, drawing_version: 4 },
]) {
  assert(
    !authorizeRunIntent(intent, {
      id: 'intent-1', tool: writeTool, params: { handle: '9462' }, context: changedContext,
    }).ok,
    'workspace or mode drift must fail closed',
  )
}

const authorized = authorizeRunIntent(intent, {
  id: 'intent-1', tool: writeTool, params: { handle: '9462' }, context,
})
assert(authorized.tool === intent.tool, 'execution must use the frozen tool snapshot')
assert(Object.isFrozen(authorized.tool), 'the executed tool snapshot must stay immutable')
assert(
  !authorizeRunIntent(
    intent,
    { id: 'intent-1', tool: writeTool, params: { handle: '9462' }, context },
    { now: intent.createdAt + 5 * 60 * 1000 + 1 },
  ).ok,
  'an expired confirmation must fail closed',
)

console.log('RUN_INTENT_OK')
