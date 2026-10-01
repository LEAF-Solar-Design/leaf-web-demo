import AxeBuilder from '@axe-core/playwright'
import { test, expect, COACH_STORAGE_KEY } from '../fixtures.mjs'

test.use({ firstRun: true })

test('first-run-open @desktop', { tag: ['@smoke', '@journey'] }, async ({ page, walkEvidence }) => {
  const started = performance.now()
  const step = async (name, action) => {
    const begin = performance.now()
    try { await test.step(name, action) } finally {
      walkEvidence.steps.push({ name, elapsedMs: performance.now() - begin })
    }
  }
  try {
    await step('Land as a first-time visitor', async () => {
      // StageLayer starts this real preview fetch on mount. The sample
      // button reloads the document; let the response body finish first so
      // that navigation does not cancel a request we are still measuring.
      const [previewSolve] = await Promise.all([
        page.waitForResponse((response) => response.request().method() === 'GET'
          && new URL(response.url()).pathname === '/api/site/demo-solve'),
        page.goto('/try'),
      ])
      expect(previewSolve.ok(), 'the preview solve must load successfully').toBe(true)
      expect(await previewSolve.finished(), 'the preview solve must finish before leaving /try').toBeNull()
      await expect(page.getByRole('dialog', { name: 'Try the command bar' })).toBeVisible()
      expect(await page.evaluate((key) => localStorage.getItem(key), COACH_STORAGE_KEY)).toBeNull()
    })
    await step('Dismiss the first-run coach', async () => {
      await page.getByRole('dialog', { name: 'Try the command bar' }).getByRole('button', { name: 'Got it', exact: true }).click()
      await expect(page.getByRole('dialog', { name: 'Try the command bar' })).toHaveCount(0)
      expect(await page.evaluate((key) => localStorage.getItem(key), COACH_STORAGE_KEY)).toBe('1')
    })
    await step('Open the offered sample rooftop', async () => {
      await page.getByRole('button', { name: 'Open the sample rooftop', exact: true }).click()
      await expect(page).toHaveURL(/\/try\?demo=1$/)
    })
    await step('See the drawing in the viewer', async () => {
      // Resolve the locator on every poll: opening the engine head replaces
      // the canvas, so an ElementHandle or an earlier box is not evidence.
      await expect.poll(async () => {
        const canvas = page.getByRole('region', { name: 'Drawing', exact: true }).locator('canvas:visible')
        const box = await canvas.boundingBox().catch(() => null)
        return !!box && box.width > 100 && box.height > 100
      }, { timeout: 60_000 }).toBe(true)
      // The studio exposes drawing facts in Properties; the legacy overview
      // paragraph remains mounted but hidden in this workspace.
      const properties = page.getByRole('complementary', { name: 'Properties', exact: true })
      await expect(properties.getByText('rooftop_demo.dwg', { exact: true })).toBeVisible({ timeout: 60_000 })
      const polylines = properties.getByText('Polylines', { exact: true }).locator('+ dd')
      await expect(polylines).toBeVisible()
      await expect(polylines).toHaveText(/^[1-9][\d,]*$/)
      const drawing = page.getByRole('tab', { name: 'CAD', exact: true })
      await expect(drawing).toHaveAttribute('aria-selected', 'true')
      await expect(page.getByRole('combobox', { name: 'Command bar', exact: true })).toBeVisible()
    })
    walkEvidence.timeToTaskMs = performance.now() - started
    await page.bringToFront()
    expect(await page.evaluate(() => innerWidth > 0 && innerHeight > 0)).toBe(true)
    const accessibility = await new AxeBuilder({ page }).analyze()
    walkEvidence.accessibility = { violations: accessibility.violations, incomplete: accessibility.incomplete }
    expect(walkEvidence.consoleErrors, 'the sample-opening journey must produce no console errors').toEqual([])
    expect(walkEvidence.pageErrors, 'the sample-opening journey must produce no page errors').toEqual([])
    expect(walkEvidence.failedRequests, 'the sample-opening journey must produce no failed requests').toEqual([])
  } finally {
    walkEvidence.stepCount = walkEvidence.steps.length
    walkEvidence.elapsedMs = performance.now() - started
  }
})
