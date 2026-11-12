import assert from 'node:assert/strict'
import { test } from 'node:test'
import { startStack, QueuedError } from './stack.mjs'

test('identity requires PostgreSQL and skips the solver preflight', async () => {
  for (const postgres of [undefined, false]) {
    await assert.rejects(startStack({ identity: true, postgres }), { name: 'TypeError', message: 'identity requires postgres' })
  }
  let solverCalls = 0
  let binaryCalls = 0
  let admissions = 0
  await assert.rejects(startStack({ identity: true, postgres: 'ephemeral',
    postgresServices: {
      resolveBinaries: async () => { binaryCalls++ },
      solver: async () => { solverCalls++; throw new Error('solver must not be needed') },
    },
    admission: () => { admissions++; throw new QueuedError('identity preflight complete') },
  }), { code: 'QUEUED', message: 'identity preflight complete' })
  assert.equal(binaryCalls, 1)
  assert.equal(admissions, 1)
  assert.equal(solverCalls, 0)
})
