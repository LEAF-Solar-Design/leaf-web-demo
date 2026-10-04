import { expect, test } from '@playwright/test'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const DRAWING = 'e2e/fixtures/distinctive-panel.dxf'

test('URL307A-E1: Browser account upload survives Solar CAD reload', async ({ page, request }) => {
  test.setTimeout(120_000)
  await requireLocalReady(request, test, API_BASE)
  await page.setViewportSize({ width: 1920, height: 1080 })
  await page.goto('/app?surface=browser')
  await page.getByRole('tab', { name: 'Project', exact: true }).click()
  const uploadAction = page.locator('[data-tool="files:upload"]')
  await expect(uploadAction).toBeEnabled({ timeout: 20_000 })
  await uploadAction.click()
  await expect(page.getByLabel('Drawing file', { exact: true })).toHaveCount(1)
  const uploadResponse = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/drawings/upload')
  const chooser = page.waitForEvent('filechooser')
  await page.getByRole('button', { name: 'Upload DWG or DXF', exact: true }).click()
  await (await chooser).setFiles(DRAWING)
  const response = await uploadResponse
  expect(response.status()).toBe(202)
  const receipt = await response.json()
  expect(receipt.tenant_kind).toBe('account')
  expect(typeof receipt.drawing_id).toBe('string')
  expect(receipt.drawing_id.length).toBeGreaterThan(0)
  await expect(page.getByText('Drawing ready', { exact: true })).toBeVisible({ timeout: 90_000 })
  await page.getByRole('tab', { name: 'CAD', exact: true }).click()
  const document = `${receipt.drawing_id}-v1.dxf`
  await expect(page.locator('.workspace-card[data-engine-document]')).toHaveAttribute('data-engine-document', document, { timeout: 30_000 })
  const dock = page.getByTestId('dock-drawing')
  await expect(dock.locator('dt').filter({ hasText: /^Name$/ }).locator('+ dd')).toHaveText(document)
  await expect(dock.locator('dt').filter({ hasText: /^Entities$/ }).locator('+ dd')).toHaveText('1')
  const solar = page.getByRole('tab', { name: 'Solar CAD', exact: true })
  await solar.click()
  const assertAddress = async () => {
    await expect.poll(() => {
      const url = new URL(page.url())
      return { surface: url.searchParams.get('surface'), drawing: url.searchParams.getAll('drawing') }
    }).toEqual({ surface: 'solar', drawing: [receipt.drawing_id] })
  }
  const assertDrawing = async () => {
    await expect(page.locator('.workspace-card[data-engine-document]')).toHaveAttribute('data-engine-document', document, { timeout: 30_000 })
    await expect(dock.locator('dt').filter({ hasText: /^Name$/ }).locator('+ dd')).toHaveText(document)
    await expect(dock.locator('dt').filter({ hasText: /^Entities$/ }).locator('+ dd')).toHaveText('1')
  }
  await expect(solar).toHaveAttribute('aria-selected', 'true')
  await assertAddress()
  await assertDrawing()

  // Count UI requests only after the reloaded document commits; an old document's
  // request cannot prove that the new document booted its drawing from the address.
  const freshRequests = new Set()
  let committed = false
  const onNavigated = (frame) => { if (frame === page.mainFrame()) committed = true }
  const onRequest = (request) => { if (committed) freshRequests.add(request) }
  const matchesDrawing = (response) => {
    const request = response.request()
    if (!freshRequests.has(request) || request.method() !== 'GET' || !response.ok()) return false
    const url = new URL(response.url())
    return decodeURIComponent(url.pathname) === `/api/drawings/${receipt.drawing_id}/intake`
      || (url.pathname === '/api/session' && url.searchParams.get('dwg') === receipt.drawing_id)
  }
  const restored = page.waitForResponse(matchesDrawing, { timeout: 90_000 })
  freshRequests.clear()
  page.on('framenavigated', onNavigated)
  page.on('request', onRequest)
  await page.reload()
  await restored
  page.off('request', onRequest)
  page.off('framenavigated', onNavigated)
  await expect(solar).toHaveAttribute('aria-selected', 'true')
  await assertAddress()
  await assertDrawing()
  await expect(page.getByText('rooftop_demo.dwg', { exact: false })).toHaveCount(0)
  await expect(page.getByText(/\b(?:2345|2,345)\b/)).toHaveCount(0)
})
