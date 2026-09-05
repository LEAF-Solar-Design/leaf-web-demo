import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'
import { setRail } from './local/railFlag.mjs'

// Standardization slice 9b/9d (context-menu rows). ElementContextMenu.jsx
// mounts through SurfaceFrame's ContextMenu slot wherever a surface declares
// non-empty `contextMenu` kinds (productSurfaces.js). Two real carriers on
// the console:
//   - a board tile (surface 'browser'; the Catalog tile's family row, the
//     one tile that renders with data-element-id independent of any open
//     workspace project — the Versions/Jobs/Built-tools tiles need one).
//   - the viewer wrapper (surface 'cad', the default; `.viewer-canvas`
//     carries `data-element-id="entity:<handle>"` once a real click-to-pick
//     raycast lands a selection — Viewer.jsx's DEV-only `__cadviewer.project`
//     hook gives the exact screen point for a known world coordinate, the
//     same hook web/e2e/viewer-controls.spec.mjs already relies on).
//
// Shift+F10 opens the menu for the FOCUSED element (ElementContextMenu.jsx
// reads `document.activeElement`); neither the board tile (`<li>`) nor the
// viewer wrapper (`<div>`) carries a tabIndex, so neither is ever the
// focused element for a real user — that is a genuine, disclosed gap, not a
// worked-around one (see the PR body). The keyboard trigger is proven
// instead against a ribbon TOOL button, a real `<button data-element-id
// ="tool:...">`, which is the one console element the app itself makes
// focusable and identity-carrying at once — exactly the carrier
// ElementContextMenu.test.jsx's own unit test uses for its Shift+F10 case.
async function installFixture(page) {
  const state = makeCatProofState()
  await page.addInitScript(() => localStorage.setItem('leaf.jwt', 'fixture-token'))
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })
}

test('a board tile opens the ElementContextMenu via right-click and a long press, filtered by its kind, and Escape closes each', async ({ page }) => {
  test.setTimeout(60_000)
  await setRail(page, '1')
  await installFixture(page)
  await page.goto('/app?surface=browser')

  const tile = page.locator('[data-tile="catalog"] li[data-element-id]').first()
  await expect(tile).toBeVisible({ timeout: 20_000 })
  await expect(tile).toHaveAttribute('data-element-id', /^family:/)

  const menu = page.getByTestId('element-context-menu')
  const askClaude = page.getByTestId('element-context-menu-ask-claude')

  // Right-click: 'family' has no registered registry vocabulary (an honest
  // gap named in ElementContextMenu.jsx's own header), so the menu opens
  // with ONLY the terminal row — filtered by kind means an empty action set
  // here, not a fabricated one.
  await tile.click({ button: 'right' })
  await expect(menu).toBeVisible()
  await expect(askClaude).toHaveAttribute('aria-disabled', 'true')
  await expect(askClaude).toHaveAttribute('data-reason', 'the scoped prompt lands with the change capsule in a later slice')
  await page.keyboard.press('Escape')
  await expect(menu).toHaveCount(0)

  // Long press: a real 500ms+ touch hold on the same tile.
  const box = await tile.boundingBox()
  await tile.dispatchEvent('touchstart', {
    touches: [{ clientX: box.x + box.width / 2, clientY: box.y + box.height / 2, identifier: 0 }],
  })
  await page.waitForTimeout(600)
  await expect(menu).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(menu).toHaveCount(0)
})

test("the viewer wrapper's selected entity opens the ElementContextMenu via right-click and a long press, with every Modify/Clipboard row disabled by the engine's own reason, and Escape closes each", async ({ page }) => {
  test.setTimeout(60_000)
  await setRail(page, '1')
  await installFixture(page)
  await page.goto('/app')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 20_000 })

  const mount = page.locator('.viewer-canvas').first()
  await expect(mount.locator('canvas')).toBeVisible()
  // The fixture's first panel (P0000) is a unit square at world (0,0)-(1,1).
  const point = await mount.evaluate((el) => el.__cadviewer.project(0.5, 0.5))
  await page.mouse.click(point.x, point.y)
  await expect(mount).toHaveAttribute('data-element-id', 'entity:P0000')

  const menu = page.getByTestId('element-context-menu')
  const askClaude = page.getByTestId('element-context-menu-ask-claude')
  const NO_ENGINE = 'no drawing in the browser engine yet'

  await page.mouse.click(point.x, point.y, { button: 'right' })
  await expect(menu).toBeVisible()
  await expect(page.getByRole('menuitem', { name: `delete (unavailable: ${NO_ENGINE})` })).toBeVisible()
  await expect(page.getByRole('menuitem', { name: `copy-clip (unavailable: ${NO_ENGINE})` })).toBeVisible()
  await expect(askClaude).toHaveAttribute('aria-disabled', 'true')
  // Escape closes the menu AND, since nothing higher on ESCAPE_RUNGS is open,
  // also clears the console's own selection (actionRegistry.js's 'selection'
  // rung) — a real, correct interaction, not a test artifact. Re-pick the
  // entity before proving the second trigger.
  await page.keyboard.press('Escape')
  await expect(menu).toHaveCount(0)
  await expect(mount).not.toHaveAttribute('data-element-id', /.+/)
  await page.mouse.click(point.x, point.y)
  await expect(mount).toHaveAttribute('data-element-id', 'entity:P0000')

  await mount.dispatchEvent('touchstart', {
    touches: [{ clientX: point.x, clientY: point.y, identifier: 0 }],
  })
  await page.waitForTimeout(600)
  await expect(menu).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(menu).toHaveCount(0)
})

test('Shift+F10 opens the ElementContextMenu on a focused ribbon tool button, the one identity-carrying element the console makes focusable', async ({ page }) => {
  test.setTimeout(60_000)
  await setRail(page, '1')
  await installFixture(page)
  await page.goto('/app')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 20_000 })
  // The ribbon's default tab is Draw (CockpitTopBand.jsx); Fit lives on View.
  await page.getByRole('tab', { name: 'View', exact: true }).click()

  const fitButton = page.locator('[data-element-id="tool:fit"]').first()
  await expect(fitButton).toBeVisible()
  await fitButton.focus()
  await expect(fitButton).toBeFocused()

  const menu = page.getByTestId('element-context-menu')
  await page.keyboard.press('Shift+F10')
  await expect(menu).toBeVisible()
  await expect(page.getByRole('menuitem', { name: 'fit' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(menu).toHaveCount(0)
})
