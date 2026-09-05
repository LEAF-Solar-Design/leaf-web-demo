import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'
import { setRail } from './local/railFlag.mjs'

// Standardization slice 10d (keyboard rows). Three of the FOUR registry
// actions that carry a real `kbd` cap (web/src/lib/actionRegistry.js: only
// bar:focus/bar:escape/bar:retry/bar:shortcuts have one — never inventing a
// fifth), proven on the console: the key ladder (App.jsx's ladderListener)
// runs the exact same handler its other, non-keyboard trigger already runs.
//   - bar:shortcuts (Shift+?): the SAME onSelect the act-palette's own
//     "Keyboard shortcuts" row runs (paletteActions in App.jsx wires both to
//     `() => setShortcutsOpen(true)`).
//   - bar:retry (R): the SAME onRetryCatalog handler NavRail.jsx's visible
//     "Retry" button already calls — proven by letting R alone recover a
//     failed catalog load with no click.
//   - bar:focus (Mod+K): the SAME command-bar element a direct mouse focus
//     already reaches.
async function installFixture(page, { catalogGate = null } = {}) {
  const state = makeCatProofState()
  await page.addInitScript(() => localStorage.setItem('leaf.jwt', 'fixture-token'))
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (catalogGate && url.pathname === '/api/capabilities' && !catalogGate.succeed) {
      await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: { message: 'catalog unavailable' } }) })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })
}

test('bar:shortcuts — Shift+? runs the exact handler the act-palette row runs', async ({ page }) => {
  test.setTimeout(60_000)
  await setRail(page, '1')
  await installFixture(page)
  await page.goto('/app')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 20_000 })

  const bar = page.getByLabel('Command bar', { exact: true })
  // Baseline: the palette row's own onSelect opens the sheet.
  await bar.click()
  await page.getByRole('button', { name: 'scope ▾' }).click()
  await page.getByRole('option', { name: /^act/ }).click()
  await page.getByRole('option', { name: /^Keyboard shortcuts/ }).click()
  const sheet = page.getByRole('dialog', { name: 'Keyboard shortcuts' })
  await expect(sheet).toBeVisible()
  const rowsFromPalette = await sheet.locator('[data-testid="shortcut-row"]').allTextContents()
  expect(rowsFromPalette.length).toBeGreaterThan(0)
  await page.keyboard.press('Escape')
  await expect(sheet).toHaveCount(0)

  // The key ladder's own cap, from a neutral (non-typing) target, reaches the
  // identical dialog with the identical rows.
  await bar.evaluate((el) => el.blur())
  await expect(bar).not.toBeFocused()
  await page.keyboard.press('Shift+?')
  await expect(sheet).toBeVisible()
  const rowsFromKey = await sheet.locator('[data-testid="shortcut-row"]').allTextContents()
  expect(rowsFromKey).toEqual(rowsFromPalette)
  await page.keyboard.press('Escape')
  await expect(sheet).toHaveCount(0)
})

test('bar:retry — R runs the exact handler the visible Retry button runs', async ({ page }) => {
  test.setTimeout(60_000)
  await setRail(page, '1')
  const catalogGate = { succeed: false }
  await installFixture(page, { catalogGate })
  // The browser surface's NavRail never collapses to a spine (rails.left is
  // 'nav', not 'spine' — productSurfaces.js), so its "Retry" chip is always
  // on screen, unlike the drafting surfaces where the rail hides behind the
  // band by default.
  await page.goto('/app?surface=browser')

  // The one failed-catalog surface: NavRail's own "Retry" chip (onRetryCatalog).
  const retryButton = page.getByRole('button', { name: 'Retry', exact: true })
  await expect(retryButton).toBeVisible({ timeout: 20_000 })
  await expect(page.getByText(/Couldn.t load families/)).toBeVisible()

  // No click on Retry: only the R key ladder rung (rTarget 'catalog') can
  // recover this, so a real recovery here is proof R invoked the same
  // onRetryCatalog handler the button carries.
  catalogGate.succeed = true
  await page.keyboard.press('r')
  await expect(retryButton).toHaveCount(0, { timeout: 15_000 })
  await expect(page.getByText(/Couldn.t load families/)).toHaveCount(0)
})

test('bar:focus — Mod+K reaches the exact command-bar element a direct focus reaches', async ({ page }) => {
  test.setTimeout(60_000)
  await setRail(page, '1')
  await installFixture(page)
  await page.goto('/app')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 20_000 })

  const bar = page.getByLabel('Command bar', { exact: true })
  await bar.click()
  await expect(bar).toBeFocused()
  const focusedByMouse = await page.evaluate(() => document.activeElement?.getAttribute('data-testid'))
  await bar.evaluate((el) => el.blur())
  await expect(bar).not.toBeFocused()

  await page.keyboard.press('Control+K')
  await expect(bar).toBeFocused()
  const focusedByKey = await page.evaluate(() => document.activeElement?.getAttribute('data-testid'))
  expect(focusedByKey).toBe(focusedByMouse)
  expect(focusedByKey).toBe('command-bar')
})
