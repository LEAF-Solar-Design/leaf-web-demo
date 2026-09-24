import { expect, test } from '@playwright/test'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'

test('viewer camera: an edit keeps a moved view, and a view never moved still fits the drawing', async ({ page, request }) => {
  test.setTimeout(120_000)
  await requireLocalReady(request, test, API_BASE)
  const headDxf = '0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n'
  // Route both heads so live boot cannot open the shared stack's drawing.
  await page.route('**/sample.dxf', (route) => route.fulfill({ status: 200, contentType: 'application/dxf', body: headDxf }))
  await page.route('**/api/drawings/*/dxf*', (route) => route.fulfill({ status: 200, contentType: 'application/dxf', body: headDxf }))
  await page.goto('/app?dev=1')
  await page.getByLabel('Use mock data (off = live backend)').check()
  const ribbon = page.getByTestId('drafting-ribbon')
  const engine = await request.get('/engine/engine.js').catch(() => null)
  await page.getByRole('tab', { name: 'Draw' }).click()
  if (!engine || engine.status() !== 200 || !(await ribbon.locator('[data-group="modify"]').count())) {
    expect(process.env.VITE_CAD_EDIT, 'the managed proof sets VITE_CAD_EDIT=1 and serves the compiled engine, so the camera carry must be exercised').not.toBe('1')
    test.info().annotations.push({ type: 'engine', description: 'compiled engine not served, or flag off; camera carry not exercised' })
    return
  }
  const count = page.getByTestId('cad-edit-entity-count')
  await expect(count).toHaveText('0', { timeout: 60_000 })
  const bar = page.getByLabel('Command bar', { exact: true })
  const drawLine = async (start, end, expected) => {
    await bar.fill('LINE')
    await bar.press('Enter')
    await page.getByLabel('ribbon x', { exact: true }).fill(start)
    const last = page.getByLabel('ribbon x2', { exact: true })
    await last.fill(end)
    await last.press('Enter')
    await expect(count).toHaveText(String(expected), { timeout: 60_000 })
    await page.keyboard.press('Escape')
  }
  const mount = page.locator('.studio-ground .viewer-canvas')
  const getPose = () => mount.evaluate((el) => el.__cadviewer.getPose())
  const expectSamePose = (pose, moved) => {
    expect(pose.position).toEqual(moved.position)
    expect(pose.target).toEqual(moved.target)
    expect(pose.zoom).toBe(moved.zoom)
    expect(pose.worldPerPixel).toBeCloseTo(moved.worldPerPixel, 9)
  }

  // 1-2. A view nobody moved refits to the grown drawing on every edit.
  await drawLine('0,0', '10,0', 1)
  const pose0 = await getPose()
  await drawLine('0,0', '40,30', 2)
  const pose1 = await getPose()
  expect(pose1.target).not.toEqual(pose0.target)

  // 3. The drafter moves the view.
  expect(await mount.evaluate((el) => el.__cadviewer.setView({ center: { x: 3, y: 2 }, zoom: 2.5 }))).toBe(true)
  const poseM = await getPose()
  expect(poseM.zoom).toBe(2.5)
  expect(poseM.target[0]).toBe(3)
  expect(poseM.target[1]).toBe(2)

  // 4. An edit to the same document keeps the moved view (main refits here: zoom returns to 1).
  await drawLine('0,0', '-50,-50', 3)
  expectSamePose(await getPose(), poseM)

  // 5. An undo reloads the same document and keeps the moved view too.
  await bar.fill('u')
  await bar.press('Enter')
  await expect(count).toHaveText('2', { timeout: 60_000 })
  expectSamePose(await getPose(), poseM)
})
