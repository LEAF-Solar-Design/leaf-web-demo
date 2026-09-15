import { expect, test } from '@playwright/test'
import { requireLocalReady } from './requireReady.mjs'
import { setRail } from './railFlag.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const conditions = [
  { name: '1366x768', viewport: { width: 1366, height: 768 }, deviceScaleFactor: 1, hasTouch: false },
  { name: '200 percent zoom (800x500 at scale 2)', viewport: { width: 800, height: 500 }, deviceScaleFactor: 2, hasTouch: false },
  { name: '390x844', viewport: { width: 390, height: 844 }, deviceScaleFactor: 1, hasTouch: true },
  { name: '1024x1366', viewport: { width: 1024, height: 1366 }, deviceScaleFactor: 1, hasTouch: true },
]

async function boot(page, request) {
  await requireLocalReady(request, test, API_BASE)
  await setRail(page, '1')
  await page.goto('/app?drawing=cat-panels')
  await expect(page.locator('.studio-shell .viewer-canvas canvas')).toHaveCount(1, { timeout: 30_000 })
  await expect(page.locator('#drafting-ribbon')).toBeVisible()
}

async function openPanels(page) {
  const more = page.getByRole('button', { name: 'More panels', exact: true })
  if (await more.isVisible() && await more.getAttribute('aria-expanded') === 'false') await more.click()
}

async function expectSeparateTabs(page) {
  const ribbon = await page.getByRole('tablist', { name: 'Ribbon', exact: true }).boundingBox()
  const profiles = await page.locator('.tc-product-tabs').boundingBox()
  expect(ribbon).not.toBeNull()
  expect(profiles).not.toBeNull()
  expect(ribbon.y + ribbon.height + 1 <= profiles.y || profiles.y + profiles.height + 1 <= ribbon.y ||
    ribbon.x + ribbon.width + 1 <= profiles.x || profiles.x + profiles.width + 1 <= ribbon.x).toBe(true)
}

