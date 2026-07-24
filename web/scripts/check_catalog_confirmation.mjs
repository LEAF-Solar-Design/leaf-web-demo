import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runMock } from '../src/mock/mockEngine.js'
import * as versions from '../src/mock/mockVersions.js'
import {
  confirmRunIntent, createCatalogToolSnapshot, createRunIntentState, dismissRunIntent, stageRunIntent,
} from '../src/runIntent.js'

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')
const intake = JSON.parse(readFileSync(join(root, 'public', 'sample.intake.json'), 'utf8'))
const registry = JSON.parse(readFileSync(join(root, 'src', 'mock', 'registry.json'), 'utf8'))
const writeTool = registry.tools.find((tool) => tool.name === 'delete-marked-panel')
const readTool = registry.tools.find((tool) => !(tool.capabilities || []).includes('drawing.write'))
const target = intake.polylines.find((polyline) => polyline.layer === 'Panels')?.handle
assert(writeTool && readTool && target, 'integration fixtures are incomplete')

versions.seedBase(intake)
const baselineCount = versions.headIntake().polylines.length
let state = createRunIntentState('catalog-session')
let executorCalls = 0

function confirmAndExecute(intent, tool, params, now) {
  const result = confirmRunIntent(state, {
    intentId: intent.intentId,
    sessionId: intent.sessionId,
    toolName: tool.name,
    params,
    context: intent.context,
    toolSnapshot: createCatalogToolSnapshot(tool),
  }, { now })
  state = result.state
  if (!result.ok) return result
  executorCalls += 1
  const envelope = runMock(tool, result.execution.params, versions.headIntake())
  if (envelope.ok && envelope.result?.new_version) versions.applyDelete(envelope.result.removed)
  return result
}

let request = stageRunIntent(state, {
  intentId: 'write-escape', toolName: writeTool.name, params: { handle: target }, createdAt: 1000,
  context: { tenantId: 'demo-tenant', orgId: null, projectId: null, drawingId: 'demo', drawingVersion: 1 },
  toolSnapshot: createCatalogToolSnapshot(writeTool),
})
state = request.state
assert(versions.list().versions.length === 1, 'catalog click created a version before confirmation')
assert(versions.headIntake().polylines.length === baselineCount, 'catalog click changed panel count')
state = dismissRunIntent(state, request.intent.intentId)
const escaped = confirmAndExecute(request.intent, writeTool, { handle: target }, 1100)
assert(!escaped.ok && executorCalls === 0, 'Escape reached the executor')
assert(versions.list().versions.length === 1, 'Escape created a version')

request = stageRunIntent(state, {
  intentId: 'write-confirm', toolName: writeTool.name, params: { handle: target }, createdAt: 1200,
  context: { tenantId: 'demo-tenant', orgId: null, projectId: null, drawingId: 'demo', drawingVersion: 1 },
  toolSnapshot: createCatalogToolSnapshot(writeTool),
})
state = request.state
const confirmed = confirmAndExecute(request.intent, writeTool, { handle: target }, 1300)
assert(confirmed.ok && executorCalls === 1, 'confirmed write did not execute exactly once')
assert(versions.list().versions.length === 2, 'confirmed write did not create exactly one version')
assert(versions.headIntake().polylines.length === baselineCount - 1, 'confirmed write changed the wrong panel count')

const doubleClick = confirmAndExecute(request.intent, writeTool, { handle: target }, 1301)
assert(!doubleClick.ok && doubleClick.code === 'replayed', 'double click did not fail as replay')
assert(executorCalls === 1 && versions.list().versions.length === 2, 'double click reached the executor')

const undone = versions.undo()
assert(undone.head === 1, 'Undo did not restore the base version')
assert(versions.headIntake().polylines.length === baselineCount, 'Undo did not restore baseline panels')

const retry = stageRunIntent(state, {
  intentId: 'write-retry', toolName: writeTool.name, params: { handle: target }, createdAt: 1400,
  context: { tenantId: 'demo-tenant', orgId: null, projectId: null, drawingId: 'demo', drawingVersion: 1 },
  toolSnapshot: createCatalogToolSnapshot(writeTool),
})
state = retry.state
assert(retry.intent.intentId !== request.intent.intentId, 'retry reused the old intent')
assert(executorCalls === 1 && versions.headIntake().polylines.length === baselineCount, 'retry ran without confirmation')
state = dismissRunIntent(state, retry.intent.intentId)

const readRequest = stageRunIntent(state, {
  intentId: 'read-confirm', toolName: readTool.name, params: {}, createdAt: 1500,
  context: { tenantId: 'demo-tenant', orgId: null, projectId: null, drawingId: 'demo', drawingVersion: 1 },
  toolSnapshot: createCatalogToolSnapshot(readTool),
})
state = readRequest.state
const readConfirmed = confirmAndExecute(readRequest.intent, readTool, {}, 1600)
assert(readConfirmed.ok && executorCalls === 2, 'read tool did not follow the intent path')
assert(versions.list().versions.length === 2, 'read tool created a drawing version')

const toolsPanel = readFileSync(join(root, 'src', 'components', 'ToolsPanel.jsx'), 'utf8')
const routePanel = readFileSync(join(root, 'src', 'components', 'RoutePanel.jsx'), 'utf8')
const app = readFileSync(join(root, 'src', 'App.jsx'), 'utf8')
const api = readFileSync(join(root, 'src', 'api.js'), 'utf8')
assert(!toolsPanel.includes('onClick={() => onRun('), 'ToolsPanel still calls the executor directly')
assert(toolsPanel.includes('onRequestRun(t, params)'), 'ToolsPanel does not create an intent request')
assert(routePanel.includes('onConfirmIntent(route.runIntent'), 'RoutePanel does not confirm catalog intents')
assert(app.includes('onRequestRun={onRequestCatalogRun}'), 'App does not wire catalog requests to intents')
assert(app.includes('idempotencyKey: confirmed.execution.intentId'), 'confirmed intent ID is not sent to the API seam')
assert(app.includes('runToolAsync(tool, merged, executionContext.drawingId'), 'confirmed drawing is not used for execution')
assert(app.includes('dwgVersion: executionContext.drawingVersion'), 'confirmed drawing version is not used for execution')
assert(api.includes("linkHeaders['Idempotency-Key'] = opts.idempotencyKey"), 'run submission omits its idempotency key')

console.log('CATALOG_CONFIRMATION_OK')
