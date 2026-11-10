import { describe, expect, it, vi } from 'vitest'
import { runProjectCatalogTool, seatProjectDrawingPublication } from './App.jsx'
import { createSolarProjectClient } from './solar/solarProjectClient.js'

// The real App project run branch and the real project client, down to the fetch boundary.
const O = '11111111-1111-1111-1111-111111111111'
const P = '22222222-2222-2222-2222-222222222222'
const D = '33333333-3333-3333-3333-333333333333'
const V = '44444444-4444-4444-4444-444444444444'
const N = '66666666-6666-6666-6666-666666666666'
const J = '55555555-5555-5555-5555-555555555555'
const digest = 'sha256:' + 'a'.repeat(64)
const proof = 'lco1.' + 'b'.repeat(64)
const binding = { kind: 'project', organization_id: O, project_id: P, drawing_id: D, input_version_id: V }
const context = { orgId: O, projectId: P, drawingId: V }
const projectContext = { organization_id: O, project_id: P, drawing_id: D, input_version_id: V }
const lease = { holder: 'editor', acquired: '2026-01-01T00:00:00Z', expires: '2099-01-01T00:00:00Z', fence: '7' }
const reply = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data })

async function setup({ take = true } = {}) {
  const calls = []
  let next = null
  const fetchImpl = vi.fn(async (url, init) => {
    calls.push({ url, init })
    if (url.endsWith('/checkout') && init.method === 'POST') {
      return reply({ organization_id: O, project_id: P, drawing_id: D, acquired: true, checkout: lease, checkout_capability: proof })
    }
    return next
  })
  const client = createSolarProjectClient({ fetchImpl, apiBase: 'https://api.test', authHeaders: () => ({ Authorization: 'Bearer synthetic' }) })
  client.setScope(binding, 'session-1')
  if (take) expect((await client.acquireCheckout()).ok).toBe(true)
  const projectSolar = { ready: true, projectClient: client }
  const run = (tool, changes = {}) => runProjectCatalogTool({ projectSolar, tool: { name: tool }, params: { drawing_id: 'demo' },
    context, catalogDigest: digest, idempotencyKey: 'confirm-1', ...changes })
  const runCalls = () => calls.filter((call) => call.url.endsWith('/api/run'))
  return { client, run, runCalls, respond: (data, status) => { next = reply(data, status) } }
}

describe('confirmed project catalog runs', () => {
  it('a count reaches the project transport with the canonical binding and shows its result', async () => {
    const lane = await setup({ take: false })
    lane.respond({ ok: true, tool: 'count-by-layer', result: { counts: { Panels: 1 }, total: 1 }, project_context: projectContext })
    const envelope = await lane.run('count-by-layer')
    expect(envelope).toMatchObject({ ok: true, result: { total: 1 } })
    const [sent] = lane.runCalls()
    expect(JSON.parse(sent.init.body)).toMatchObject({ tool: 'count-by-layer', dwg: V, params: { drawing_id: D } })
    expect(sent.init.headers).toMatchObject({ 'X-Org-Id': O, 'X-Project-Id': P })
    expect(sent.init.headers).not.toHaveProperty('X-Checkout-Capability')
    lane.client.dispose()
  })

  it('a write carries the project checkout proof privately and returns its new version', async () => {
    const lane = await setup()
    lane.respond({ ok: true, tool: 'delete-marked-panel', result: { deleted_handle: 'ABCD' }, project_context: projectContext,
      version: { version_id: N, org_id: O, project_id: P, drawing_id: D, seq: 2 } })
    const envelope = await lane.run('delete-marked-panel')
    expect(envelope.version.version_id).toBe(N)
    const [sent] = lane.runCalls()
    expect(sent.init.headers['X-Checkout-Capability']).toBe(proof)
    expect(sent.init.headers['Idempotency-Key']).toBe('confirm-1')
    expect(JSON.stringify(envelope)).not.toContain(proof)
    expect(JSON.stringify(lane.client.getSnapshot())).not.toContain(proof)
    lane.client.dispose()
  })

  it('a refused write shows the server reason and no version', async () => {
    const lane = await setup({ take: false })
    lane.respond({ error: { reason_code: 'SIP_R1_CHECKOUT_REQUIRED', error_code: 'BAD_PARAMS', retryable: false } }, 409)
    const envelope = await lane.run('delete-marked-panel')
    expect(envelope).toMatchObject({ ok: false, version: null, reason_code: 'SIP_R1_CHECKOUT_REQUIRED',
      error: { error_code: 'SIP_R1_CHECKOUT_REQUIRED' } })
    lane.client.dispose()
  })

  it('a changed project version or a result for another context dispatches or completes nothing', async () => {
    const lane = await setup()
    const stale = await lane.run('delete-marked-panel', { context: { ...context, drawingId: N } })
    expect(stale).toMatchObject({ ok: false, error: { error_code: 'project_request_required' } })
    expect(lane.runCalls()).toHaveLength(0)
    lane.respond({ ok: true, tool: 'delete-marked-panel', project_context: { ...projectContext, input_version_id: N },
      version: { version_id: N, org_id: O, project_id: P, drawing_id: D } })
    const foreign = await lane.run('delete-marked-panel')
    expect(foreign).toMatchObject({ ok: false, version: null, error: { error_code: 'SIP_R7_RESPONSE_INVALID' } })
    const absent = await runProjectCatalogTool({ projectSolar: { ready: false, projectClient: lane.client }, tool: { name: 'count-by-layer' },
      params: {}, context, catalogDigest: digest, idempotencyKey: 'confirm-2' })
    expect(absent.ok).toBe(false)
    lane.client.dispose()
  })

  it('a real queued job is attached through the existing subscription', async () => {
    const lane = await setup()
    lane.respond({ job_id: J, status: 'submitted' }, 202)
    const onSubmit = vi.fn()
    const attach = vi.fn(async () => ({ ok: true, tool: 'delete-marked-panel' }))
    const envelope = await lane.run('delete-marked-panel', { onSubmit, attach })
    expect(onSubmit).toHaveBeenCalledWith(J)
    expect(attach).toHaveBeenCalledWith(J, expect.any(Object))
    expect(envelope.ok).toBe(true)
    lane.client.dispose()
  })

  it('a written version is selected only after the workspace lists it', async () => {
    const version = { version_id: N, org_id: O, project_id: P, drawing_id: D }
    const state = { openProjectId: P, orgId: O, canonicalVersionId: V }
    const make = (rows) => ({ getSnapshot: () => state, rehydrate: vi.fn(async () => ({ drawing_versions: rows })),
      selectCanonicalVersion: vi.fn() })
    const listed = make([version])
    expect(await seatProjectDrawingPublication(listed, { drawing_version: version })).toBe(true)
    expect(listed.rehydrate).toHaveBeenCalledTimes(1)
    expect(listed.selectCanonicalVersion).toHaveBeenCalledWith(N)
    const unlisted = make([])
    expect(await seatProjectDrawingPublication(unlisted, { drawing_version: version })).toBe(false)
    expect(unlisted.selectCanonicalVersion).not.toHaveBeenCalled()
  })
})
