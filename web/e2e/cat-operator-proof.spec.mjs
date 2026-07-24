import { expect, test } from '@playwright/test'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..', '..')
const PROOF_DIR = join(ROOT, 'artifacts', 'cat-operator-proof')
const REQUEST = 'Rearrange the existing panels in this drawing into the shape of a sitting cat. Preserve every panel, create a new version, and show me the proposed change before anything runs.'

function readPbm(path) {
  const tokens = readFileSync(path, 'utf8')
    .replace(/#[^\n]*/g, '')
    .trim()
    .split(/\s+/)
  if (tokens.shift() !== 'P1') throw new Error('cat proof expects an ASCII PBM fixture')
  const width = Number(tokens.shift())
  const height = Number(tokens.shift())
  const points = []
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      if (tokens[y * width + x] === '1') points.push([x, height - y - 1])
    }
  }
  return points.sort((a, b) => a[1] - b[1] || a[0] - b[0])
}

function panel(handle, x, y) {
  return {
    handle,
    layer: 'PANELS',
    closed: true,
    pts: [[x, y, 7], [x + 1, y, 7], [x + 1, y + 1, 7], [x, y + 1, 7]],
    xdata: { panel: handle },
    metadata: { kind: 'roof-panel' },
  }
}

function makeIntakes() {
  const points = readPbm(join(ROOT, 'server', 'tests', 'fixtures', 'cat_oracle', 'sitting-v1.pbm'))
  const handles = points.map((_, index) => `P${String(index).padStart(4, '0')}`)
  const base = {
    dwg: 'cat.dwg', layers: ['PANELS'], inserts: [], faces3d: [], blockdefs: {},
    polylines: handles.map((handle, index) => panel(handle, index % 64, Math.floor(index / 64))),
  }
  const cat = {
    ...base,
    polylines: handles.map((handle, index) => panel(handle, points[index][0], points[index][1])),
  }
  return { base, cat, count: handles.length }
}

function envelope(seq, turnId, type, data) {
  return { v: 1, session_id: 'cat-session', turn_id: turnId, seq, type, data }
}

