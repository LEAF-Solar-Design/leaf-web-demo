import { expect, test } from '@playwright/test'
import { REQUEST, catProofResponse, makeCatProofState } from './catProofFixture.mjs'

async function install(page) {
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const body = request.postData() ? request.postDataJSON() : {}
    const result = catProofResponse({ method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })
}

test('the named resident viewer supports pan and zoom and Fit restores its bounds', async ({ page }) => {
  await install(page)
  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  const viewer = page.getByRole('region', { name: 'Drawing viewer' })
  const mount = viewer.locator('.viewer-canvas')
  const canvas = mount.locator('canvas')
  await expect(canvas).toHaveCount(1)
  await expect(viewer.locator('.stage-viewer')).toHaveClass(/settled/, { timeout: 20_000 })
  await canvas.evaluate((element) => { element.dataset.residentProof = 'same-canvas' })
  const baseline = await mount.evaluate((element) => element.__cadviewer.project(0, 0))
  const box = await canvas.boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.wheel(0, -700)
  await page.waitForTimeout(250)
  const zoomed = await mount.evaluate((element) => element.__cadviewer.project(0, 0))
  expect(Math.hypot(zoomed.x - baseline.x, zoomed.y - baseline.y)).toBeGreaterThan(5)

  await page.getByRole('tab', { name: 'View' }).click()
  await page.getByRole('button', { name: 'Fit', exact: true }).click()
  const restored = await mount.evaluate((element) => element.__cadviewer.project(0, 0))
  expect(Math.hypot(restored.x - baseline.x, restored.y - baseline.y)).toBeLessThan(2)
  await expect(canvas).toHaveAttribute('data-resident-proof', 'same-canvas')
})

test('the Version 2 sculpture can orbit at the deployed acceptance viewport', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  await install(page)
  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByLabel('Command bar').fill(REQUEST)
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  const approval = page.locator('.converse-confirm').filter({ hasText: 'arrange-panels-as-cat' })
  await approval.getByRole('button', { name: 'Approve' }).click()
  await expect(page.getByTestId('version-head')).toContainText('Version 2', { timeout: 15_000 })

  await page.getByRole('tab', { name: 'View', exact: true }).click()
  await expect(page.getByTestId('camera-controls')).toBeVisible()
  await page.getByTestId('focus-3d').click()
  const mount = page.getByRole('region', { name: 'Drawing viewer' }).locator(
    '.viewer-canvas[data-view-mode="panel-sculpture"][data-camera-position][data-camera-target]',
  )
  await expect(mount).toBeVisible()
  const canvas = mount.locator('canvas')
  await expect(canvas).toBeVisible()
  const before = await mount.evaluate((element) =>
    `${element.dataset.cameraPosition}|${element.dataset.cameraTarget}`,
  )
  const box = await canvas.boundingBox()
  await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.5)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width * 0.7, box.y + box.height * 0.4, { steps: 8 })
  await page.mouse.up()
  await expect.poll(() => mount.evaluate((element) =>
    `${element.dataset.cameraPosition}|${element.dataset.cameraTarget}`,
  )).not.toBe(before)
})

test('WebGL loss keeps a 2D drawing visible and offers a real retry', async ({ page }) => {
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext
    window.__webglAttempts = 0
    HTMLCanvasElement.prototype.getContext = function patched(type, ...args) {
      if (String(type).includes('webgl')) {
        window.__webglAttempts += 1
        return null
      }
      return original.call(this, type, ...args)
    }
  })
  await install(page)
  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  const notice = page.getByRole('alert').filter({ hasText: 'Interactive viewer unavailable' })
  await expect(notice).toContainText('A 2D drawing remains visible.')
  await expect(page.locator('.stage-fallback2d canvas')).toBeVisible()
  const before = await page.evaluate(() => window.__webglAttempts)
  await notice.getByRole('button', { name: 'Retry viewer' }).click()
  await expect.poll(() => page.evaluate(() => window.__webglAttempts)).toBeGreaterThan(before)
  await expect(page.locator('.stage-fallback2d canvas')).toBeVisible()
})
