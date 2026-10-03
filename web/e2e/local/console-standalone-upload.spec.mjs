import { expect, test } from '@playwright/test'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const DRAWING = 'e2e/fixtures/distinctive-panel.dxf'

test('sf-w3-console-standalone-upload E1: Browser upload opens its drawing in CAD', async ({ page, request }) => {
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
})
