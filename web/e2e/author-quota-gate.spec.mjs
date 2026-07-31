import { expect, test } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const PROOF_DIR = join(HERE, '..', '..', 'artifacts', 'cat-operator-proof')

// The per-tenant DAILY authoring cap refuses with HTTP 429 (CONTRACT-ADDENDUM
// §17). The web must render that as a CALM QuotaGate — a plan boundary that
// says when it lifts — and never as the red "Couldn't author the tool" surface.
//
// The bodies below are not hand-written: they are the exact output of
// `da.usage.daily_author_envelope(...)`, which is what server/routers/author.py
// returns from `_quota_exceeded_response`, plus the `degraded_mode: false` that
// same function overwrites onto it (the wire schema requires a boolean).
const envelope = (tier, limit, used) => ({
  ok: false,
  tool: null,
  result: null,
  overlay: null,
  cost: null,
  error: {
    error_code: 'quota_exceeded',
    message: `Daily authoring limit reached for your plan (${used}/${limit}). Resets 00:00 UTC. Upgrade for more.`,
    retryable: true,
    tier,
    limit,
    used,
    quota_kind: 'daily_author',
  },
  error_code: 'quota_exceeded',
  retryable: true,
  message: `Daily authoring limit reached for your plan (${used}/${limit}). Resets 00:00 UTC. Upgrade for more.`,
  tier,
  limit,
  used,
  quota_kind: 'daily_author',
  degraded_mode: false,
})

// Both the temporary probe posture (cap 1) and the reviewed staging pilot value
// (cap 10). The gate must read the counts off the envelope in both.
const CASES = [
  { name: 'probe posture', tier: 'hosted_starter', limit: 1, used: 1 },
  { name: 'reviewed staging pilot', tier: 'hosted_starter', limit: 10, used: 10 },
]

for (const { name, tier, limit, used } of CASES) {
  test(`daily authoring 429 renders a calm QuotaGate, not a failure (${name})`, async ({ page }) => {
    test.setTimeout(120_000)
    const proofState = makeCatProofState()
    const body = envelope(tier, limit, used)
    let stageAttempts = 0

    await page.addInitScript(() => localStorage.setItem('leaf.org_id', 'cat-proof-org'))

    await page.route('http://leaf-proof.invalid/api/**', async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      let payload = {}
      if (request.postData()) {
        try { payload = request.postDataJSON() } catch { payload = {} }
      }
      const headers = {
        'access-control-allow-origin': '*',
        'access-control-allow-headers': '*',
      }

      // The one route under test: the authoring lane the Generate button hits.
      if (url.pathname === '/api/author/stage' && request.method() === 'POST') {
        stageAttempts += 1
        await route.fulfill({
          status: 429,
          contentType: 'application/json',
          body: JSON.stringify(body),
          headers,
        })
        return
      }

      const result = catProofResponse({
        method: request.method(),
        path: url.pathname,
        body: payload,
        query: Object.fromEntries(url.searchParams),
      }, proofState)
      await route.fulfill({
        status: result.status,
        contentType: result.body == null ? undefined : 'application/json',
        body: result.body == null ? '' : JSON.stringify(result.body),
        headers,
      })
    })

    await page.goto('/try?proof=1')
    await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })

    await page.getByRole('tab', { name: 'Author' }).click()
    await page.getByLabel('What should the tool do?').fill('count panels within 24in of the roof edge')
    await page.getByRole('button', { name: 'Generate tool' }).click()

    // CALM: the gate is a status region, states the counts from the envelope,
    // and says when the boundary lifts.
    const gate = page.locator('.author-gate[role="status"]')
    await expect(gate).toBeVisible()
    await expect(gate).toContainText(`(${used}/${limit})`)
    await expect(gate).toContainText('daily limit on authoring new tools')
    await expect(gate).toContainText('resets at 00:00 UTC')

    // NOT a failure: no red inline error, no retry affordance, nothing staged.
    await expect(page.locator('.inline-error')).toHaveCount(0)
    await expect(page.getByText(/Couldn’t author the tool/)).toHaveCount(0)
    await expect(page.locator('.authored')).toHaveCount(0)

    // The description survives, and the refusal was one attempt (no auto-retry
    // loop against a lane that costs money to enter).
    await expect(page.getByLabel('What should the tool do?'))
      .toHaveValue('count panels within 24in of the roof edge')
    expect(stageAttempts).toBe(1)

    mkdirSync(PROOF_DIR, { recursive: true })
    await page.screenshot({
      path: join(PROOF_DIR, `author-quota-gate-${limit}.png`),
      fullPage: true,
    })
  })
}
