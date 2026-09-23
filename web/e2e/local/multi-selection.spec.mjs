import { expect, test } from '@playwright/test'
import { requireLocalReady } from './requireReady.mjs'
import { setRail } from './railFlag.mjs'

test('SSD1-24C marquee: window, crossing, Shift union, clear, Escape and right pan', async ({ page, request }) => {
  test.setTimeout(120_000)
  await requireLocalReady(request, test, API_BASE)
  await setRail(page, '1')
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
    expect(process.env.VITE_CAD_EDIT, 'the managed proof sets VITE_CAD_EDIT=1 and serves the compiled engine, so selection must be exercised').not.toBe('1')
    test.info().annotations.push({ type: 'engine', description: 'compiled engine not served, or flag off; canvas selection not exercised' })
    return
  }
  await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('0', { timeout: 60_000 })
  const bar = page.getByLabel('Command bar', { exact: true })
  for (const [index, command] of [
    { word: 'LINE', start: '0,0', end: '10,0' },
    { word: 'LINE', start: '0,20', end: '10,20' },
  ].entries()) {
    await bar.fill(command.word)
    await bar.press('Enter')
    await page.getByLabel('ribbon x', { exact: true }).fill(command.start)
    const last = page.getByLabel('ribbon x2', { exact: true })
    await last.fill(command.end)
    await last.press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(index + 1), { timeout: 60_000 })
    await page.keyboard.press('Escape')
  }
  const mount = page.locator('.studio-ground .viewer-canvas')
  const setCount = page.getByTestId('dock-selection-count')
  const engineProps = page.getByTestId('dock-properties')
  const geometry = page.getByTestId('dock-geometry')
  const start = geometry.locator('dt', { hasText: /^Start$/ }).locator('xpath=following-sibling::dd[1]')
  const pose = () => mount.evaluate(el => ({
    position: el.dataset.cameraPosition, target: el.dataset.cameraTarget,
  }))
  const project = async (x, y) => {
    const point = await mount.evaluate((el, { x, y }) => {
      const pt = el.__cadviewer.project(x, y)
      return { ...pt, onGround: !!document.elementFromPoint(pt.x, pt.y)?.closest('.studio-ground') }
    }, { x, y })
    expect(point.onGround, `projected point (${x},${y}) must be on the drawing`).toBe(true)
    return point
  }
  const pointer = async (type, pointerId, pointerType, world, extra = {}) => {
    const point = await project(...world)
    await mount.evaluate((el, { type, pointerId, pointerType, point, extra }) => {
      const clientX = point.x, clientY = point.y
      document.elementFromPoint(clientX, clientY).dispatchEvent(new PointerEvent(type, {
        bubbles: true, cancelable: true, composed: true, pointerId, pointerType,
        isPrimary: extra.isPrimary ?? true, button: type === 'pointermove' ? -1 : 0,
        buttons: type === 'pointerup' ? 0 : 1, clientX, clientY,
      }))
    }, { type, pointerId, pointerType, point, extra })
  }
  const pointers = async (events) => {
    await mount.evaluate((el, events) => {
      for (const [type, pointerId, pointerType, point, extra = {}] of events) {
        const clientX = point.x, clientY = point.y
        document.elementFromPoint(clientX, clientY).dispatchEvent(new PointerEvent(type, {
          bubbles: true, cancelable: true, composed: true, pointerId, pointerType,
          isPrimary: extra.isPrimary ?? true, button: type === 'pointermove' ? -1 : 0,
          buttons: type === 'pointerup' ? 0 : 1, clientX, clientY,
        }))
      }
    }, events)
  }
  const drag = async (a, b, { shift = false, cancel = false, button = 'left' } = {}) => {
    const start = await project(...a)
    const end = await project(...b)
    if (shift) await page.keyboard.down('Shift')
    try {
      await page.mouse.move(start.x, start.y)
      await page.mouse.down({ button })
      await page.mouse.move(end.x, end.y, { steps: 8 })
      if (cancel) await page.keyboard.press('Escape')
      await page.mouse.up({ button })
    } finally {
      if (shift) await page.keyboard.up('Shift')
    }
    await expect(mount.locator('.viewer-marquee')).toHaveCount(0)
  }
  const initialPose = await pose()
  await drag([-2, -2], [12, 5])
  await expect(engineProps).toBeVisible()
  await expect(setCount).toHaveCount(0)
  await expect(start).toHaveText('0.00, 0.00')
  expect(await pose()).toEqual(initialPose)
  await drag([8, 25], [2, 15])
  await expect(start).toHaveText('0.00, 20.00')
  await expect(setCount).toHaveCount(0)
  await drag([-2, -2], [12, 5], { shift: true })
  await expect(setCount).toHaveText('2 objects selected')
  await drag([2, 40], [8, 48])
  await expect(setCount).toHaveCount(0)
  await expect(engineProps).toHaveCount(0)
  await expect(geometry).toHaveCount(0)
  // Cancel a drag that would otherwise select the first line.
  const beforeCancel = await pose()
  await drag([-2, -2], [12, 5], { cancel: true })
  await expect(setCount).toHaveCount(0)
  await expect(engineProps).toHaveCount(0)
  await expect(geometry).toHaveCount(0)
  expect(await pose()).toEqual(beforeCancel)
  // A gated press owns selection and camera input until it ends or is cancelled.
  await drag([-2, -2], [12, 5])
  await expect(start).toHaveText('0.00, 0.00')
  const beforePointers = await pose()
  await pointer('pointerdown', 11, 'touch', [-2, -2])
  await pointer('pointermove', 11, 'touch', [12, 5])
  await expect(mount.locator('.viewer-marquee')).toHaveCount(1)
  await pointer('pointerdown', 12, 'touch', [5, 20], { isPrimary: false })
  await pointer('pointerup', 12, 'touch', [5, 20], { isPrimary: false })
  await expect(start).toHaveText('0.00, 0.00')
  await expect(setCount).toHaveCount(0)
  await pointer('pointerdown', 13, 'pen', [5, 10])
  for (let step = 1; step <= 4; step++) {
    await pointer('pointermove', 13, 'pen', [5 + 3 * step / 4, 10])
  }
  await pointer('pointerup', 13, 'pen', [8, 10])
  expect(await pose()).toEqual(beforePointers)
  await page.keyboard.press('Escape')
  await pointer('pointerup', 11, 'touch', [12, 5])
  await expect(mount.locator('.viewer-marquee')).toHaveCount(0)
  await expect(start).toHaveText('0.00, 0.00')
  expect(await pose()).toEqual(beforePointers)
  const heldPoint = await project(5, 20)
  const secondaryPoint = await project(5, 0)
  await pointers([
    ['pointerdown', 14, 'touch', heldPoint],
    ['pointerdown', 15, 'touch', secondaryPoint, { isPrimary: false }],
    ['pointerup', 15, 'touch', secondaryPoint, { isPrimary: false }],
    ['pointerup', 14, 'touch', heldPoint],
  ])
  await expect(start).toHaveText('0.00, 20.00')
  await expect(setCount).toHaveCount(0)
  await pointer('pointerdown', 21, 'touch', [-2, -2])
  await pointer('pointermove', 21, 'touch', [12, 5])
  await expect(mount.locator('.viewer-marquee')).toHaveCount(1)
  await mount.evaluate(el => {
    const point = el.__cadviewer.project(12, 5)
    document.body.dispatchEvent(new PointerEvent('pointerup', {
      bubbles: true, cancelable: true, composed: true, pointerId: 21, pointerType: 'touch',
      isPrimary: true, button: 0, buttons: 0, clientX: point.x, clientY: point.y,
    }))
  })
  await expect(mount.locator('.viewer-marquee')).toHaveCount(0)
  const nextPoint = await project(5, 0)
  await pointers([
    ['pointerdown', 22, 'touch', nextPoint],
    ['pointerup', 22, 'touch', nextPoint],
  ])
  await expect(start).toHaveText('0.00, 0.00')
  await drag([-2, -2], [12, 5], { button: 'right' })
  await expect.poll(pose).not.toEqual(beforeCancel)
})


