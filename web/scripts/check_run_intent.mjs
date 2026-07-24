import { authorizeRunIntent, createRunIntent } from '../src/runIntent.js'

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

const writeTool = {
  name: 'delete-marked-panel',
  version: '1.0.0',
  capabilities: ['drawing.read', 'drawing.write'],
}
const intent = createRunIntent(writeTool, { handle: '9462' }, 'intent-1')

assert(Object.isFrozen(intent), 'run intent must be immutable')
assert(Object.isFrozen(intent.params), 'run intent params must be immutable')
assert(
  authorizeRunIntent(intent, { id: 'intent-1', tool: writeTool, params: { handle: '9462' } }).ok,
  'the exact displayed intent must authorize',
)
assert(
  !authorizeRunIntent(intent, { id: 'intent-1', tool: writeTool, params: { handle: 'other' } }).ok,
  'changed parameters must fail closed',
)
assert(
  !authorizeRunIntent(intent, { id: 'stale', tool: writeTool, params: { handle: '9462' } }).ok,
  'a stale intent id must fail closed',
)
assert(
  !authorizeRunIntent(intent, {
    id: 'intent-1',
    tool: { ...writeTool, name: 'different-tool' },
    params: { handle: '9462' },
  }).ok,
  'a different tool must fail closed',
)

console.log('RUN_INTENT_OK')
