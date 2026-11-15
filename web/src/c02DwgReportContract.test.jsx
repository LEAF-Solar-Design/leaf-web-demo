import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { runProjectCatalogTool } from './App.jsx'
import { createSolarProjectClient } from './solar/solarProjectClient.js'
import { createCatalogToolSnapshot } from './runIntent.js'
import ResultPanel from './components/ResultPanel.jsx'

// Unrelated integration panels are not mounted in this contract test.
vi.mock('./integrations/client.js', () => ({}))
vi.mock('./integrations/projectLogRetentionClient.js', () => ({}))

// Synthetic published-tool fixture, not a claim that this tool is published on LIVE.
const ORG = '10000000-0000-4000-8000-000000000001'
const PROJECT = '20000000-0000-4000-8000-000000000001'
const DRAWING = '30000000-0000-4000-8000-000000000001'
const VERSION = '40000000-0000-4000-8000-000000000001'
const JOB = '50000000-0000-4000-8000-000000000001'
const DIGEST = 'sha256:' + 'a'.repeat(64)
const catalog = [{ name: 'layer-bounding-boxes', version: '1.0.0', capabilities: ['drawing.read'],
  catalog_digest: DIGEST, parameters: { units: { type: 'string' }, layer_filter: { type: 'string' } } }]
const tool = createCatalogToolSnapshot(catalog.find(row => row.name === 'layer-bounding-boxes'))
const binding = { kind: 'project', organization_id: ORG, project_id: PROJECT, drawing_id: DRAWING, input_version_id: VERSION }
const context = { orgId: ORG, projectId: PROJECT, drawingId: VERSION }
const projectContext = { organization_id: ORG, project_id: PROJECT, drawing_id: DRAWING, input_version_id: VERSION }
const report = { units: 'mm', layer_count: 2, layers: [
  { name: 'Large roof', min_x: 0, min_y: 0, max_x: 20, max_y: 10, area: 200 },
  { name: 'Small roof', min_x: 30, min_y: 0, max_x: 35, max_y: 4, area: 20 },
] }
const artifactText = JSON.stringify(report)
const envelope = { ok: true, tool: tool.name, version: '1.0.0', project_context: projectContext,
  result: { ...report, files: [{ name: 'geometry-report.json', mime: 'application/json', bytes: artifactText.length, base64: btoa(artifactText) }] },
  error: null, timing_ms: 3, cost: null }
const clients = []
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.dispose()); vi.unstubAllGlobals() })

function lane(data = envelope, status = 200) {
  const fetchImpl = vi.fn(async () => ({ ok: status >= 200 && status < 300, status, json: async () => data }))
  const client = createSolarProjectClient({ fetchImpl, apiBase: 'https://c02.invalid', authHeaders: () => ({}) })
  clients.push(client)
  client.setScope(binding, 'c02-offline-session')
  const run = (changes = {}) => runProjectCatalogTool({ projectSolar: { ready: true, projectClient: client },
    tool: tool.definition, params: { units: 'mm', layer_filter: 'roof', drawing_id: 'stale-standalone-id' },
    context, catalogDigest: tool.catalogDigest, idempotencyKey: 'c02-confirm-report', ...changes })
  return { client, fetchImpl, run }
}

describe('C02 fixture catalog to project-bound DWG report contract', () => {
  it('sends exact canonical drawing/version and confirmed params without checkout, then renders rows and artifact', async () => {
    const test = lane()
    const result = await test.run()
    expect(test.fetchImpl).toHaveBeenCalledTimes(1)
    const [url, request] = test.fetchImpl.mock.calls[0]
    expect(url).toBe('https://c02.invalid/api/run')
    expect(request.method).toBe('POST')
    expect(JSON.parse(request.body)).toEqual({ tool: 'layer-bounding-boxes', dwg: VERSION,
      params: { units: 'mm', layer_filter: 'roof', drawing_id: DRAWING }, catalog_digest: DIGEST })
    expect(request.headers).toMatchObject({ 'X-Org-Id': ORG, 'X-Project-Id': PROJECT,
      'X-Tenant-Id': ORG, 'Idempotency-Key': 'c02-confirm-report' })
    expect(request.headers).not.toHaveProperty('X-Checkout-Capability')
    expect(result).toEqual(envelope)
    expect(test.client.getSnapshot().binding).toEqual(binding)
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:c02-report'), revokeObjectURL: vi.fn() })
    render(<ResultPanel running={false} tool={tool.definition} result={result} />)
    const rows = within(screen.getByRole('table', { name: 'layers' })).getAllByRole('row').slice(1)
    expect(rows.map(row => within(row).getAllByRole('cell')[0].textContent)).toEqual(['Large roof', 'Small roof'])
    expect(screen.getByText('Download geometry-report.json')).toHaveAttribute('download', 'geometry-report.json')
    expect(result.result.files[0].base64).toBe(btoa(artifactText))
  })

  it('retains the outer job identity and handles the eventual report through the real subscription boundary', async () => {
    const test = lane({ job_id: JOB, status: 'submitted' }, 202)
    const onSubmit = vi.fn()
    const onStatus = vi.fn()
    const attach = vi.fn(async () => envelope)
    expect(await test.run({ onSubmit, onStatus, attach })).toEqual(envelope)
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith(JOB)
    expect(onStatus).toHaveBeenCalledWith({ status: 'submitted', progress: 'queued', elapsed_ms: null, job_id: JOB })
    expect(attach).toHaveBeenCalledExactlyOnceWith(JOB, { onStatus, signal: undefined })
  })

  it.each(['counts', 'files-only'])('keeps supplied file downloads for %s results', (shape) => {
    const files = envelope.result.files
    const result = { ...envelope, result: shape === 'counts' ? { counts: { Roof: 2 }, total: 2, files } : { files } }
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:c02-report'), revokeObjectURL: vi.fn() })
    render(<ResultPanel running={false} tool={tool.definition} result={result} />)
    expect(screen.getAllByText('Download geometry-report.json')).toHaveLength(1)
    expect(screen.getByText('Download geometry-report.json')).toHaveAttribute('href', 'blob:c02-report')
    expect(screen.queryByText('Completed with no output.')).toBeNull()
  })

  it('refuses a stale selected version before dispatch and a foreign report after dispatch', async () => {
    const stale = lane()
    expect(await stale.run({ context: { ...context, drawingId: JOB } })).toMatchObject({ ok: false, error: { error_code: 'project_request_required' } })
    expect(stale.fetchImpl).not.toHaveBeenCalled()
    const foreign = lane({ ...envelope, project_context: { ...projectContext, project_id: JOB } })
    expect(await foreign.run()).toMatchObject({ ok: false, version: null, error: { error_code: 'SIP_R7_RESPONSE_INVALID' } })
  })

  it('returns invalid-parameter refusal without turning it into an empty successful report', async () => {
    const test = lane({ error: { reason_code: 'BAD_PARAMS', error_code: 'BAD_PARAMS', message: 'Unsupported units', retryable: false } }, 422)
    const result = await test.run({ params: { units: 'invalid' } })
    expect(result).toMatchObject({ ok: false, result: null, version: null, error: { message: 'Unsupported units' } })
    expect(test.client.getSnapshot().binding).toEqual(binding)
  })
})
