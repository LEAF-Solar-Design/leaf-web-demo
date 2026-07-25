import { expect, test } from '@playwright/test'
import { join, relative } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')

function polygonCentroid(points) {
  let twiceArea = 0
  let x = 0
  let y = 0
  for (let index = 0; index < points.length; index += 1) {
    const current = points[index]
    const next = points[(index + 1) % points.length]
    const cross = current[0] * next[1] - next[0] * current[1]
    twiceArea += cross
    x += (current[0] + next[0]) * cross
    y += (current[1] + next[1]) * cross
  }
  if (Math.abs(twiceArea) < 0.000001) {
    return {
      x: points.reduce((sum, point) => sum + point[0], 0) / points.length,
      y: points.reduce((sum, point) => sum + point[1], 0) / points.length,
    }
  }
  return { x: x / (3 * twiceArea), y: y / (3 * twiceArea) }
}

test('resident viewer uses the real local drawing for layers, navigation, and picking', async ({ page, request }, testInfo) => {
  const readyResponse = await request.get(`${API_BASE}/api/ready`, { timeout: 3_000 })
  test.skip(!readyResponse.ok(), `real local stack is not ready at ${API_BASE}`)
  test.skip(!(await readyResponse.json())?.ready, `real local stack is not ready at ${API_BASE}`)

  const sessionResponse = await request.get(`${API_BASE}/api/session?dwg=rooftop_demo`, {
    headers: TENANT_HEADERS,
  })
  expect(sessionResponse.status()).toBe(200)
  const session = await sessionResponse.json()
  const intake = session.intake
  expect(Array.isArray(intake?.polylines)).toBe(true)
  const candidates = intake.polylines
    .filter((entity) => (
      entity.handle && entity.layer && entity.closed === true && Array.isArray(entity.pts) && entity.pts.length >= 3
    ))
    .map((entity) => ({ entity, point: polygonCentroid(entity.pts) }))
  expect(candidates.length, 'the real local drawing must contain selectable closed polylines').toBeGreaterThan(0)

  const observed = []
  page.on('response', (response) => {
    if (!response.url().startsWith(API_BASE)) return
    const url = new URL(response.url())
    observed.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  const viewer = page.getByRole('region', { name: 'Drawing viewer' })
  const stage = viewer.locator('.stage-viewer')
  const mount = viewer.locator('.viewer-canvas')
  const canvas = mount.locator('canvas')
  await expect(stage).toHaveClass(/settled/, { timeout: 20_000 })
  await expect(canvas).toHaveCount(1)
  await canvas.evaluate((element) => { element.dataset.residentProof = 'same-canvas' })

  const [leftRail, rightRail, commandBar] = await Promise.all([
    page.locator('.tc-rail-l').boundingBox(),
    page.locator('.tc-rail-r').boundingBox(),
    page.locator('.tc-bar-wrap').boundingBox(),
  ])
  expect(leftRail).toBeTruthy()
  expect(rightRail).toBeTruthy()
  expect(commandBar).toBeTruthy()
  const projectedCandidates = await mount.evaluate(
    (element, entries) => entries.map(({ point }) => element.__cadviewer.project(point.x, point.y)),
    candidates,
  )
  const targetIndex = projectedCandidates.findIndex((point) => (
    point.x > leftRail.x + leftRail.width + 16
    && point.x < rightRail.x - 16
    && point.y > 72
    && point.y < commandBar.y - 16
  ))
  expect(targetIndex, 'a selectable entity must be visible between the unified operator rails').toBeGreaterThanOrEqual(0)
  const target = candidates[targetIndex].entity
  const targetPoint = candidates[targetIndex].point
  const layerCount = intake.polylines.filter((entity) => entity.layer === target.layer).length

  await page.getByRole('tab', { name: 'View' }).click()
  const layerRow = page.locator('.legend-row').filter({ has: page.locator('.legend-name', { hasText: target.layer }) })
  await expect(layerRow).toContainText(layerCount.toLocaleString())
  await expect(layerRow).not.toHaveClass(/off/)

  const baseline = await mount.evaluate((element) => element.__cadviewer.project(0, 0))
  const box = await canvas.boundingBox()
  expect(box).toBeTruthy()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.wheel(0, -700)
  await page.waitForTimeout(250)
  const zoomed = await mount.evaluate((element) => element.__cadviewer.project(0, 0))
  expect(Math.hypot(zoomed.x - baseline.x, zoomed.y - baseline.y)).toBeGreaterThan(5)

  await page.getByRole('button', { name: 'Fit', exact: true }).click()
  const restored = await mount.evaluate((element) => element.__cadviewer.project(0, 0))
  expect(Math.hypot(restored.x - baseline.x, restored.y - baseline.y)).toBeLessThan(2)
  await expect(canvas).toHaveAttribute('data-resident-proof', 'same-canvas')

  const projected = await mount.evaluate(
    (element, point) => element.__cadviewer.project(point.x, point.y),
    targetPoint,
  )
  await page.mouse.move(projected.x, projected.y)
  await page.mouse.down()
  await page.mouse.up()
  const selection = page.locator('.selection-readout')
  await expect(selection).toContainText('Polyline')
  await expect(selection).toContainText(target.handle)
  await expect(selection).toContainText(target.layer)

  const visibleCanvas = await canvas.screenshot()
  await layerRow.click()
  await expect(layerRow).toHaveClass(/off/)
  await expect(selection).toContainText('Click an entity to select it')
  const hiddenCanvas = await canvas.screenshot()
  expect(hiddenCanvas.equals(visibleCanvas)).toBe(false)

  await layerRow.click()
  await expect(layerRow).not.toHaveClass(/off/)

  writeProofReceipt(join(PROOF_DIR, 'viewer-interaction-receipt.json'), {
    capability_ids: ['VW-01', 'VW-02'],
    evidence_tier: 'local-e2e',
    route: '/try',
    runtime: 'real local Vite, FastAPI, cached drawing intake, and resident WebGL viewer',
    api_endpoints: observed,
    artifacts: [
      relative(join(process.cwd(), '..'), testInfo.outputPath('video.webm')).replaceAll('\\', '/'),
    ],
    assertions: [
      'the unified View tab rendered the layer and entity count from the real local intake',
      'wheel zoom changed the resident camera and Fit restored the original bounds',
      'the same WebGL canvas stayed mounted across viewer actions',
      'a click projected from real drawing coordinates selected the expected entity handle',
      'hiding the selected layer changed the rendered canvas and cleared the selection',
      'the layer could be restored without leaving the unified scene',
    ],
    result: {
      verdict: 'pass',
      drawing: intake.dwg || 'rooftop_demo',
      polyline_count: intake.polylines.length,
      selected_handle: target.handle,
      selected_layer: target.layer,
      selected_layer_count: layerCount,
      canvas_retained: true,
      selection_cleared_on_hide: true,
    },
    limitations: [
      'APS_LIVE=0 serves the bundled real local intake rather than extracting a DWG through Autodesk Platform Services.',
      'The proof runs Chromium headless with software WebGL rather than a user desktop GPU.',
      'LEAF_AUTH_LIVE=0 uses the local tenant header instead of an Auth0 identity.',
    ],
  })
})
