import assert from 'node:assert/strict'
import test from 'node:test'

import { createCatalogController } from '../../../src/controllers/catalog/createCatalogController.js'
import {
  entitlementAllows,
  runnableCatalogTools,
} from '../../../src/controllers/catalog/catalogRouting.js'

const readTool = { name: 'count-by-layer', description: 'Count panels', capabilities: ['drawing.read'] }
const writeTool = { name: 'delete-marked-panel', description: 'Delete a panel', capabilities: ['drawing.write'] }

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function controllerWith(overrides = {}) {
  return createCatalogController({
    services: {
      getTools: async () => [readTool, writeTool],
      getCapabilities: async () => ({
        source: 'endpoint',
        families: [{ family_id: 'measurement', capabilities: [readTool] }],
      }),
      routePrompt: async () => ({ lane: 'run', tool: readTool.name, confidence: 0.9, alternatives: [] }),
      ...overrides.services,
    },
    adapters: {
      humanizeError: (error) => `calm: ${error.message}`,
      previewRoute: () => ({ lane: 'run' }),
      ...overrides.adapters,
    },
    context: overrides.context,
  })
}

test('unknown entitlements allow tools while a denied write policy filters only write tools', () => {
  assert.equal(entitlementAllows(null, 'run_write'), true)
  assert.deepEqual(runnableCatalogTools([readTool, writeTool], null), [readTool, writeTool])
  assert.deepEqual(
    runnableCatalogTools([readTool, writeTool], { entitlements: { run_write: false } }),
    [readTool],
  )
})

test('slash routing bypasses NL routing and preserves exact and typo behavior', async () => {
  let routeCalls = 0
  const controller = controllerWith({
    services: { routePrompt: async () => { routeCalls += 1; throw new Error('must not run') } },
  })
  await controller.actions.loadTools()

  const exact = await controller.actions.dispatch('/COUNT-BY-LAYER')
  assert.equal(exact.tool, 'count-by-layer')
  assert.equal(exact.confidence, 1)
  assert.equal(exact.slash, true)

  const typo = await controller.actions.dispatch('/count-by-layre')
  assert.equal(typo.confidence, 0)
  assert.deepEqual(typo.alternatives, [{ tool: 'count-by-layer', description: 'Count panels' }])
  assert.equal(routeCalls, 0)
})

test('tool retry clears the error, increments its key, and retains the last good list on failure', async () => {
  let fail = false
  const controller = controllerWith({
    services: {
      getTools: async () => {
        if (fail) throw new Error('offline')
        return [readTool]
      },
    },
  })
  await controller.actions.loadTools()
  fail = true
  await controller.actions.retryTools()

  const state = controller.getState()
  assert.deepEqual(state.tools, [readTool])
  assert.equal(state.toolsRetryKey, 1)
  assert.equal(state.toolsError, 'calm: offline')
})

test('published tool replacement is additive and keeps the tool runnable immediately', async () => {
  const controller = controllerWith()
  await controller.actions.loadTools()
  const replacement = { ...readTool, description: 'Updated count' }
  controller.actions.upsertTool(replacement)

  assert.deepEqual(controller.getState().tools, [writeTool, replacement])
  assert.equal(controller.getState().runnableTools.at(-1).description, 'Updated count')
})

test('catalog failure clears grouped families, reports calm error, and raises live 401 auth state', async () => {
  let authRequired = 0
  const controller = controllerWith({
    services: {
      getCapabilities: async () => {
        const error = new Error('GET capabilities -> 401')
        error.status = 401
        throw error
      },
    },
    adapters: { onAuthRequired: () => { authRequired += 1 } },
    context: { mock: false },
  })
  await controller.actions.loadCatalog()

  assert.deepEqual(controller.getState().catalog, { families: [], source: null })
  assert.equal(controller.getState().catalogError, 'calm: GET capabilities -> 401')
  assert.equal(authRequired, 1)
})

test('a stale tool response cannot replace the active mode response', async () => {
  const first = deferred()
  let calls = 0
  const controller = controllerWith({
    services: {
      getTools: async () => {
        calls += 1
        return calls === 1 ? first.promise : [writeTool]
      },
    },
  })

  const oldLoad = controller.actions.loadTools()
  const newLoad = controller.actions.loadTools()
  await newLoad
  first.resolve([readTool])
  await oldLoad

  assert.deepEqual(controller.getState().tools, [writeTool])
})

test('NL routing keeps route failures retryable and prompt edits invalidate stale state', async () => {
  let fail = true
  const controller = controllerWith({
    services: {
      routePrompt: async () => {
        if (fail) throw new Error('router offline')
        return { lane: 'run', tool: readTool.name, confidence: 0.9, alternatives: [] }
      },
    },
  })
  await controller.actions.loadTools()
  controller.actions.setPrompt('count panels')
  assert.equal(await controller.actions.dispatch(), undefined)
  assert.equal(controller.getState().routeError, 'calm: router offline')

  controller.actions.setPrompt('count panels again')
  assert.equal(controller.getState().routeError, null)
  fail = false
  const decision = await controller.actions.dispatch()
  assert.equal(decision.tool, readTool.name)
  assert.equal(controller.getState().route.tool, readTool.name)
})

test('alternative selection removes the chosen row and retains fallback provenance', async () => {
  const controller = controllerWith({
    services: {
      routePrompt: async () => ({
        lane: 'run',
        tool: 'live-only',
        confidence: 0.2,
        alternatives: [{ tool: readTool.name }, { tool: writeTool.name }],
        stub: true,
        stubReason: 'local fallback',
      }),
    },
  })
  controller.actions.setPrompt('unclear request')
  await controller.actions.dispatch()
  const decision = controller.actions.pickAlternative(readTool.name)
  assert.equal(decision.tool, readTool.name)
  assert.equal(decision.confidence, 0.99)
  assert.deepEqual(decision.alternatives, [{ tool: writeTool.name }])
  assert.equal(decision.stub, true)
  assert.equal(decision.stubReason, 'local fallback')
})
