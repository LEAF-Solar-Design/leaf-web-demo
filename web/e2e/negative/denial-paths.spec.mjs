import { expect, test } from '@playwright/test'
import {
  expectNoCreatedWork,
  installNegativeApi,
  openApp,
  proposeCat,
  submitReadRun,
} from './negativeApiFixture.mjs'

test.describe('negative browser contracts', () => {
  test.setTimeout(60_000)
  test('operator denial stays denied and creates no job or version', async ({ page }) => {
    const { evidence, state } = await installNegativeApi(page, { approval: 'denied' })
    await openApp(page)
    const card = await proposeCat(page)

    await card.getByRole('button', { name: 'Deny' }).click()

    await expect(card).toContainText('Denied')
    await expect(page.getByText('Denied. The drawing remains unchanged.')).toBeVisible()
    await expect(page).toHaveURL(/\/app$/)
    expect(evidence.runSubmissions).toBe(0)
    expectNoCreatedWork(expect, evidence, state)
  })

  test('stale approval 409 is honest and creates no job or version', async ({ page }) => {
    const { evidence, state } = await installNegativeApi(page, { approval: 'stale' })
    await openApp(page)
    const card = await proposeCat(page)

    await card.getByRole('button', { name: 'Approve' }).click()

    await expect(page.getByText(/That request was already decided/)).toBeVisible()
    await expect(card.getByRole('button', { name: 'Approve' })).toBeEnabled()
    await expect(page).toHaveURL(/\/app$/)
    expect(evidence.runSubmissions).toBe(0)
    expectNoCreatedWork(expect, evidence, state)
  })

  test('expired approval 410 asks for a fresh proposal and creates no work', async ({ page }) => {
    const { evidence, state } = await installNegativeApi(page, { approval: 'expired' })
    await openApp(page)
    const card = await proposeCat(page)

    await card.getByRole('button', { name: 'Approve' }).click()

    await expect(page.getByText(/That confirmation expired/)).toBeVisible()
    await expect(card.getByRole('button', { name: 'Approve' })).toBeEnabled()
    await expect(page).toHaveURL(/\/app$/)
    expect(evidence.runSubmissions).toBe(0)
    expectNoCreatedWork(expect, evidence, state)
  })

  test('entitlement denial is a calm plan boundary with no created work', async ({ page }) => {
    const { evidence, state } = await installNegativeApi(page, {
      run: {
        status: 403,
        body: {
          entitlement_required: true,
          required: 'run_read',
          tier: 'restricted',
          error: { error_code: 'entitlement_required', message: 'Read tools are not included in this plan.', retryable: false },
        },
      },
    })
    await openApp(page)
    await submitReadRun(page)

    const result = page.locator('.result-panel')
    await expect(result.getByText('Plan', { exact: true }).first()).toBeVisible()
    await expect(result).toContainText('Read tools are not included in this plan.')
    await expect(result).toContainText('nothing ran')
    await expect(page).toHaveURL(/\/app$/)
    expect(evidence.runSubmissions).toBe(1)
    expectNoCreatedWork(expect, evidence, state)
  })

  test('daily quota 429 shows the limit and creates no job or version', async ({ page }) => {
    const { evidence, state } = await installNegativeApi(page, {
      run: {
        status: 429,
        body: {
          tier: 'starter',
          limit: 10,
          used: 10,
          error: {
            error_code: 'quota_exceeded',
            message: 'Daily run limit reached for your plan (10/10). Resets 00:00 UTC. Upgrade for more.',
            retryable: true,
          },
        },
        usage: { today: { runs: 10, usd_est: 0 }, total: { runs: 10, usd_est: 0 } },
      },
    })
    await openApp(page)
    await submitReadRun(page)

    const result = page.locator('.result-panel')
    await expect(result.getByText('Daily limit', { exact: true }).first()).toBeVisible()
    await expect(result).toContainText('10/10 runs used')
    await expect(result).toContainText('Resets 00:00 UTC. Upgrade for more.')
    await expect(page).toHaveURL(/\/app$/)
    expect(evidence.runSubmissions).toBe(1)
    expectNoCreatedWork(expect, evidence, state)
  })

  test('spend cap 402 shows an uncharged rejection and creates no work', async ({ page }) => {
    const { evidence, state } = await installNegativeApi(page, {
      run: {
        status: 402,
        body: {
          ok: false,
          tool: 'count-by-layer',
          version: null,
          result: null,
          overlay: null,
          timing_ms: 0,
          cost: null,
          degraded_mode: false,
          error: { error_code: 'quota_exceeded', message: 'The tenant spend cap has been reached.', retryable: false },
        },
        usage: {
          today: { runs: 4, usd_est: 1 },
          total: { runs: 4, usd_est: 1 },
          cap: { enabled: true, usd: 1, remaining: 0 },
        },
      },
    })
    await openApp(page)
    await submitReadRun(page)

    const result = page.locator('.result-panel')
    await expect(result.getByText('Spend cap', { exact: true }).first()).toBeVisible()
    await expect(result).toContainText('The tenant spend cap has been reached.')
    await expect(result).toContainText("this run wasn’t charged")
    await expect(page).toHaveURL(/\/app$/)
    expect(evidence.runSubmissions).toBe(1)
    expectNoCreatedWork(expect, evidence, state)
  })
})
