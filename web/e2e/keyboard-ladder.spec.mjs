import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

test('Escape dismisses a proposal before a second Escape leaves the unified scene', async ({ page }) => {
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByRole('tab', { name: /Catalog/ }).click()
  await page.getByRole('button', { name: /count-panels/i }).click()
  await page.getByRole('button', { name: 'Review & run' }).click()
  await expect(page.getByRole('button', { name: 'Run count-panels' })).toBeVisible()

  await page.keyboard.press('Escape')
  await expect(page).toHaveURL(/\/try$/)
  await expect(page.getByRole('button', { name: 'Run count-panels' })).toHaveCount(0)
  await expect(page.locator('.route')).toHaveCount(0)
  const command = page.getByLabel('Command bar', { exact: true })
  await expect(command).toBeFocused()

  // The proposal dismiss refocuses the bar on the next animation frame
  // (ToolCast's dismissProposalOnEscape), which can land after a blur made
  // the moment toBeFocused passed. Blur until it holds across two frames, so
  // the second Escape starts off the field.
  await expect(async () => {
    await command.evaluate((element) => element.blur())
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    await expect(command).not.toBeFocused({ timeout: 0 })
  }).toPass({ timeout: 5_000 })
  await page.keyboard.press('Escape')
  await expect(page).toHaveURL(/\/$/)
})

test('data-instant is stamped only when the global ladder handles a hotkey', async ({ page }) => {
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })

  const unhandledRetryStamped = await page.evaluate(() => {
    delete document.documentElement.dataset.instant
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'r' }))
    return document.documentElement.dataset.instant === '1'
  })
  expect(unhandledRetryStamped).toBe(false)

  const focusHotkeyStamped = await page.evaluate(() => {
    delete document.documentElement.dataset.instant
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }))
    return document.documentElement.dataset.instant === '1'
  })
  expect(focusHotkeyStamped).toBe(true)
})

// S25: Ctrl+Z steps the drawing's versions off the drafting surface (ToolCast's
// listener on the registry's versionShortcutDecision). The probe reads only
// whether the key was claimed; with no version to step nothing is undone.
test('Ctrl+Z is claimed off the drafting surface and yields to a field and a visible engine document', async ({ page }) => {
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await expect(page.locator('[data-engine-document]:visible')).toHaveCount(0)

  const probe = await page.evaluate(() => {
    const fire = (target, init = {}) => {
      const event = new KeyboardEvent('keydown', { key: 'z', ctrlKey: true, bubbles: true, cancelable: true, ...init })
      target.dispatchEvent(event)
      return event.defaultPrevented
    }
    document.activeElement?.blur?.()
    const neutral = fire(document.body)
    const redo = fire(document.body, { key: 'Z', shiftKey: true })
    const withAlt = fire(document.body, { altKey: true })
    const field = document.createElement('input')
    document.body.appendChild(field)
    field.focus()
    const inField = fire(field)
    field.remove()
    const engine = document.createElement('div')
    engine.setAttribute('data-engine-document', 'probe.dxf')
    document.body.appendChild(engine)
    const onCanvas = fire(document.body)
    engine.remove()
    return { neutral, redo, withAlt, inField, onCanvas }
  })
  expect(probe).toEqual({ neutral: true, redo: true, withAlt: false, inField: false, onCanvas: false })
})
