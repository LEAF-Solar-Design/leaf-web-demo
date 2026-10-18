import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { createHash } from 'node:crypto'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const DRAWING = join(process.cwd(), 'e2e', 'fixtures', 'distinctive-panel.dxf')
const REQUEST_TIMEOUT = 15_000

function flagsOn(testInfo) {
  if (process.env.VITE_CAD_EDIT === '1' && process.env.VITE_SOLAR_FLOW_RAIL === '1' && process.env.VITE_SOLAR_SETTINGS_FORM === '1') return true
  throw new Error('Seat proof requires VITE_CAD_EDIT=1, VITE_SOLAR_FLOW_RAIL=1 and VITE_SOLAR_SETTINGS_FORM=1; flag-off is not evidence.')
}

// Upload and extraction wait copied from solar-local-graph-commit.spec.mjs.
async function openSolarDrawing(page) {
  const api = page.request
  await requireLocalReady(api, test, API_BASE)
  const readJson = async (path) => {
    const response = await api.get(`${API_BASE}${path}`, { headers: TENANT_HEADERS, timeout: REQUEST_TIMEOUT })
    expect(response.status(), `GET ${path}`).toBe(200)
    return response.json()
  }
  const upload = await api.post(`${API_BASE}/api/drawings/upload`, {
    headers: TENANT_HEADERS,
    multipart: { file: { name: 'distinctive-panel.dxf', mimeType: 'application/dxf', buffer: readFileSync(DRAWING) } },
    timeout: REQUEST_TIMEOUT,
  })
  expect(upload.status()).toBe(202)
  const receipt = await upload.json()
  expect(receipt).toMatchObject({ tenant_id: 'demo-tenant', tenant_kind: 'account' })
  const drawingId = receipt.drawing_id
  expect(typeof drawingId).toBe('string')
  await expect.poll(async () => (await readJson(`/api/drawings/${drawingId}/upload-status`)).status, {
    timeout: 30_000, message: 'upload extraction settles',
  }).toMatch(/^(ready|failed)$/)
  expect((await readJson(`/api/drawings/${drawingId}/upload-status`)).status).toBe('ready')
  await page.addInitScript((id) => {
    sessionStorage.setItem('leaf.cat.workbench.id.v1', id)
  }, drawingId)
  await page.goto(`/app?surface=solar&drawing=${encodeURIComponent(drawingId)}`)
  await expect(page.locator('.workspace-card')).toHaveAttribute('data-engine-document', `${drawingId}-v1.dxf`, { timeout: 60_000 })
  await expect(page.locator('.studio-ground .viewer-canvas canvas')).toHaveCount(1, { timeout: 30_000 })
  await expect(page.getByRole('tab', { name: 'Solar CAD', exact: true })).toHaveAttribute('aria-selected', 'true')
  const seat = page.getByTestId('solar-flow-seat')
  await expect(seat).toHaveCount(1)
  await expect(seat).toBeVisible()
  return seat
}

async function openNarrowSolarDrawing(page) {
  const api = page.request
  await requireLocalReady(api, test, API_BASE)
  const readJson = async (path) => {
    const response = await api.get(`${API_BASE}${path}`, { headers: TENANT_HEADERS, timeout: REQUEST_TIMEOUT })
    expect(response.status(), `GET ${path}`).toBe(200)
    return response.json()
  }
  const upload = await api.post(`${API_BASE}/api/drawings/upload`, {
    headers: TENANT_HEADERS,
    multipart: { file: { name: 'distinctive-panel.dxf', mimeType: 'application/dxf', buffer: readFileSync(DRAWING) } },
    timeout: REQUEST_TIMEOUT,
  })
  expect(upload.status()).toBe(202)
  const receipt = await upload.json()
  expect(receipt).toMatchObject({ tenant_id: 'demo-tenant', tenant_kind: 'account' })
  const drawingId = receipt.drawing_id
  expect(typeof drawingId).toBe('string')
  await expect.poll(async () => (await readJson(`/api/drawings/${drawingId}/upload-status`)).status, {
    timeout: 30_000, message: 'upload extraction settles',
  }).toMatch(/^(ready|failed)$/)
  expect((await readJson(`/api/drawings/${drawingId}/upload-status`)).status).toBe('ready')
  await page.addInitScript((id) => {
    sessionStorage.setItem('leaf.cat.workbench.id.v1', id)
  }, drawingId)
  await page.goto(`/app?surface=solar&drawing=${encodeURIComponent(drawingId)}`)
  await expect(page.locator('.workspace-card')).toHaveAttribute('data-engine-document', `${drawingId}-v1.dxf`, { timeout: 60_000 })
  await expect(page.locator('.studio-ground .viewer-canvas canvas')).toHaveCount(1, { timeout: 30_000 })
  await expect(page.getByRole('tab', { name: 'Solar CAD', exact: true })).toHaveAttribute('aria-selected', 'true')
  const seat = page.getByTestId('solar-flow-seat')
  await expect(seat).toHaveCount(1)
  return seat
}

