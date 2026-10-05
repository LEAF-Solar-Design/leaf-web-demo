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

test('URL307B-E1: Project switch forgets an account upload before reload', async ({ page, request }) => {
  test.setTimeout(180_000)
  await requireLocalReady(request, test, API_BASE)
  await page.setViewportSize({ width: 1920, height: 1080 })
  const drawingKey = 'leaf.cat.workbench.id.v1'
  const projectNames = ['Reset project P', 'Reset project Q']
  const board = page.locator('[data-ground="browser"]')
  for (const name of projectNames) {
    await page.goto('/app?surface=browser')
    const start = board.getByRole('region', { name: 'Workspace projects', exact: true })
    await expect(start).toBeVisible({ timeout: 30_000 })
    if (await start.getByRole('button', { name, exact: true }).count() === 0) {
      await start.getByLabel('Project name').fill(name)
      await start.getByRole('button', { name: 'Create project', exact: true }).click()
      await expect(board).toHaveAttribute('data-project-state', 'project')
      await expect(board).toContainText(name)
    }
  }
  await page.evaluate((key) => sessionStorage.removeItem(key), drawingKey)
  await page.goto('/app?surface=browser')
  await expect(board).toHaveAttribute('data-project-state', 'drawing')
  expect(new URL(page.url()).searchParams.has('drawing')).toBe(false)
  expect(await page.evaluate((key) => sessionStorage.getItem(key), drawingKey)).toBeNull()
  await page.getByRole('tab', { name: 'Project', exact: true }).click()
  const uploadAction = page.locator('[data-tool="files:upload"]')
  await expect(uploadAction).toBeEnabled({ timeout: 20_000 })
  await uploadAction.click()
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
  const uploadedDocument = `${receipt.drawing_id}-v1.dxf`
  await expect(page.getByText('Drawing ready', { exact: true })).toBeVisible({ timeout: 90_000 })
  await expect.poll(() => new URL(page.url()).searchParams.getAll('drawing')).toEqual([receipt.drawing_id])
  await page.getByRole('tab', { name: 'CAD', exact: true }).click()
  const document = page.locator('.workspace-card[data-engine-document]')
  await expect(document).toHaveAttribute('data-engine-document', uploadedDocument, { timeout: 30_000 })
  const dock = page.getByTestId('dock-drawing')
  await expect(dock.locator('dt').filter({ hasText: /^Name$/ }).locator('+ dd')).toHaveText(uploadedDocument)
  await expect(dock.locator('dt').filter({ hasText: /^Entities$/ }).locator('+ dd')).toHaveText('1')

  const switcher = page.getByRole('button', { name: /^Projects: change project\./ })
  await switcher.click()
  await page.getByRole('menuitem').filter({ hasText: projectNames[0] }).click()
  await expect(switcher).toContainText(projectNames[0])
  await expect.poll(() => new URL(page.url()).searchParams.getAll('drawing')).toEqual([receipt.drawing_id])
  expect(await page.evaluate((key) => sessionStorage.getItem(key), drawingKey)).toBe(receipt.drawing_id)
  await switcher.click()
  await page.getByRole('menuitem').filter({ hasText: projectNames[1] }).click()
  await expect(switcher).toContainText(projectNames[1])
  await expect.poll(() => new URL(page.url()).searchParams.has('drawing')).toBe(false)
  await expect.poll(() => page.evaluate((key) => sessionStorage.getItem(key), drawingKey)).toBeNull()

  const solar = page.getByRole('tab', { name: 'Solar CAD', exact: true })
  await solar.click()
  await expect(solar).toHaveAttribute('aria-selected', 'true')
  await expect.poll(() => ({ surface: new URL(page.url()).searchParams.get('surface'),
    drawing: new URL(page.url()).searchParams.has('drawing') })).toEqual({ surface: 'solar', drawing: false })

  const freshRequests = new Set()
  const freshResponses = []
  let committed = false
  const onNavigated = (frame) => { if (frame === page.mainFrame()) committed = true }
  const onRequest = (req) => { if (committed) freshRequests.add(req) }
  const onResponse = (res) => { if (freshRequests.has(res.request())) freshResponses.push(res) }
  const booted = page.waitForResponse((res) => {
    const url = new URL(res.url())
    return freshRequests.has(res.request()) && res.request().method() === 'GET' && res.ok()
      && url.pathname === '/api/session' && url.searchParams.get('dwg') === 'rooftop_demo'
  }, { timeout: 90_000 })
  page.on('framenavigated', onNavigated)
  page.on('request', onRequest)
  page.on('response', onResponse)
  try {
    await page.reload()
    await booted
    await expect(solar).toHaveAttribute('aria-selected', 'true')
    await expect(document).toHaveAttribute('data-engine-document', /^demo-v\d+\.dxf$/, { timeout: 60_000 })
    const defaultDocument = await document.getAttribute('data-engine-document')
    await expect(dock.locator('dt').filter({ hasText: /^Name$/ }).locator('+ dd')).toHaveText(defaultDocument)
    await expect(dock.locator('dt').filter({ hasText: /^Entities$/ }).locator('+ dd')).toHaveText('2,345')
    await expect(page.locator(`.workspace-card[data-engine-document="${uploadedDocument}"]`)).toHaveCount(0)
    await expect(page.getByText(uploadedDocument, { exact: true })).toHaveCount(0)
    expect(new URL(page.url()).searchParams.has('drawing')).toBe(false)
    expect(new URL(page.url()).searchParams.get('surface')).toBe('solar')
    expect(await page.evaluate((key) => sessionStorage.getItem(key), drawingKey)).toBeNull()
    const versionRequests = [...freshRequests].filter((req) => /\/api\/drawings\/[^/]+\/versions$/.test(new URL(req.url()).pathname))
    expect(versionRequests.length).toBeGreaterThan(0)
    for (const req of versionRequests) expect(decodeURIComponent(new URL(req.url()).pathname)).toBe('/api/drawings/demo/versions')
    expect(freshResponses.some((res) => res.ok() && new URL(res.url()).pathname === '/api/drawings/demo/versions')).toBe(true)
    for (const req of freshRequests) {
      expect(decodeURIComponent(req.url())).not.toContain(receipt.drawing_id)
      expect(req.postData() || '').not.toContain(receipt.drawing_id)
    }
  } finally {
    page.off('response', onResponse)
    page.off('request', onRequest)
    page.off('framenavigated', onNavigated)
  }
})