test('operator request produces an approved cat version and undo restores its parent', async ({ page }) => {
  test.setTimeout(60_000)
  mkdirSync(PROOF_DIR, { recursive: true })
  const { base, cat, count } = makeIntakes()
  expect(count).toBe(3328)
  const browserErrors = []
  const failedResources = []
  page.on('pageerror', (error) => browserErrors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') browserErrors.push(message.text())
  })
  page.on('response', (response) => {
    if (response.status() >= 400) failedResources.push(`${response.status()} ${response.url()}`)
  })
  let head = 1
  let events = []

  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()
    const json = (body, status = 200) => route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body),
      headers: { 'access-control-allow-origin': '*' },
    })

    if (method === 'OPTIONS') return route.fulfill({ status: 204, headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' } })
    if (path === '/api/session') return json({ intake: base, tenant_id: 'cat-litmus-tenant', tier: 'proof', org_id: 'cat-proof-org' })
    if (path === '/api/tools') return json({ tools: [] })
    if (path === '/api/capabilities') return json({ families: [] })
    if (path === '/api/entitlements') return json({ tier: 'proof', entitlements: { run_read: true, run_write: true, build: true, converse: true } })
    if (path === '/api/tenant/claude-grant') return json({ linked: true, linked_at: '2026-07-24T12:00:00Z' })
    if (path === '/api/health') return json({ ok: true, aps_live: false, n_tools: 1, n_authored: 1 })
    if (path === '/api/usage') return json({ today: { runs: head - 1, usd_est: 0 }, total: { runs: head - 1, usd_est: 0 } })
    if (path === '/api/jobs') return json({ jobs: [] })
    if (path === '/api/nl-prompt' && method === 'POST') return json({ lane: 'build', tool: null, params: {}, confidence: 0.42, rationale: 'The assistant must plan a controlled drawing write.', alternatives: [] })
    if (path === '/api/sessions' && method === 'POST') return json({ session_id: 'cat-session', status: 'idle', created_at: '2026-07-24T12:00:00Z' })
    if (path === '/api/sessions/cat-session/messages' && method === 'POST') {
      const body = request.postDataJSON()
      if (body.text) {
        events = [
          envelope(1, 'turn-1', 'turn_started', {}),
          envelope(2, 'turn-1', 'text_delta', { text: 'I found a tenant tool that preserves all panel identities and local geometry. I will stage a sitting-cat layout as a new version.' }),
          envelope(3, 'turn-1', 'proposed_run', { confirmation_id: 'cat-confirmation', tool: 'arrange-panels-as-cat', params: { template: 'sitting-v1', drawing_id: 'cat-panels', expected_head: 1 }, capability: 'drawing.write', rationale: 'Creates version 2 and keeps version 1 available for undo.' }),
          envelope(4, 'turn-1', 'turn_complete', { stop_reason: 'awaiting_approval' }),
        ]
        return json({ turn_id: 'turn-1', status: 'started' }, 202)
      }
      events.push(
        envelope(5, 'turn-2', 'turn_started', {}),
        envelope(6, 'turn-2', 'confirmation_resolved', { confirmation_id: 'cat-confirmation', approved: true, by: 'operator' }),
        envelope(7, 'turn-2', 'tool_call', { tool: 'arrange-panels-as-cat', args_summary: 'sitting-v1 · 3,328 panels · expected head v1' }),
        envelope(8, 'turn-2', 'tool_result', { tool: 'arrange-panels-as-cat', ok: true, summary: 'Cat oracle passed · IoU 1.000 · overlap 0' }),
        envelope(9, 'turn-2', 'job_linked', { job_id: 'cat-job-0002', tool: 'arrange-panels-as-cat' }),
        envelope(10, 'turn-2', 'text_delta', { text: 'Version 2 is ready. The cat oracle passed and version 1 remains available through Undo.' }),
        envelope(11, 'turn-2', 'turn_complete', { stop_reason: 'end_turn' }),
      )
      return json({ turn_id: 'turn-2', status: 'started' }, 202)
    }
    if (path === '/api/agent/approvals/cat-confirmation' && method === 'POST') return json({ resolved: true, approved: true })
    if (path === '/api/sessions/cat-session/transcript') return json({ events })
    if (path === '/api/sessions/cat-session/stream') return route.fulfill({ status: 204, headers: { 'access-control-allow-origin': '*' } })
    if (path === '/api/jobs/cat-job-0002/stream') return route.fulfill({ status: 204, headers: { 'access-control-allow-origin': '*' } })
    if (path === '/api/jobs/cat-job-0002' && method === 'GET') {
      head = 2
      return json({
        job_id: 'cat-job-0002', status: 'complete', tool: 'arrange-panels-as-cat', elapsed_ms: 5240,
        result: { ok: true, tool: 'arrange-panels-as-cat', version: '1.0.0', timing_ms: 5240, cost: null, error: null, degraded_mode: false, overlay: null, result: { panels_preserved: count, cat_oracle: { verdict: 'pass', template: 'sitting-v1', iou: 1, outline_chamfer_px: 0, overlap_pixels: 0 }, new_version: { drawing_id: 'cat-panels', version: 2, parent: 1 } } },
      })
    }
    if (path === '/api/drawings/cat-panels/intake') return json({ drawing_id: 'cat-panels', intake: head === 2 ? cat : base, version: head, head, latest: 2 })
    if (path === '/api/drawings/cat-panels/versions') return json({
      drawing_id: 'cat-panels', head, latest: 2, checkout: null,
      versions: [
        { v: 1, parent: null, tool: 'base', note: 'Original drawing' },
        { v: 2, parent: 1, tool: 'arrange-panels-as-cat', note: 'Sitting cat, oracle pass' },
      ],
    })
    if (path === '/api/drawings/cat-panels/undo' && method === 'POST') {
      head = 1
      return json({ drawing_id: 'cat-panels', intake: base, version: 1, head: 1, latest: 2 })
    }
    if (path === '/api/drawings/demo/versions') return json({ drawing_id: 'demo', head: 1, latest: 1, checkout: null, versions: [] })
    return json({ error: { message: `unhandled proof route ${method} ${path}` } }, 404)
  })

  await page.goto('/app')
  await expect(page.getByText('cat.dwg')).toBeVisible()
  await expect(page.locator('body')).not.toHaveText('Internal Server Error')
  await expect(page.locator('.vite-error-overlay')).toHaveCount(0)
  await page.getByRole('combobox', { name: 'Command bar' }).fill(REQUEST)
  await page.getByRole('button', { name: 'Run' }).click()

  const approval = page.locator('.converse-confirm').filter({ hasText: 'arrange-panels-as-cat' })
  await expect(approval).toContainText('drawing.write', { timeout: 10_000 })
  await expect(approval).toContainText('expected head 1')
  await page.locator('.converse-card').screenshot({ path: join(PROOF_DIR, '01-request-and-approval.png') })

  await approval.getByRole('button', { name: 'Approve' }).click()
  const attach = page.getByRole('button', { name: 'Attach' })
  await expect(attach).toBeVisible({ timeout: 10_000 })
  await attach.click()

  await expect(page.getByRole('button', { name: 'Undo' })).toBeEnabled({ timeout: 10_000 })
  await expect(page.getByText('Cat oracle passed · IoU 1.000 · overlap 0')).toBeVisible()
  await page.locator('.workspace-card').screenshot({ path: join(PROOF_DIR, '02-cat-version.png') })
  await page.screenshot({ path: join(PROOF_DIR, '02-cat-version-full.png'), fullPage: true })

  await page.getByRole('button', { name: 'Undo' }).click()
  await expect(page.getByRole('button', { name: 'Redo' })).toBeEnabled({ timeout: 10_000 })
  await page.locator('.workspace-card').screenshot({ path: join(PROOF_DIR, '03-undo-restored.png') })

  writeFileSync(join(PROOF_DIR, 'receipt.json'), JSON.stringify({
    scope: 'deterministic browser acceptance fixture, not a live Claude or APS run',
    request: REQUEST,
    panels_preserved: count,
    proposal: { tool: 'arrange-panels-as-cat', capability: 'drawing.write', expected_head: 1 },
    result: { verdict: 'pass', template: 'sitting-v1', iou: 1, outline_chamfer_px: 0, overlap_pixels: 0, version: 2, parent: 1 },
    undo: { head: 1, redo_available: true },
  }, null, 2) + '\n')
  expect({ browserErrors, failedResources }).toEqual({ browserErrors: [], failedResources: [] })
})
