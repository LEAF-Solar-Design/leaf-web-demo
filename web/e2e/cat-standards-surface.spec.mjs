import { expect, test } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { REQUEST, catProofResponse, makeCatProofState } from './catProofFixture.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const PROOF_DIR = join(HERE, '..', '..', 'artifacts', 'cat-operator-proof')

test('standards surface keeps the complete cat operator flow in one scene', async ({ page }) => {
  test.setTimeout(120_000)
  mkdirSync(PROOF_DIR, { recursive: true })
  const proofState = makeCatProofState()

  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const body = request.postData() ? request.postDataJSON() : {}
    const result = catProofResponse({ method: request.method(), path: url.pathname, body }, proofState)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try')
  await expect(page).toHaveURL(/\/try$/)
  await expect(page.getByTestId('operator-phase')).toContainText('Backend ready')
  await page.waitForTimeout(2_600)

  const command = page.getByRole('textbox', { name: 'Command bar' })
  await expect(command).toHaveValue(REQUEST)
  await page.getByRole('button', { name: 'Run', exact: true }).click()

  const surface = page.getByTestId('operator-surface')
  await expect(surface).toContainText('Rearrange the existing panels')
  await expect(surface.getByRole('button', { name: 'Approve' })).toBeVisible({ timeout: 12_000 })
  await page.screenshot({ path: join(PROOF_DIR, 'standards-01-approval.png'), fullPage: true })
  await expect(page).toHaveURL(/\/try$/)

  await surface.getByRole('button', { name: 'Approve' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('3D cat ready', { timeout: 15_000 })
  await expect(page.getByTestId('version-head')).toContainText('Version 2')
  await expect(page.getByText('Panels preserved')).toBeVisible()
  await expect(page.getByText('3,328')).toBeVisible()
  await expect(page.getByTestId('camera-controls')).toContainText('Drag to orbit')
  const viewer = page.locator('.stage-viewer .viewer-canvas')
  await expect(viewer).toHaveAttribute('data-view-mode', 'panel-sculpture')
  const depth = Number(await viewer.getAttribute('data-depth-span'))
  expect(depth).toBeGreaterThan(8)
  const beforeOrbit = await viewer.evaluate((node) => node.__cadviewer.cameraPose())
  const box = await viewer.boundingBox()
  expect(box).not.toBeNull()
  await page.mouse.move(box.x + box.width * 0.58, box.y + box.height * 0.46)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width * 0.71, box.y + box.height * 0.56, { steps: 12 })
  await page.mouse.up()
  await page.waitForTimeout(300)
  const afterOrbit = await viewer.evaluate((node) => node.__cadviewer.cameraPose())
  expect(afterOrbit.position).not.toEqual(beforeOrbit.position)
  await page.screenshot({ path: join(PROOF_DIR, 'standards-02-cat-complete.png'), fullPage: true })
  await expect(page).toHaveURL(/\/try$/)

  await page.getByRole('button', { name: 'Undo' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('Original restored')
  await expect(page.getByTestId('version-head')).toContainText('Version 1')
  await page.screenshot({ path: join(PROOF_DIR, 'standards-03-undo.png'), fullPage: true })

  await page.getByRole('button', { name: 'Redo' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('3D cat ready')
  await expect(page.getByTestId('version-head')).toContainText('Version 2')
  await page.screenshot({ path: join(PROOF_DIR, 'standards-04-redo.png'), fullPage: true })
  await expect(page).toHaveURL(/\/try$/)
})
