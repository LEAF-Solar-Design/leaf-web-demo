import { expect, test } from '@playwright/test'
import { createHash, randomUUID } from 'node:crypto'
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { makeProofReceipt } from '../proofReceipt.mjs'
import { requireLocalReady } from './requireReady.mjs'
import { setRail } from './railFlag.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT = 'demo-tenant'
const TIER = 'fixture-replay'
const hash = (bytes) => createHash('sha256').update(bytes).digest('hex')

test('fixture-replay: marathon card running, verified, and synthetic promotion', async ({ page, request }, testInfo) => {
  test.setTimeout(180_000)
  // Both variables must name the same dedicated root BEFORE the managed
  // stack starts. Never point this fixture writer at a retained/live root.
  const configured = process.env.LEAF_MARATHON_RUNS_DIR
  const fixtureRoot = process.env.LEAF_E2E_MARATHON_FIXTURE_ROOT
  expect(process.env.LEAF_E2E_MANAGED).toBe('1')
  expect(configured && fixtureRoot && resolve(configured) === resolve(fixtureRoot),
    'Start the managed stack with an isolated marathon fixture root').toBeTruthy()
  await requireLocalReady(request, test, API_BASE)
  const health = await (await request.get(`${API_BASE}/api/health`)).json()
  expect(health.source_sha).toMatch(/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/i)
  const runId = `fixture-replay-${randomUUID()}`
  const runDir = join(resolve(fixtureRoot), TENANT, runId)
  mkdirSync(runDir, { recursive: true })
  const artifacts = []
  const observations = []
  const screenshots = []
  const title = `fixture-replay marathon ${runId}`
  const write = (name, value) => {
    const target = join(runDir, name)
    writeFileSync(target + '.tmp', JSON.stringify(value))
    renameSync(target + '.tmp', target)
  }
  const running = { run_id: runId, rounds: 1, spent_usd: 0, mission_complete: false,
    round_in_progress: { milestone: 'a', round: 2, attempt: 1 }, milestones: { a: { status: 'running' } } }
  const done = { run_id: runId, rounds: 2, spent_usd: 0, mission_complete: true,
    milestones: { a: { status: 'done', verified_at: new Date().toISOString() } } }
  try {
    write('run-manifest.json', { title, requested_by: TIER, started_at: Date.now() / 1000 })
    write('state.json', running)
    await page.setViewportSize({ width: 1600, height: 1000 })
    await setRail(page, '1')
    await page.goto('/app?surface=cad')
    await expect(page.locator('.app[data-surface="cad"]')).toHaveCount(1, { timeout: 30_000 })
    const badge = page.getByTestId('builds-badge')
    await expect(badge).toBeVisible({ timeout: 30_000 })
    await badge.click()
    const card = page.locator('.bq-card').filter({ hasText: title })
    async function capture(id, state, promoted, filenames) {
      await expect(card).toBeVisible({ timeout: 30_000 })
      await expect(card).toHaveAttribute('data-lane', 'fold')
      await expect(card).toHaveAttribute('data-state', state, { timeout: 30_000 })
      await expect(card).toHaveAttribute('data-verified', state === 'done' ? '1' : '0')
      await expect(card).toHaveAttribute('data-promoted', promoted ? '1' : '0', { timeout: 30_000 })
      const response = await request.get(`${API_BASE}/api/builds?limit=200`, { headers: { 'X-Tenant-Id': TENANT } })
      expect(response.ok()).toBe(true)
      const body = await response.json()
      expect(body.sources.fold).toBe('runs-dir')
      const record = body.builds.find((r) => r.id === runId)
      expect(record).toMatchObject({ lane: 'fold', state, terminal: { verified: state === 'done', promoted } })
      expect(record.actions).toEqual(state === 'running' ? ['cancel'] : promoted ? [] : ['promote'])
      await expect(card.locator('.rail-word')).toHaveText(record.status.word)
      const refs = filenames.map((name) => {
        const bytes = readFileSync(join(runDir, name))
        const filename = testInfo.outputPath(`fixture-replay-${id}-${name}`)
        writeFileSync(filename, bytes)
        artifacts.push({ filename, sha256: hash(bytes), synthetic: true, reconstructed: false, kind: 'fixture' })
        return filename
      })
      observations.push({ observation_id: id, run_id: runId, tenant_binding: TENANT,
        observed_at: Date.now(), synthetic: true, reconstructed: false, capture_mode: 'replay',
        source_artifacts: refs, api_result: body })
      await card.scrollIntoViewIfNeeded()
      const filename = testInfo.outputPath(`fixture-replay-${id}.png`)
      await page.screenshot({ path: filename })
      screenshots.push({ filename, sha256: hash(readFileSync(filename)), observation_id: id,
        run_id: runId, tenant_binding: TENANT, card_visible: true, card_state: state })
    }
    await capture('running', 'running', false, ['state.json', 'run-manifest.json'])
    write('state.json', done)
    await capture('verified-without-promotion', 'done', false, ['state.json', 'run-manifest.json'])
    write('promotion.json', { synthetic: true, promotion_stage: { status: 'promoted', ref: `fixture-replay/${runId}` } })
    await capture('synthetic-promotion', 'done', true, ['state.json', 'run-manifest.json', 'promotion.json'])
    // Reuse shared metadata validation in memory. Its local-e2e transport
    // vocabulary is not a marathon tier; no such receipt is written.
    const common = makeProofReceipt({ capability_ids: ['BQ-01'], evidence_tier: 'local-e2e',
      route: '/app?surface=cad', runtime: TIER, result: { evidence_tier: TIER },
      assertions: ['Fixture running card matched the real API', 'Fixture verification did not imply promotion',
        'Explicitly synthetic promotion changed the card; no deployment occurred'] })
    const receipt = { schema: 'leaf.marathon-card-proof.v1', evidence_tier: TIER,
      run_id: runId, source_sha: health.source_sha, tenant_binding: TENANT,
      source_artifacts: artifacts, observed_records: observations, screenshots, assertions: common.assertions }
    const path = testInfo.outputPath('fixture-replay-marathon-card-receipt.json')
    writeFileSync(path, JSON.stringify(receipt, null, 2) + '\n')
    await testInfo.attach('fixture-replay marathon receipt', { path, contentType: 'application/json' })
  } finally {
    rmSync(runDir, { recursive: true, force: true })
  }
})