const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'

test('SSD1-24B canvas selection: Shift adds, Shift again removes, a blank click clears, a set refuses Move', async ({ page, request }) => {
  test.setTimeout(120_000)
  await requireLocalReady(request, test, API_BASE)
  await setRail(page, '1')
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
    expect(process.env.VITE_CAD_EDIT, 'the managed proof sets VITE_CAD_EDIT=1 and serves the compiled engine, so selection must be exercised').not.toBe('1')
    test.info().annotations.push({ type: 'engine', description: 'compiled engine not served, or flag off; canvas selection not exercised' })
    return
  }
  await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('0', { timeout: 60_000 })
  const bar = page.getByLabel('Command bar', { exact: true })
  for (const [index, command] of [
    { word: 'LINE', start: '0,0', end: '10,0' },
    { word: 'LINE', start: '0,20', end: '10,20' },
  ].entries()) {
    await bar.fill(command.word)
    await bar.press('Enter')
    await page.getByLabel('ribbon x', { exact: true }).fill(command.start)
    const last = page.getByLabel('ribbon x2', { exact: true })
    await last.fill(command.end)
    await last.press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(index + 1), { timeout: 60_000 })
    await page.keyboard.press('Escape')
  }
  const clickWorld = async (x, y) => {
    const point = await page.evaluate(({ x, y }) => {
      const pt = document.querySelector('.studio-ground .viewer-canvas').__cadviewer.project(x, y)
      return { ...pt, onGround: !!document.elementFromPoint(pt.x, pt.y)?.closest('.studio-ground') }
    }, { x, y })
    expect(point.onGround, `projected point (${x},${y}) must be on the drawing`).toBe(true)
    await page.mouse.click(point.x, point.y)
  }
  const shiftClickWorld = async (x, y) => {
    await page.keyboard.down('Shift')
    try { await clickWorld(x, y) } finally { await page.keyboard.up('Shift') }
  }
  const setCount = page.getByTestId('dock-selection-count')
  const engineProps = page.getByTestId('dock-properties')
  const geometry = page.getByTestId('dock-geometry')
  const start = geometry.locator('dt', { hasText: /^Start$/ }).locator('xpath=following-sibling::dd[1]')
  await clickWorld(5, 0)
  await expect(engineProps).toBeVisible()
  await expect(setCount).toHaveCount(0)
  await expect(start).toHaveText('0.00, 0.00')
  await shiftClickWorld(5, 20)
  await expect(setCount).toHaveText('2 objects selected')
  const move = ribbon.locator('[data-tool="modify:move"]')
  await expect(move).toBeDisabled()
  await expect(move).toHaveAccessibleName(/more than one is selected/)
  await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('2')
  await shiftClickWorld(5, 0)
  await expect(setCount).toHaveCount(0)
  await expect(engineProps).toBeVisible()
  await expect(start).toHaveText('0.00, 20.00')
  await shiftClickWorld(5, 0)
  await expect(setCount).toHaveText('2 objects selected')
  // The view fits the two lines with a 1.08 margin, so a point far beyond y=20 sits under
  // the chrome; (5,10) is on the drawing and ten units from either line.
  await clickWorld(5, 10)
  await expect(setCount).toHaveCount(0)
  await expect(engineProps).toHaveCount(0)
  await expect(geometry).toHaveCount(0)
})