async function ownsCentre(control) {
  await expect(control).toBeVisible()
  expect(await control.evaluate((element) => {
    const r = element.getBoundingClientRect()
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
    return hit === element || element.contains(hit)
  })).toBe(true)
}

async function noHorizontalOverflow(seat) {
  expect(await seat.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
}

async function openLandxml(page, seat) {
  const select = page.getByTestId('solar-flow-select')
  await select.scrollIntoViewIfNeeded()
  await ownsCentre(select)
  await select.selectOption('ground-physical')
  const trigger = seat.getByRole('button', { name: 'Import LandXML terrain', exact: true })
  await trigger.scrollIntoViewIfNeeded()
  await ownsCentre(trigger)
  await trigger.click()
  const panel = page.getByTestId('solar-landxml-upload')
  await expect(panel).toBeVisible()
  const file = panel.locator('input[type="file"]')
  await file.scrollIntoViewIfNeeded()
  await ownsCentre(file)
  await noHorizontalOverflow(seat)
}

async function expandSeat(seat) {
  const toggle = seat.getByRole('button', { name: 'Solar flow tools', exact: true })
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await toggle.focus()
  await toggle.press('Enter')
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
}

// Replay on the root-managed, source-bound Vite/API stack, with the three
// VITE flags above and LEAF_E2E_API_BASE matching that stack. No mocked heads,
// camera movement, selected-id injection, or forced DOM clicks are used here.
for (const op of ['move', 'rotate', 'arrayRect']) {
  test(`SEAT selection ${op} starts with an ordinary drawing click`, async ({ page }, testInfo) => {
    flagsOn(testInfo)
    test.setTimeout(120_000)
    await page.setViewportSize({ width: 1280, height: 800 })
    const seat = await openSolarDrawing(page)
    const compact = await seat.boundingBox()
    await expect(seat).toHaveAttribute('data-expanded', 'false')
    const mount = page.locator('.studio-ground .viewer-canvas')
    await expect.poll(() => mount.evaluate((element) => typeof element.__cadviewer?.project)).toBe('function')
    const pose = () => mount.evaluate((element) => ({ position: element.dataset.cameraPosition, target: element.dataset.cameraTarget }))
    const initialPose = await pose()
    const properties = page.getByTestId('dock-properties')
    await expect(properties).toHaveCount(0)
    await expandSeat(seat)
    // Sample the actual fixture's closed rectangle, without changing its pose.
    // Require a point whose topmost DOM owner is the expanded seat, and which
    // is below the compact disclosure. Failure means the obstruction was not
    // reproduced and must be investigated, rather than moving the drawing.
    const point = await mount.evaluate((element, compactBottom) => {
      const seat = document.querySelector('[data-testid="solar-flow-seat"]')
      for (let x = 1; x < 20; x += 1) for (let y = 1; y < 20; y += 1) {
        const world = { x: 111.25 + 222.5 * x / 20, y: 222.5 + 221.5 * y / 20 }
        const projected = element.__cadviewer.project(world.x, world.y)
        if (projected.y > compactBottom + 8 && seat.contains(document.elementFromPoint(projected.x, projected.y))) {
          return { ...projected, world }
        }
      }
      return null
    }, compact.y + compact.height)
    expect(point, 'expanded ready seat must obstruct a real projected fixture point').not.toBeNull()
    await page.screenshot({ path: testInfo.outputPath(`${op}-expanded-obstruction.png`) })
    const toggle = seat.getByRole('button', { name: 'Solar flow tools', exact: true })
    await toggle.press('Space')
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(await mount.evaluate((element, point) => document.elementFromPoint(point.x, point.y) === element.querySelector('canvas'), point)).toBe(true)
    await expect(page.locator('.workspace-card[data-cockpit-picking="1"]')).toHaveCount(0)
    await page.mouse.click(point.x, point.y)
    // This portal reads the real EngineSessionProvider's selected entity.
    await expect(properties).toBeVisible()
    const count = page.getByTestId('cad-edit-entity-count')
    await expect(count).toHaveText('1')
    expect(await pose()).toEqual(initialPose)
    await page.screenshot({ path: testInfo.outputPath(`${op}-ordinary-selection.png`) })
    const tool = page.getByTestId('drafting-ribbon').locator(`[data-tool="solar-panels:${op}"]`)
    await expect(tool).toBeEnabled()
    await tool.click()
    if (op === 'arrayRect') {
      // ARRAYRECT asks for grid counts and spacings, with no canvas pick
      // sequence (promptKeys.js and arrayVerb.test.jsx).
      await expect(page.locator('.workspace-card[data-cockpit-picking="1"]')).toHaveCount(0)
      expect(await seat.evaluate((element) => getComputedStyle(element).pointerEvents)).toBe('auto')
      for (const field of ['rows', 'columns', 'row spacing', 'column spacing']) {
        await expect(page.getByLabel(`ribbon ${field}`, { exact: true })).toBeVisible()
      }
    } else {
      await expect(page.locator('.workspace-card[data-cockpit-picking="1"]')).toHaveCount(1)
      expect(await seat.evaluate((element) => getComputedStyle(element).pointerEvents)).toBe('none')
    }
    const fields = {
      move: [['dx', '10'], ['dy', '0']],
      rotate: [['cx', '222.5'], ['cy', '333.25'], ['angle', '15']],
      arrayRect: [['rows', '1'], ['columns', '2'], ['row spacing', '500'], ['column spacing', '500']],
    }[op]
    for (const [field, value] of fields) await page.getByLabel(`ribbon ${field}`, { exact: true }).fill(value)
    await page.getByLabel(`ribbon ${fields.at(-1)[0]}`, { exact: true }).press('Enter')
    await expect(page.getByRole('status').filter({ hasText: new RegExp(`${op} applied`) })).toHaveCount(1, { timeout: 60_000 })
    await expect(count).toHaveText(op === 'arrayRect' ? '2' : '1')
    await page.keyboard.press('Escape')
    await page.screenshot({ path: testInfo.outputPath(`${op}-applied.png`) })
    const sources = Object.fromEntries(['src/solar/SolarFlowSeat.jsx', 'src/site/cockpit.css', 'e2e/local/solar-flow-seat.spec.mjs'].map((path) =>
      [path, createHash('sha256').update(readFileSync(join(process.cwd(), path))).digest('hex')]))
    const ready = await page.request.get(`${API_BASE}/api/ready`)
    expect(ready.ok()).toBe(true)
    await testInfo.attach('seat-replay.json', { contentType: 'application/json', body: JSON.stringify({
      url: page.url(), api: API_BASE, ready: await ready.json(), sources,
      fixture: DRAWING, fixtureSha256: createHash('sha256').update(readFileSync(DRAWING)).digest('hex'),
      flags: { VITE_CAD_EDIT: '1', VITE_SOLAR_FLOW_RAIL: '1', VITE_SOLAR_SETTINGS_FORM: '1' },
      viewport: page.viewportSize(), op, point, initialPose,
      replay: 'Upload distinctive-panel.dxf; Solar CAD; expand and observe coverage; fold; click projected point; run the Solar modify verb.',
    }, null, 2) })
  })
}

for (const [row, width, height, seatWidth] of [
  ['S1', 1280, 800, 280],
  ['S2', 1600, 1000, 360],
]) {
  test(`SEAT ${row} reachable Solar flow at ${width}x${height}`, async ({ page }, testInfo) => {
    if (!flagsOn(testInfo)) return
    test.setTimeout(120_000)
    await page.setViewportSize({ width, height })
    const seat = await openSolarDrawing(page)
    expect(await seat.evaluate((element) => getComputedStyle(element).position)).toBe('fixed')
    const compact = await seat.boundingBox()
    expect(compact.height).toBeLessThan(70)
    await expect(page.getByTestId('solar-flow-select')).toBeHidden()
    const toggle = seat.getByRole('button', { name: 'Solar flow tools', exact: true })
    await toggle.focus()
    await page.keyboard.press('Tab')
    expect(await seat.getByTestId('solar-flow-seat-content').evaluate((element) => element.contains(document.activeElement))).toBe(false)
    await expandSeat(seat)
    const box = await seat.boundingBox()
    expect(Math.abs(box.width - seatWidth)).toBeLessThanOrEqual(1)
    expect(box.height).toBeGreaterThan(compact.height)
    expect(box.x).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width).toBeLessThanOrEqual(width)
    expect(box.y + box.height).toBeLessThanOrEqual(height - 90)
    expect(await seat.evaluate((element) => {
      const a = element.getBoundingClientRect()
      return [...document.querySelectorAll('.properties-dock, .bar-dock, .cockpit-view, .cad-overview')].every((other) => {
        const b = other.getBoundingClientRect()
        return b.width === 0 || b.height === 0 || a.right <= b.left || a.left >= b.right || a.bottom <= b.top || a.top >= b.bottom
      })
    })).toBe(true)
    await noHorizontalOverflow(seat)
    await ownsCentre(page.getByTestId('solar-flow-select'))
    const firstStep = page.getByTestId('solar-flow-rail').locator('.solar-flow-step button').first()
    await expect(firstStep).toBeVisible({ timeout: 30_000 })
    await ownsCentre(firstStep)
    await firstStep.click()
    await openLandxml(page, seat)
    await page.screenshot({ path: testInfo.outputPath(`seat-${width}x${height}.png`) })
  })
}

