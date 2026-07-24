import { expect, test } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { REQUEST, catProofResponse, makeCatProofState } from './catProofFixture.mjs'
import { writeProofReceipt } from './proofReceipt.mjs'

const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'reduced-motion')

test('reduced motion completes the operator flow without hiding filled panes', async ({ page }) => {
  mkdirSync(PROOF_DIR, { recursive: true })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const proofState = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const body = request.postData() ? request.postDataJSON() : {}
    const result = catProofResponse({ method: request.method(), path: url.pathname, body }, proofState)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Backend ready')
  await page.getByRole('textbox', { name: 'Command bar' }).fill(REQUEST)
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  await page.getByRole('button', { name: 'Approve' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('Cat version ready', { timeout: 15_000 })

  const surface = page.getByTestId('operator-surface')
  await expect(surface).toBeVisible()
  const style = await surface.evaluate((element) => {
    const computed = getComputedStyle(element)
    return { opacity: computed.opacity, animationDuration: computed.animationDuration }
  })
  expect(style.opacity).toBe('1')
  expect(style.animationDuration).toBe('0s')
  await page.screenshot({ path: join(PROOF_DIR, 'completed.png'), fullPage: true })

  writeProofReceipt(join(PROOF_DIR, 'receipt.json'), {
    capability_ids: ['MO-02', 'MO-03'],
    evidence_tier: 'contract',
    route: '/try',
    runtime: 'Vite with deterministic API transport and prefers-reduced-motion: reduce',
    assertions: [
      'the completed operator surface remains visible at opacity 1',
      'filled animations complete at zero duration',
      'the request, approval, job, and version flow completes under reduced motion',
    ],
    artifacts: ['completed.png'],
    result: { verdict: 'pass', computed_style: style },
    limitations: ['This is a deterministic browser contract, not a real local backend run.'],
  })
})
