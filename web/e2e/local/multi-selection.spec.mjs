import { expect, test } from '@playwright/test'
import { requireLocalReady } from './requireReady.mjs'
import { setRail } from './railFlag.mjs'

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
  const lines = page.getByTestId('cad-edit-entity-list').getByRole('radio')
  const selectionCount = page.getByTestId('cad-edit-selection-count')
  await clickWorld(5, 0)
  await expect(lines.nth(0)).toBeChecked()
  await expect(selectionCount).toHaveCount(0)
  await shiftClickWorld(5, 20)
  await expect(selectionCount).toHaveText('2 objects selected')
  const move = ribbon.locator('[data-tool="modify:move"]')
  await expect(move).toBeDisabled()
  await expect(move).toHaveAccessibleName(/more than one is selected/)
  await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('2')
  await shiftClickWorld(5, 0)
  await expect(selectionCount).toHaveCount(0)
  await expect(lines.nth(0)).not.toBeChecked()
  await expect(lines.nth(1)).toBeChecked()
  await shiftClickWorld(5, 0)
  await expect(selectionCount).toHaveText('2 objects selected')
  await clickWorld(5, 50)
  await expect(lines.nth(0)).not.toBeChecked()
  await expect(lines.nth(1)).not.toBeChecked()
  await expect(selectionCount).toHaveCount(0)
})
