import { expect, test } from '@playwright/test'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const viewports = [
  { width: 1280, height: 800 },
  { width: 1366, height: 768 },
  { width: 1600, height: 1000 },
  { width: 1920, height: 940 },
]

for (const surface of ['solar', 'cad']) {
  for (const { width, height } of viewports) {
    test(`header fit ${surface} ${width}x${height}`, async ({ page, request }) => {
      await requireLocalReady(request, test, API_BASE)
      await page.setViewportSize({ width, height })
      await page.goto(`/app?surface=${surface}&drawing=cat-panels`)
      const shell = '.studio-shell .app[data-studio-shell="cockpit"][data-surface="' + surface + '"]'
      await expect(page.locator(shell)).toHaveCount(1)
      await expect(page.locator(`${shell} .cockpit-ribbon-tabs`)).toBeVisible({ timeout: 30000 })
      await page.evaluate(() => new Promise((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(resolve))
      }))

      const boxes = await page.locator(
        `${shell} header.top button, ${shell} .cockpit-ribbon-tabs [role="tab"], ${shell} .tc-product-tabs [role="tab"]`,
      ).evaluateAll((elements) => elements.filter((element) => element.checkVisibility()).map((element) => {
        const { x, y, width, height } = element.getBoundingClientRect()
        const label = element.getAttribute('aria-label') || element.getAttribute('title') || element.textContent.trim()
        const enabled = !element.matches(':disabled, [aria-disabled="true"]')
        const hit = document.elementFromPoint(x + width / 2, y + height / 2)
        return { x, y, width, height, label, enabled, centreHits: element.contains(hit) }
      }))
      expect(boxes.length).toBeGreaterThanOrEqual(8)
      for (let i = 0; i < boxes.length; i += 1) {
        const a = boxes[i]
        for (let j = i + 1; j < boxes.length; j += 1) {
          const b = boxes[j]
          const overlapX = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x)
          const overlapY = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y)
          expect(overlapX > 0.5 && overlapY > 0.5, `${a.label} overlaps ${b.label}`).toBe(false)
        }
        if (a.enabled) expect(a.centreHits, `${a.label} is covered at its centre`).toBe(true)
      }

      const header = await page.locator(`${shell} header.top`).boundingBox()
      const nav = await page.locator(`${shell} .tc-product-nav`).boundingBox()
      expect(header).not.toBeNull()
      expect(nav).not.toBeNull()
      expect(nav.y).toBeGreaterThanOrEqual(header.y + header.height - 0.5)
      expect(Math.abs(nav.height - 30)).toBeLessThanOrEqual(1)
    })
  }
}
