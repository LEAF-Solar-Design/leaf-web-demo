import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const DRAWING = join(process.cwd(), 'e2e', 'fixtures', 'distinctive-panel.dxf')
const REQUEST_TIMEOUT = 15_000

function flagsOn(testInfo) {
  if (process.env.VITE_CAD_EDIT === '1' && process.env.VITE_SOLAR_FLOW_RAIL === '1' && process.env.VITE_SOLAR_SETTINGS_FORM === '1') return true
  testInfo.annotations.push({ type: 'flag-gate', description: 'Requires VITE_CAD_EDIT=1, VITE_SOLAR_FLOW_RAIL=1 and VITE_SOLAR_SETTINGS_FORM=1.' })
  return false
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

for (const [row, width, height, top, seatWidth, bottom] of [
  ['S1', 1280, 800, 271, 280, 678],
  ['S2', 1600, 1000, 241, 360, 878],
]) {
  test(`SEAT ${row} reachable Solar flow at ${width}x${height}`, async ({ page }, testInfo) => {
    if (!flagsOn(testInfo)) return
    test.setTimeout(120_000)
    await page.setViewportSize({ width, height })
    const seat = await openSolarDrawing(page)
    expect(await seat.evaluate((element) => getComputedStyle(element).position)).toBe('fixed')
    const box = await seat.boundingBox()
    for (const [actual, expected] of [[box.x, 708], [box.y, top], [box.width, seatWidth]]) expect(Math.abs(actual - expected)).toBeLessThanOrEqual(1)
    expect(box.y + box.height).toBeLessThanOrEqual(bottom)
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
    await openLandxml(page, seat)
    await page.screenshot({ path: testInfo.outputPath(`seat-${width}x${height}.png`) })
  })
}

test('SEAT S3 wheel scroll stays inside the bounded seat', async ({ page }, testInfo) => {
  if (!flagsOn(testInfo)) return
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1280, height: 640 })
  const seat = await openSolarDrawing(page)
  await openLandxml(page, seat)
  expect(await seat.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true)
  expect(await seat.evaluate((element) => getComputedStyle(element).overflowY)).toBe('auto')
  await seat.evaluate((element) => { element.scrollTop = 0 })
  const rect = await seat.boundingBox()
  const readOuterScroll = () => page.evaluate(() => ({
    window: window.scrollY, main: document.querySelector('main.center-scroll').scrollTop,
  }))
  const before = await readOuterScroll()
  await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2)
  await page.mouse.wheel(0, 200)
  await expect.poll(() => seat.evaluate((element) => element.scrollTop)).toBeGreaterThan(0)
  expect(await seat.boundingBox()).toEqual(rect)
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
  await openLandxml(page, seat)
  const scrollTop = await seat.evaluate((element) => {
    element.__seatContinuity = 'seat-s4'
    element.scrollTop = 100
    return element.scrollTop
  })
  expect(scrollTop).toBeGreaterThan(0)
  await page.locator('.doc-tab-start').click()
  await expect(seat).toBeAttached()
  await expect(seat).toBeHidden()
  expect(await seat.evaluate((element) => getComputedStyle(element).visibility)).toBe('hidden')
  await page.getByRole('button', { name: 'Return to drawing', exact: true }).click()
  await expect(seat).toBeVisible()
  expect(await seat.evaluate((element) => element.__seatContinuity)).toBe('seat-s4')
  expect(await seat.evaluate((element) => element.scrollTop)).toBe(scrollTop)
})

test('SEAT S6 narrow layouts keep main\'s flow host styles', async ({ page }, testInfo) => {
  if (!flagsOn(testInfo)) return
  test.setTimeout(240_000)
  for (const { width, height } of [{ width: 390, height: 844 }, { width: 800, height: 768 }]) {
    await page.setViewportSize({ width, height })
    const seat = await openNarrowSolarDrawing(page)
    expect(await seat.evaluate((element) => getComputedStyle(element).display)).toBe('contents')
    if (width === 390) {
      const children = await seat.evaluate((element) => [...element.children].map((child) => {
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
  }
})

test('SEAT S5 the overview yields to the seat between 981 and 1199 px', async ({ page }, testInfo) => {
  if (!flagsOn(testInfo)) return
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1024, height: 768 })
  const seat = await openSolarDrawing(page)
  const box = await seat.boundingBox()
  for (const [actual, expected] of [[box.x, 708], [box.y, 271], [box.width, 280]]) expect(Math.abs(actual - expected)).toBeLessThanOrEqual(1)
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