for (const condition of conditions) {
  test.describe(condition.name, () => {
    test.use({ viewport: condition.viewport, deviceScaleFactor: condition.deviceScaleFactor, hasTouch: condition.hasTouch })
    test(`cockpit viewports ${condition.name}: panels, keyboard, canvas and touch`, async ({ page, request }) => {
      test.setTimeout(180_000)
      await boot(page, request)
      await expectSeparateTabs(page)
      const ribbon = page.locator('#drafting-ribbon')
      const draw = page.getByRole('tab', { name: 'Draw', exact: true })
      await draw.click()
      await draw.focus()
      await page.keyboard.press('Tab')
      await expect.poll(() => ribbon.evaluate((el) => el.contains(document.activeElement))).toBe(true)
      await openPanels(page)
      const clipboard = ribbon.getByRole('group', { name: 'Clipboard', exact: true })
      await clipboard.scrollIntoViewIfNeeded()
      await expect(clipboard).toBeVisible()
      const lastBox = await clipboard.boundingBox()
      expect(lastBox.x).toBeGreaterThanOrEqual(0)
      expect(lastBox.x + lastBox.width).toBeLessThanOrEqual(condition.viewport.width)
      await page.keyboard.press('Escape')

      const tabs = page.locator('.cockpit-ribbon-tabs [role="tab"]:not(:disabled)')
      for (let tabIndex = 0; tabIndex < await tabs.count(); tabIndex += 1) {
        await tabs.nth(tabIndex).click()
        await tabs.nth(tabIndex).focus()
        await page.keyboard.press('Tab')
        await expect.poll(() => ribbon.evaluate((el) => el.contains(document.activeElement))).toBe(true)
        await openPanels(page)
        const tools = ribbon.locator('.ribbon-tool:not(:disabled)')
        for (let index = 0; index < await tools.count(); index += 1) {
          const tool = tools.nth(index)
          await tool.scrollIntoViewIfNeeded()
          await expect(tool).toBeInViewport({ ratio: 1 })
          await tool.click({ trial: true })
        }
        if (condition.hasTouch) {
          const boxes = await ribbon.locator('.ribbon-tool').evaluateAll((tools) => tools.map((tool) => {
            const { width, height } = tool.getBoundingClientRect()
            return { id: tool.dataset.tool, width, height }
          }))
          for (const box of boxes) {
            expect(box.width, box.id).toBeGreaterThanOrEqual(44)
            expect(box.height, box.id).toBeGreaterThanOrEqual(44)
          }
        }
        await page.keyboard.press('Escape')
      }
      await draw.click()
      await expectSeparateTabs(page)

      const canvas = page.locator('.studio-ground .viewer-canvas canvas')
      if (condition.viewport.width < 981) {
        const activity = await page.locator('.studio-shell .rail-stack').boundingBox()
        const drawing = await canvas.boundingBox()
        const band = await ribbon.boundingBox()
        expect(activity).not.toBeNull()
        expect(activity.y).toBeGreaterThanOrEqual(drawing.y + drawing.height)
        expect(activity.y).toBeGreaterThanOrEqual(band.y + band.height)
      }
      // On narrow screens the readout overlays the drawing. On wide screens
      // its real home is the properties dock, so use the canvas itself there.
      const target = condition.viewport.width < 981
        ? page.locator('.studio-shell .selection-readout .sel-hint, .studio-shell .selection-readout .sel-kind').first()
        : canvas
      await expect(target).toBeVisible()
      const box = await target.boundingBox()
      const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 }
      await canvas.evaluate((el) => {
        el.dataset.viewportPointer = 'pending'
        el.addEventListener('pointerdown', () => { el.dataset.viewportPointer = 'received' }, { once: true })
      })
      if (condition.hasTouch) await page.touchscreen.tap(point.x, point.y)
      else await page.mouse.click(point.x, point.y)
      await expect(canvas).toHaveAttribute('data-viewport-pointer', 'received')
      await expect(page.getByLabel('Command bar', { exact: true })).toBeInViewport()

      if (condition.hasTouch) {
        const toggles = page.locator('.cockpit-status-toggles button')
        for (const box of await toggles.evaluateAll((buttons) => buttons.map((button) => {
          const { width, height } = button.getBoundingClientRect()
          return { width, height }
        }))) {
          expect(box.width).toBeGreaterThanOrEqual(44)
          expect(box.height).toBeGreaterThanOrEqual(44)
        }
      }
      if (condition.viewport.width === 390) {
        await expect(page.locator('[data-toggle="ortho"]')).toBeInViewport({ ratio: 1 })
        await page.setViewportSize({ width: 1024, height: 1366 })
        await page.setViewportSize(condition.viewport)
        await expectSeparateTabs(page)
        await expect(page.locator('[data-toggle="ortho"]')).toBeInViewport({ ratio: 1 })
        expect(await page.locator('.studio-shell').evaluate((el) => el.scrollHeight - el.clientHeight)).toBeLessThanOrEqual(1)
      }
    })
  })
}

for (const hasTouch of [false, true]) test.describe(`reference frame hasTouch=${hasTouch}`, () => {
  test.use({ viewport: { width: 1920, height: 940 }, deviceScaleFactor: 1, hasTouch })
  test(`cockpit viewports 1920x940 preserves the W4e band edges hasTouch=${hasTouch}`, async ({ page, request }) => {
    await boot(page, request)
    const edges = await page.evaluate(() => {
      const rect = (selector) => {
        const { y, height } = document.querySelector(selector).getBoundingClientRect()
        return [Math.round(y), Math.round(height)]
      }
      return [rect('header.top'), rect('#drafting-ribbon'), rect('.viewer-toolbar'), rect('footer.foot-bar')]
    })
    expect(edges).toEqual([[0, 28], [28, 95], [123, 32], [909, 31]])
  })
})