test('SEAT S3 wheel scroll stays inside the bounded seat', async ({ page }, testInfo) => {
  if (!flagsOn(testInfo)) return
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1280, height: 640 })
  const seat = await openSolarDrawing(page)
  await expandSeat(seat)
  await openLandxml(page, seat)
  const content = seat.getByTestId('solar-flow-seat-content')
  expect(await content.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true)
  expect(await content.evaluate((element) => getComputedStyle(element).overflowY)).toBe('auto')
  await content.evaluate((element) => { element.scrollTop = 0 })
  const rect = await content.boundingBox()
  const readOuterScroll = () => page.evaluate(() => ({
    window: window.scrollY, main: document.querySelector('main.center-scroll').scrollTop,
  }))
  const before = await readOuterScroll()
  await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2)
  await page.mouse.wheel(0, 200)
  await expect.poll(() => content.evaluate((element) => element.scrollTop)).toBeGreaterThan(0)
  expect(await content.boundingBox()).toEqual(rect)
  expect(await readOuterScroll()).toEqual(before)
  const last = seat.getByRole('button').last()
  await last.scrollIntoViewIfNeeded()
  await ownsCentre(last)
})

test('SEAT S4 Start preserves the mounted seat and its scroll position', async ({ page }, testInfo) => {
  if (!flagsOn(testInfo)) return
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1280, height: 800 })
  const seat = await openSolarDrawing(page)
  await expandSeat(seat)
  await openLandxml(page, seat)
  const coordinateSystem = page.getByTestId('solar-landxml-upload').getByLabel('Coordinate system', { exact: true })
  await coordinateSystem.fill('EPSG:32617')
  const content = seat.getByTestId('solar-flow-seat-content')
  const scrollTop = await content.evaluate((element) => {
    element.__seatContinuity = 'seat-s4'
    element.scrollTop = 100
    return element.scrollTop
  })
  expect(scrollTop).toBeGreaterThan(0)
  const toggle = seat.getByRole('button', { name: 'Solar flow tools', exact: true })
  await toggle.press('Space')
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await expect(content).toBeHidden()
  await toggle.press('Enter')
  expect(await content.evaluate((element) => element.scrollTop)).toBe(scrollTop)
  await expect(page.getByTestId('solar-flow-select')).toHaveValue('ground-physical')
  await expect(page.getByTestId('solar-landxml-upload')).toBeVisible()
  await expect(coordinateSystem).toHaveValue('EPSG:32617')
  await page.locator('.doc-tab-start').click()
  await expect(seat).toBeAttached()
  await expect(seat).toBeHidden()
  expect(await seat.evaluate((element) => getComputedStyle(element).visibility)).toBe('hidden')
  await page.getByRole('button', { name: 'Return to drawing', exact: true }).click()
  await expect(seat).toBeVisible()
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  expect(await content.evaluate((element) => element.__seatContinuity)).toBe('seat-s4')
  expect(await content.evaluate((element) => element.scrollTop)).toBe(scrollTop)
  await expect(coordinateSystem).toHaveValue('EPSG:32617')
})

