import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

// Studio lane L: GRID in the drafting status bar is real. It is owned by the
// viewer (ViewerGrid in site/DrawingCockpit.jsx), travels the same
// cockpit:modes path as ORTHO and OSNAP, and draws one layer in the drawing
// ground behind the transparent canvas, anchored to world (0, 0) and repainted
// from the camera. SNAP and POLAR stay disabled with their reason.

async function install(page) {
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const body = request.postData() ? request.postDataJSON() : {}
    const result = catProofResponse({ method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, state)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })
}

// Where world (0, 0) lands inside the layer, against the offsets the layer
// actually paints (the third background layer is the minor X/Y tile).
async function anchoring(page) {
  return page.evaluate(() => {
    const layer = document.querySelector('.studio-ground > [data-testid="viewer-grid"]')
    const mount = document.querySelector('.studio-ground .viewer-canvas')
    const origin = mount.__cadviewer.project(0, 0)
    const rect = layer.getBoundingClientRect()
    const sizes = layer.style.backgroundSize.split(',').map((part) => parseFloat(part))
    const positions = layer.style.backgroundPosition.split(',').map((part) => part.trim().split(/\s+/).map((v) => parseFloat(v)))
    return { origin, rect: { left: rect.left, top: rect.top }, minorPx: sizes[2], majorPx: sizes[0], minor: positions[2], major: positions[0], step: layer.getAttribute('data-grid-step') }
  })
}

function expectAnchored({ origin, rect, minorPx, majorPx, minor, major }) {
  const near = (value, offset, size) => {
    const expected = (((value % size) + size) % size)
    const diff = Math.abs(expected - offset)
    return Math.min(diff, size - diff)
  }
  expect(minorPx).toBeGreaterThanOrEqual(12)
  expect(majorPx).toBeCloseTo(minorPx * 5, 3)
  expect(near(origin.x - rect.left, minor[0], minorPx)).toBeLessThan(0.75)
  expect(near(origin.y - rect.top, minor[1], minorPx)).toBeLessThan(0.75)
  expect(near(origin.x - rect.left, major[0], majorPx)).toBeLessThan(0.75)
  expect(near(origin.y - rect.top, major[1], majorPx)).toBeLessThan(0.75)
}

test('the grid toggle shows and hides a camera-anchored grid behind the drawing; snap and polar stay disabled', async ({ page }) => {
  test.setTimeout(90_000)
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await install(page)
  await page.goto('/app')
  await expect(page.locator('.studio-ground .viewer-canvas canvas')).toHaveCount(1, { timeout: 30_000 })

  const grid = page.locator('[data-toggle="grid"]')
  const layer = page.locator('.studio-ground > [data-testid="viewer-grid"]')
  await expect(grid).toBeEnabled({ timeout: 30_000 })
  await expect(grid).toHaveAttribute('aria-pressed', 'false')
  await expect(grid).toHaveAttribute('aria-label', 'Grid display')
  await expect(grid).toHaveAttribute('title', 'Grid display off. Shows a reference grid behind the drawing.')
  await expect(layer).toHaveCount(0)

  for (const id of ['snap', 'polar']) {
    const toggle = page.locator(`[data-toggle="${id}"]`)
    await expect(toggle).toBeDisabled()
    await expect(toggle).toHaveAttribute('aria-label', /\(unavailable: not in the browser viewer yet\)$/)
    await expect(toggle).toHaveAttribute('title', /Not in the browser viewer yet\.$/)
    await expect(toggle).not.toHaveAttribute('aria-pressed', /.*/)
  }

  await grid.click()
  await expect(grid).toHaveAttribute('aria-pressed', 'true')
  await expect(grid).toHaveAttribute('title', 'Grid display on. Shows a reference grid behind the drawing.')
  await expect(layer).toHaveCount(1)
  await expect(layer).toBeVisible()
  await expect(layer).toHaveAttribute('data-grid-step', /^\d/, { timeout: 20_000 })

  const style = await layer.evaluate((element) => {
    const computed = getComputedStyle(element)
    const box = element.getBoundingClientRect()
    const canvas = document.querySelector('.studio-ground .viewer-canvas canvas').getBoundingClientRect()
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2)
    return {
      image: computed.backgroundImage,
      zIndex: computed.zIndex,
      pointerEvents: computed.pointerEvents,
      coversCanvas: box.left <= canvas.left + 1 && box.top <= canvas.top + 1
        && box.right >= canvas.right - 1 && box.bottom >= canvas.bottom - 1,
      hitIsLayer: hit === element,
    }
  })
  expect(style.image).toContain('linear-gradient')
  expect(style.zIndex).toBe('-1')
  expect(style.pointerEvents).toBe('none')
  expect(style.coversCanvas).toBe(true)
  expect(style.hitIsLayer).toBe(false)

  const first = await anchoring(page)
  expectAnchored(first)

  // Zoom the real camera through the view strip: the grid repaints from the
  // camera channel and stays anchored to world (0, 0) at the new scale.
  const view = page.getByTestId('cockpit-view')
  await view.getByRole('button', { name: 'Zoom in', exact: true }).click()
  await view.getByRole('button', { name: 'Zoom in', exact: true }).click()
  const scaleKey = (a) => `${a.step}|${a.minorPx.toFixed(3)}`
  await expect.poll(async () => scaleKey(await anchoring(page))).not.toBe(scaleKey(first))
  expectAnchored(await anchoring(page))

  await grid.click()
  await expect(grid).toHaveAttribute('aria-pressed', 'false')
  await expect(layer).toHaveCount(0)
  await expect(page.locator('[data-toggle="snap"]')).toBeDisabled()
  await expect(page.locator('[data-toggle="polar"]')).toBeDisabled()
  expect(pageErrors.filter((message) => /grid/i.test(message))).toEqual([])
})
