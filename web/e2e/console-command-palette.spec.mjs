import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'
import { setRail } from './local/railFlag.mjs'

// Standardization slice 10d (palette row). The console's command bar
// (web/src/components/PromptBox.jsx) has no scope of its own until the
// "scope ▾" chip picks 'act' — Mod+K only focuses the bar (actionRegistry.js
// `bar:focus`), it does not itself open a resolver. This row proves the real
// sequence: Mod+K reaches the one command bar, the act scope shows the
// palette rows built over the action registry (web/src/lib/palette.js
// `actionPaletteRows`, fed by App.jsx's `paletteActions`), typing filters
// them, Enter runs the highlighted row or leaves a disabled one alone (the
// honesty-ladder reason is the outcome, never a crash or a silent run), and
// Escape closes the resolver.
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

test('Ctrl/Cmd+K focuses the command bar, and the act scope shows palette rows over the action registry', async ({ page }) => {
  test.setTimeout(60_000)
  await setRail(page, '1')
  await installFixture(page)
  await page.goto('/app')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 20_000 })

  const bar = page.getByLabel('Command bar', { exact: true })
  await expect(bar).not.toBeFocused()
  await page.keyboard.press('Control+K')
  await expect(bar).toBeFocused()

  // The scope chip is the real path to the act-scope palette (Mod+K alone
  // never opens it — see the header note).
  await page.getByRole('button', { name: 'scope ▾' }).click()
  await expect(page.getByRole('listbox', { name: 'Scope' })).toBeVisible()
  await page.getByRole('option', { name: /^act/ }).click()

  const palette = page.getByRole('listbox', { name: 'Actions and artifacts' })
  await expect(palette).toBeVisible()
  // A real, always-live registry row: View cluster's Fit action.
  await expect(page.getByRole('option', { name: /^fit\b/ })).toBeVisible()

  // Typing filters the rows down to the one match.
  await bar.fill('shortcut')
  await expect(page.getByRole('option', { name: /^fit\b/ })).toHaveCount(0)
  const shortcutsRow = page.getByRole('option', { name: /^Keyboard shortcuts/ })
  await expect(shortcutsRow).toBeVisible()

  // Enter runs the highlighted (only) match.
  await bar.press('Enter')
  await expect(page.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeVisible()
  // ShortcutSheet closes on its own Escape (component-local, capture phase),
  // not the global bar:escape ladder rung.
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: 'Keyboard shortcuts' })).toHaveCount(0)

  // A disabled row's honest reason: fresh boot, nothing to undo yet
  // (REASONS.nothingToUndo in actionRegistry.js). Enter must not run it.
  await page.getByRole('button', { name: 'scope ▾' }).click()
  await page.getByRole('option', { name: /^act/ }).click()
  await bar.fill('undo')
  const undoRow = page.getByRole('option', { name: /^undo/ })
  await expect(undoRow).toContainText('unavailable: nothing to undo')
  await expect(undoRow).toHaveAttribute('aria-disabled', 'true')
  await bar.press('Enter')
  // Disabled: nothing ran, and the palette stays open (no crash, no navigation).
  await expect(page.getByRole('listbox', { name: 'Actions and artifacts' })).toBeVisible()
  await expect(page.locator('.toast')).toHaveCount(0)

  // Escape closes the resolver.
  await bar.press('Escape')
  await expect(page.getByRole('listbox', { name: 'Actions and artifacts' })).toHaveCount(0)
})