test('SEAT S6 narrow layouts keep main\'s flow host styles', async ({ page }, testInfo) => {
  if (!flagsOn(testInfo)) return
  test.setTimeout(240_000)
  for (const { width, height } of [{ width: 390, height: 844 }, { width: 800, height: 768 }]) {
    await page.setViewportSize({ width, height })
    const seat = await openNarrowSolarDrawing(page)
    expect(await seat.evaluate((element) => getComputedStyle(element).display)).toBe('contents')
    if (width === 390) {
      const children = await seat.getByTestId('solar-flow-seat-content').evaluate((element) => [...element.children].map((child) => {
        const style = getComputedStyle(child)
        return { grow: style.flexGrow, shrink: style.flexShrink, basis: style.flexBasis }
      }))
      expect(children.length).toBeGreaterThan(0)
      for (const child of children) expect(child).toEqual({ grow: '0', shrink: '0', basis: 'auto' })
    } else {
      const select = page.locator('#solar-flow-select')
      await expect(select).toHaveCount(1)
      expect(await select.evaluate((element) => getComputedStyle(element).backgroundColor)).not.toBe('rgba(240, 242, 240, 0.08)')
    }
    await expect(seat.getByRole('button', { name: 'Solar flow tools', exact: true })).toBeHidden()
    await page.screenshot({ path: testInfo.outputPath(`seat-narrow-${width}.png`) })
  }
})

