import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

// Previewing an older version is a READ-ONLY view of the drawing. /app's
// VersionHistory always locked writes during preview; /try's port carried the
// preview note and the active-row highlight but not the lock, so the deployed
// acceptance driver had to delete its read-only assertion (PR #409).
//
// This pins the ported behavior HERE, where it costs one mocked browser run,
// instead of only on the deployed surface where it costs a full authoring run.
test('previewing an older version locks drawing writes on /try until head returns', async ({ page }) => {
  test.setTimeout(150_000)
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    // The fixture serves /api/run for the read tool and the authored tool only:
    // the cat WRITE tool is normally driven through the agent path. Hand the
    // catalog run its job id and the fixture takes over from there, advancing
    // the head to v2 on the job read (catProofFixture.mjs `cat-job-0002`).
    if (
      url.pathname === '/api/run' && request.method() === 'POST' &&
      body.tool === 'arrange-panels-as-cat'
    ) {
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ job_id: 'cat-job-0002' }),
      })
      return
    }
    const result = catProofResponse(
      { method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) },
      state,
    )
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  const catalogTab = page.getByRole('tab', { name: /Catalog/ })
  const versionsTab = page.getByRole('tab', { name: /Versions/ })
  const card = page.locator('.tool-card').filter({ hasText: 'arrange-panels-as-cat' })
  // Scoped to the card on purpose: once the Versions rail is open, its v2 row
  // ("v2 arrange-panels-as-cat head") is a button carrying the same tool name.
  const toolHead = card.locator('button.tool-head')
  const catalogRun = card.getByRole('button', { name: 'Review & run', exact: true })
  const previewLock = page.getByTestId('try-preview-write-lock')
  // Idempotent: switching the RIGHT rail to Versions does not unmount the LEFT
  // catalog rail, so the card can still be open from a previous step — clicking
  // its head again would collapse it and hide the run control.
  const openToolCard = async () => {
    await catalogTab.click()
    const openCard = page.locator('.tool-card.open').filter({ hasText: 'arrange-panels-as-cat' })
    if (await openCard.count() === 0) await toolHead.click()
    await expect(catalogRun).toBeVisible()
  }

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })

  // Reach a two-version drawing, so there is an older version to preview.
  await catalogTab.click()
  await toolHead.click()
  await expect(catalogRun).toBeEnabled()
  await catalogRun.click()
  await page.getByRole('button', { name: 'Run arrange-panels-as-cat' }).click()
  await expect(page.getByTestId('version-head')).toContainText('Version 2', { timeout: 45_000 })

  // Preview v1. The head must stay at v2 — this is a view, not a restore.
  await versionsTab.click()
  const history = page.getByRole('region', { name: 'Version history' })
  await history.getByTestId('try-version-v1').getByRole('button').first().click()
  await expect(history.getByText('Viewing v1 read-only')).toBeVisible()
  await expect(previewLock).toBeVisible()
  await expect(page.getByTestId('version-head')).toContainText('Version 2')

  // The write control is genuinely DISABLED, not merely left unused, and the
  // catalog says why rather than presenting a dead chip. (The version panel is
  // the right rail and the catalog the left, so both are on screen at once.)
  await openToolCard()
  await expect(catalogRun).toBeDisabled()
  await expect(card.locator('.lock-note')).toContainText(/viewing v1 read-only/i)

  // Returning to head LIFTS the lock. Without this, a single preview would
  // strand the surface read-only for the rest of the session.
  await versionsTab.click()
  await history.getByRole('button', { name: 'Back to head', exact: true }).click()
  await expect(previewLock).toHaveCount(0)
  await openToolCard()
  await expect(catalogRun).toBeEnabled()
})
