import { expect, test } from '@playwright/test'
import { join } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { requireLocalReady } from './requireReady.mjs'
import { setRail } from './railFlag.mjs'

// Slice 11a: the BuildQueueCard on the console's job monitor, driven by ONE
// real local job (the local engine stands in for APS under APS_LIVE=0, the
// guarded _qa_sleep_s hook keeps it running long enough to observe).
//
// What this row proves, against the real stack:
//   1. GET /api/builds lists the job as a broker-lane record while it runs,
//      with the two-stage terminal both false and `cancel` declared;
//   2. the studio console's document band shows the running-count badge,
//      and one click on it expands the job spine;
//   3. the rail hosts a BuildQueueCard for the job that reads the SAME
//      status word JobRail always showed ("running", then "complete");
//   4. once complete, the card is verified and NOT promoted (never inferred),
//      GET /api/builds carries the SHA-stamped terminal receipt, and the
//      receipt's source_sha is the stack's own LEAF_SOURCE_SHA.
const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')

async function submitSlowJob(request, seconds) {
  const toolsResponse = await request.get(`${API_BASE}/api/tools`, { headers: TENANT_HEADERS })
  expect(toolsResponse.ok()).toBe(true)
  const tool = (await toolsResponse.json()).tools.find((candidate) => candidate.name === 'count-by-layer')
  expect(tool?.catalog_digest).toBeTruthy()
  const response = await request.post(`${API_BASE}/api/run`, {
    headers: { ...TENANT_HEADERS, 'Content-Type': 'application/json' },
    data: {
      tool: 'count-by-layer',
      params: { _qa_sleep_s: seconds },
      dwg: 'cat-panels',
      catalog_digest: tool.catalog_digest,
    },
  })
  const body = await response.json()
  expect(response.status(), JSON.stringify(body)).toBe(202)
  return body.job_id
}

async function buildRecord(request, jobId) {
  const response = await request.get(`${API_BASE}/api/builds?limit=50`, { headers: TENANT_HEADERS })
  expect(response.ok()).toBe(true)
  const body = await response.json()
  return { body, record: (body.builds || []).find((b) => b.id === jobId) || null }
}

test('one real job rides the BuildQueueCard from running to a verified, unpromoted terminal with a SHA-stamped receipt', async ({ page, request }) => {
  test.setTimeout(240_000)
  const ready = await requireLocalReady(request, test, API_BASE)
  const health = await (await request.get(`${API_BASE}/api/health`)).json()

  // Boot the console FIRST: it polls GET /api/jobs every 2.5 s once it is up,
  // and a job submitted before the shell finished booting (15-30 s on the
  // local stack) would be complete before the first poll, which the badge
  // would honestly report as nothing running.
  await setRail(page, '1')
  await page.goto('/app?surface=cad')
  await expect(page.locator('.app[data-surface="cad"]')).toHaveCount(1, { timeout: 30_000 })
  await expect(page.locator('aside.rail[data-spine]')).toHaveCount(1, { timeout: 30_000 })
  await expect(page.getByTestId('builds-badge')).toHaveCount(0)

  const jobId = await submitSlowJob(request, 45)

  // 1. the record while it runs
  const running = await buildRecord(request, jobId)
  expect(running.record, JSON.stringify(running.body)).toBeTruthy()
  expect(running.record.lane).toBe('broker')
  expect(['queued', 'running']).toContain(running.record.state)
  expect(running.record.terminal).toEqual({ verified: false, promoted: false })
  expect(running.record.actions).toEqual(['cancel'])
  expect(running.record.title).toBe('count-by-layer')
  expect(running.body.sources.broker).toBe('jobs-store')
  expect(Array.isArray(running.body.warnings)).toBe(true)

  // 2. the console under the studio: CAD is a job-spine surface, so the
  //    badge is a button that expands the spine. The next poll tick lands
  //    within 2.5 s; the wait covers a slow tick under host load.
  const badge = page.getByTestId('builds-badge')
  await expect(badge).toBeVisible({ timeout: 20_000 })
  await expect(badge).toContainText('running')
  await expect(page.locator('aside.rail[data-spine]')).toHaveCount(1)
  await badge.click()
  await expect(page.locator('aside.rail[data-spine]')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: /Job monitor/ })).toBeVisible()

  // 3. the card, with JobRail's own status word
  const card = page.locator('.bq-card').filter({ hasText: 'count-by-layer' }).first()
  await expect(card).toBeVisible()
  await expect(card).toHaveAttribute('data-lane', 'broker')
  const row = card.locator('.rail-row.build-queue-card')
  await expect(row).toHaveCount(1)
  await expect(row.locator('.rail-word')).toHaveText(/running|submitted/)
  await expect(card.locator('.bq-stages')).toHaveCount(0)

  // 4. to done: verified by its own terminal receipt, not promoted
  await expect(card).toHaveAttribute('data-state', 'done', { timeout: 90_000 })
  await expect(row.locator('.rail-word')).toHaveText('complete')
  await expect(card).toHaveAttribute('data-verified', '1')
  await expect(card).toHaveAttribute('data-promoted', '0')
  await expect(card.locator('.bq-mark.verified.on')).toHaveCount(1)
  await expect(card.locator('.bq-mark.promoted.on')).toHaveCount(0)
  await expect(page.getByTestId('builds-badge')).toHaveCount(0)

  await expect.poll(async () => (await buildRecord(request, jobId)).record?.state, { timeout: 60_000 }).toBe('done')
  const done = await buildRecord(request, jobId)
  expect(done.record.terminal).toEqual({ verified: true, promoted: false })
  expect(done.record.actions).toEqual([])
  expect(done.record.status.word).toBe('complete')
  expect(done.record.receipts).toHaveLength(1)
  expect(done.record.receipts[0]).toMatchObject({ kind: 'terminal', ref: `receipts/${jobId}/receipt.json` })
  await expect(card.locator('.bq-receipts')).toHaveText('1 receipt', { timeout: 15_000 })

  writeProofReceipt(join(PROOF_DIR, 'build-queue-card-receipt.json'), {
    capability_ids: ['JB-01', 'JB-02', 'BQ-01'],
    evidence_tier: 'local-e2e',
    route: '/app?surface=cad',
    runtime: 'real local Vite, FastAPI, broker, SQLite job store and job worker with guarded QA latency; the local engine stands in for APS',
    api_endpoints: ['GET /api/tools', 'POST /api/run', 'GET /api/builds', 'GET /api/health'],
    assertions: [
      'GET /api/builds listed the running job as a broker-lane record with both terminal stages false and cancel declared',
      'the studio console showed the running-count badge and one click expanded the job spine',
      'the rail hosted a BuildQueueCard carrying JobRail\'s own status word for the job',
      'the card reached done as verified and not promoted, and /api/builds carried its terminal receipt',
    ],
    result: {
      verdict: 'pass',
      job_id: jobId,
      source_sha: health.source_sha,
      ready: !!ready?.ready,
      receipt_ref: done.record.receipts[0].ref,
      sources: done.body.sources,
      warnings: done.body.warnings,
    },
    limitations: [
      'The guarded local _qa_sleep_s hook makes the running state observable.',
      'APS_LIVE=0 substitutes the local engine for Autodesk APS.',
      'The fleet and fold lanes are unconfigured on the local stack, so only the broker lane carries a record here; the route reports them as unconfigured.',
    ],
  })
})