test('SEAT S5 the overview yields to the seat between 981 and 1199 px', async ({ page }, testInfo) => {
  if (!flagsOn(testInfo)) return
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1024, height: 768 })
  const seat = await openSolarDrawing(page)
  await expandSeat(seat)
  const box = await seat.boundingBox()
  for (const [actual, expected] of [[box.x, 708], [box.width, 280]]) expect(Math.abs(actual - expected)).toBeLessThanOrEqual(1)
  expect(box.y + box.height).toBeLessThanOrEqual(646)
  const overview = page.locator('.cad-overview')
  await expect(overview).toHaveCount(1)
  await expect(overview).toBeHidden()
  await ownsCentre(page.getByTestId('solar-flow-select'))
  const firstStep = page.getByTestId('solar-flow-rail').locator('.solar-flow-step button').first()
  await expect(firstStep).toBeVisible({ timeout: 30_000 })
  await ownsCentre(firstStep)
  await page.getByTestId('solar-flow-select').selectOption('ground-physical')
  const trigger = seat.getByRole('button', { name: 'Import LandXML terrain', exact: true })
  await trigger.scrollIntoViewIfNeeded()
  await ownsCentre(trigger)
  await noHorizontalOverflow(seat)
  await page.setViewportSize({ width: 1200, height: 800 })
  await expect(overview).toBeVisible()
  const wideSeat = await seat.boundingBox()
  const wideOverview = await overview.boundingBox()
  expect(wideSeat.x + wideSeat.width <= wideOverview.x || wideSeat.x >= wideOverview.x + wideOverview.width ||
    wideSeat.y + wideSeat.height <= wideOverview.y || wideSeat.y >= wideOverview.y + wideOverview.height).toBe(true)
})
