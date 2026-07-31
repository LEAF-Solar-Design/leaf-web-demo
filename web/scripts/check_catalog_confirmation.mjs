import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runMock } from '../src/mock/mockEngine.js'
import * as versions from '../src/mock/mockVersions.js'
import {
  confirmRunIntent, createCatalogRunContext, createCatalogToolSnapshot, createRunIntentState,
  createRunSubmissionRequest, dismissRunIntent, prepareCatalogRunParams, stageRunIntent,
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
assert(app.includes('const latestTools = await getTools(false)'), 'live confirmation does not refresh catalog ground truth')
assert(app.includes('idempotencyKey: confirmed.execution.intentId'), 'confirmed intent ID is not sent to the API seam')
assert(app.includes('runToolAsync(tool, merged, executionContext.drawingId'), 'confirmed drawing is not used for execution')
assert(app.includes('dwgVersion: executionContext.drawingVersion'), 'confirmed drawing version is not used for execution')
assert(api.includes('createRunSubmissionRequest(toolName, params, dwg, opts)'),
  'run submission bypasses the tested request-shape authority')

const canonicalContext = createCatalogRunContext({
  tenantId: 'tenant-canonical',
  orgId: '11111111-1111-4111-8111-111111111111',
  projectId: '22222222-2222-4222-8222-222222222222',
  workspace: {
    project: { project_id: '22222222-2222-4222-8222-222222222222' },
    drawing_versions: [
      { version_id: '33333333-3333-4333-8333-333333333333', seq: 1,
        org_id: '11111111-1111-4111-8111-111111111111', project_id: '22222222-2222-4222-8222-222222222222',
        drawing_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' },
      { version_id: '44444444-4444-4444-8444-444444444444', seq: 99,
        org_id: '11111111-1111-4111-8111-111111111111', project_id: '22222222-2222-4222-8222-222222222222',
        drawing_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' },
    ],
  },
  selectedVersionId: '33333333-3333-4333-8333-333333333333',
  drawingState: { drawing_id: 'legacy-drawing', version: 7 },
  fallbackDrawingId: 'rooftop_demo',
})
assert(canonicalContext.drawingId === '33333333-3333-4333-8333-333333333333',
  'canonical context did not preserve the explicit version selection in a two-drawing project')
assert(canonicalContext.drawingVersion === null,
  'canonical context retained a legacy integer drawing version')
assert(createCatalogRunContext({
  tenantId: canonicalContext.tenantId,
  orgId: canonicalContext.orgId,
  projectId: canonicalContext.projectId,
  workspace: {
    project: { project_id: canonicalContext.projectId },
    drawing_versions: [{
      version_id: canonicalContext.drawingId, seq: 1,
      org_id: canonicalContext.orgId, project_id: canonicalContext.projectId,
      drawing_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    }],
  },
  fallbackDrawingId: 'rooftop_demo',
}) === null, 'canonical context chose a version without an explicit selection')
assert(createCatalogRunContext({
  tenantId: canonicalContext.tenantId,
  orgId: canonicalContext.orgId,
  projectId: canonicalContext.projectId,
  selectedVersionId: canonicalContext.drawingId,
  workspace: {
    project: { project_id: canonicalContext.projectId },
    drawing_versions: [{
      version_id: canonicalContext.drawingId, seq: 1,
      org_id: '99999999-9999-4999-8999-999999999999',
      project_id: canonicalContext.projectId,
      drawing_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    }],
  },
  fallbackDrawingId: 'rooftop_demo',
}) === null, 'canonical context accepted a cross-org selected version')
assert(createCatalogRunContext({
  tenantId: canonicalContext.tenantId,
  orgId: canonicalContext.orgId,
  projectId: canonicalContext.projectId,
  workspace: { project: { project_id: canonicalContext.projectId }, drawing_versions: [] },
  fallbackDrawingId: 'rooftop_demo',
}) === null, 'missing canonical drawing version did not fail closed')
assert(createCatalogRunContext({
  tenantId: canonicalContext.tenantId,
  orgId: canonicalContext.orgId,
  projectId: canonicalContext.projectId,
  workspace: {
    project: { project_id: '55555555-5555-4555-8555-555555555555' },
    drawing_versions: [{ version_id: canonicalContext.drawingId, seq: 2 }],
  },
  fallbackDrawingId: 'rooftop_demo',
}) === null, 'stale cross-project workspace hydration did not fail closed')

const runRequest = createRunSubmissionRequest('string-autofill-opt', { groups: [] }, canonicalContext.drawingId, {
  orgId: canonicalContext.orgId,
  projectId: canonicalContext.projectId,
  dwgVersion: canonicalContext.drawingVersion ?? undefined,
  idempotencyKey: 'catalog-session:1',
  catalogDigest: `sha256:${'c'.repeat(64)}`,
})
const runBody = runRequest.body
const runHeaders = runRequest.headers
assert(runBody.dwg === '33333333-3333-4333-8333-333333333333',
  'canonical request did not send the immutable version UUID as dwg')
assert(!Object.hasOwn(runBody, 'dwg_version'),
  'canonical request mixed the legacy integer version into its payload')
assert(runHeaders['X-Org-Id'] === canonicalContext.orgId
  && runHeaders['X-Project-Id'] === canonicalContext.projectId,
  'canonical request omitted its confirmed org/project binding')
assert(runHeaders['Idempotency-Key'] === 'catalog-session:1',
  'canonical request omitted the confirmed intent idempotency key')
assert(runBody.catalog_digest === `sha256:${'c'.repeat(64)}`,
  'canonical request omitted the server-issued catalog digest')

const legacyContext = createCatalogRunContext({
  tenantId: 'tenant-legacy',
  drawingState: { drawing_id: 'demo', version: 4, head: 4, latest: 4 },
  fallbackDrawingId: 'demo',
})
const legacyParams = prepareCatalogRunParams(writeTool, { handle: target }, legacyContext)
const legacyRequest = createRunSubmissionRequest(
  writeTool.name,
  legacyParams,
  legacyContext.drawingId,
  { dwgVersion: legacyContext.drawingVersion },
)
assert(legacyRequest.body.dwg === 'demo',
  'legacy /app request did not target the mapped store drawing')
assert(legacyRequest.body.dwg_version === 4,
  'legacy /app request omitted the current drawing head')
assert(legacyRequest.body.params.drawing_id === 'demo',
  'legacy /app write params were not bound to the mapped store drawing')

console.log('CATALOG_CONFIRMATION_OK')
