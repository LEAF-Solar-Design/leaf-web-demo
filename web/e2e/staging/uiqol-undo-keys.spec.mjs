import { expect, test } from '@playwright/test'
import { assertPageOnAllowedOrigin, stagingProofPath } from './stagingConfig.mjs'
import { captureStagingIdentity } from './stagingIdentity.mjs'

// uiqol S25 changed-behaviour check against the deployed staging origin, run
// after the D4 identity probe proves the candidate source is live. Two halves:
//   1. Single-key shortcuts switch: turned off in the shortcut sheet, Shift+?
//      opens no sheet (Mod+K still focuses the bar). Fork
//      F-studio-rollback-storage: the switch is remembered in localStorage, so
//      the check restores it on the way out.
//   2. Version Mod+Z off the drafting surface: the console's key ladder claims
//      Ctrl+Z on a neutral target and leaves it alone in a field. Nothing is
//      undone (a signed-out visitor has no version to step), so the probe reads
//      only whether the key was claimed and never mutates a drawing.
// When the console shell is absent for a signed-out visitor the check is the
// identity probe only, and it says so.

const SINGLE_KEY_SHORTCUTS_KEY = 'leaf.singleKeyShortcuts'

test.use({ viewport: { width: 1600, height: 1000 } })

test('single-key shortcuts switch off stops Shift+?, and Ctrl+Z is claimed off the drafting surface', async ({ page, request }, testInfo) => {
  const identity = await captureStagingIdentity(request)
  await testInfo.attach('staging-identity', {
    body: JSON.stringify(identity, null, 2),
    contentType: 'application/json',
  })
  await page.goto('/app?demo=off', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  expect(new URL(page.url()).pathname).toBe('/app')

  await page.evaluate(() => document.activeElement?.blur?.())
  await page.keyboard.press('Shift+?')
  const sheet = page.getByRole('dialog', { name: 'Keyboard shortcuts' })
  if (await sheet.count() === 0) {
    testInfo.annotations.push({ type: 'identity-only', description: 'the console key ladder is absent signed out' })
    return
  }

  try {
    const toggle = sheet.getByRole('switch', { name: 'Single-key shortcuts' })
    await expect(toggle).toHaveAttribute('aria-checked', 'true')
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-checked', 'false')
    await page.screenshot({ path: stagingProofPath('uiqol-undo-keys', 'single-key-off.png') })
    await page.keyboard.press('Escape')
    await expect(sheet).toHaveCount(0)

    await page.evaluate(() => document.activeElement?.blur?.())
    await page.keyboard.press('Shift+?')
    await expect(sheet).toHaveCount(0)

    // Version half: claimed on a neutral target, left alone in a field, and
    // only when no engine document is on screen (on the canvas Mod+Z is the
    // engine's own undo).
    const probe = await page.evaluate(() => {
      const visibleEngine = [...document.querySelectorAll('[data-engine-document]')]
        .some((node) => node.getClientRects().length > 0)
      const fire = (target) => {
        const event = new KeyboardEvent('keydown', { key: 'z', ctrlKey: true, bubbles: true, cancelable: true })
        target.dispatchEvent(event)
        return event.defaultPrevented
      }
      document.activeElement?.blur?.()
      const neutral = fire(document.body)
      const field = document.createElement('input')
      document.body.appendChild(field)
      field.focus()
      const inField = fire(field)
      field.remove()
      return { visibleEngine, neutral, inField }
    })
    await testInfo.attach('version-mod-z-probe', { body: JSON.stringify(probe, null, 2), contentType: 'application/json' })
    expect(probe.inField).toBe(false)
    if (probe.visibleEngine) {
      testInfo.annotations.push({ type: 'version-half-skipped', description: 'an engine document is on screen, Mod+Z is the engine\'s' })
    } else {
      expect(probe.neutral).toBe(true)
    }
  } finally {
    await page.evaluate((key) => localStorage.removeItem(key), SINGLE_KEY_SHORTCUTS_KEY)
  }
})
