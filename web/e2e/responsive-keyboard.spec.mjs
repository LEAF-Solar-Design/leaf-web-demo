import { expect, test } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const PROOF_DIR = join(HERE, '..', '..', 'artifacts', 'unified-surface-proof', 'responsive')

async function installFixture(page) {
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })
}

test('the unified scene keeps primary controls reachable across standard viewports', async ({ page }) => {
  test.setTimeout(120_000)
  mkdirSync(PROOF_DIR, { recursive: true })
  await installFixture(page)
  const sizes = [
    ['desktop', 1440, 900],
    ['narrow', 1024, 768],
    ['tablet', 768, 1024],
    ['phone', 390, 844],
    ['short', 1280, 600],
  ]
  for (const [name, width, height] of sizes) {
    await page.setViewportSize({ width, height })
    await page.goto('/try')
    await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
    await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('operator-surface')).toBeVisible()
    await expect(page.locator('.tc-rail-r')).toBeVisible()
    const geometry = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      inputSize: parseFloat(getComputedStyle(document.querySelector('.tc-bar-input')).fontSize),
    }))
    expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.innerWidth)
    if (name === 'phone') expect(geometry.inputSize).toBeGreaterThanOrEqual(16)
    await page.screenshot({ path: join(PROOF_DIR, `${name}.png`), fullPage: true })
  }
})

test('shortcut focus and roving tab arrows operate the visible controls', async ({ page }) => {
  await installFixture(page)
  await page.goto('/try')
  await expect(page.getByTestId('operator-surface')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  const operator = page.getByRole('tab', { name: 'Operator' })
  await operator.focus()
  await page.keyboard.press('ArrowRight')
  const catalog = page.getByRole('tab', { name: /Catalog/ })
  await expect(catalog).toBeFocused()
  await expect(catalog).toHaveAttribute('aria-selected', 'true')
  await page.keyboard.press('Control+K')
  await expect(page.getByLabel('Command bar')).toBeFocused()
  await expect(page.locator('[aria-live="polite"]')).toHaveCount(1)
})
