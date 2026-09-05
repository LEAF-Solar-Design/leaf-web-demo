import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

// Standardization slice 7b (web half, item 2): GET /api/surface-config is a
// real dev-server route, mocked here exactly like every other proof endpoint
// (catProofFixture's fallback 404s on it, so the overlay response is the ONE
// local addition to the route handler, same pattern solar-author-stage.spec.mjs
// uses for /api/capabilities). Proves end to end, through a real browser and a
// real dev server, what useSurfaceContract.test.js and
// ProductSurfaceTabs.provenance.test.jsx already prove in jsdom: an overlay
// that flips `chrome.tab` for a surface the default manifest ships `false`
// (sheets) makes a new tab appear with no deploy, carrying the provenance
// chip; an empty overlay renders the frozen four-tab default, no chip.
function routeSurfaceConfig(page, proofState, overlayBody) {
  return page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/surface-config') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(overlayBody),
        headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
      })
      return
    }
    const body = request.postData() ? request.postDataJSON() : {}
    const result = catProofResponse({ method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, proofState)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })
}

test('a chrome.tab-flipping overlay adds the Sheets tab with the provenance chip', async ({ page }) => {
  test.setTimeout(60_000)
  const proofState = makeCatProofState()
  await routeSurfaceConfig(page, proofState, {
    surfaces: { sheets: { chrome: { tab: true } } },
    source: { sha256: 'abcdef0123456789', authored_at: '2026-09-04T00:00:00Z' },
  })

  await page.goto('/app')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 15_000 })

  const sheetsTab = page.getByRole('tab', { name: 'Sheets' })
  await expect(sheetsTab).toBeVisible()
  await expect(page.getByRole('tab')).toHaveCount(5)
  const chip = sheetsTab.getByTestId('surface-config-provenance')
  await expect(chip).toBeVisible()
  await expect(chip).toContainText('surface config authored by Claude · abcdef01')
})

test('an empty overlay renders the frozen four-tab default, no chip', async ({ page }) => {
  test.setTimeout(60_000)
  const proofState = makeCatProofState()
  await routeSurfaceConfig(page, proofState, { surfaces: {} })

  await page.goto('/app')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 15_000 })

  await expect(page.getByRole('tab')).toHaveCount(4)
  await expect(page.getByRole('tab', { name: 'Sheets' })).toHaveCount(0)
  await expect(page.getByTestId('surface-config-provenance')).toHaveCount(0)
})
