import { test, expect } from '@playwright/test'
import { readFile, writeFile } from 'node:fs/promises'

const sample = '\u03b1\u{1F600} e\u0301 "q"\\\n\u7b2c\u4e8c\t<&>'

test('full Unicode text round-trips through Save, Load and actual Blob export', async ({ page }, testInfo) => {
  const documents = new Map()
  await page.route('**/api/**', async route => {
    const request = route.request(), match = new URL(request.url()).pathname.match(/^\/api\/lab\/docs\/([^/]+)\/(head|save)$/)
    expect(match).not.toBeNull()
    const [, id, operation] = match, previous = documents.get(id)
    if (operation === 'head') await route.fulfill({ status: previous ? 200 : 404, json: previous ?? { detail: 'not found' } })
    else {
      const body = request.postDataJSON()
      expect(body.expected_head).toBe(previous?.revision ?? null)
      expect(body.kind).toBe('widget')
      const next = { graph: body.graph, kind: 'widget', revision: (previous?.revision ?? 0) + 1, sha256: 'a'.repeat(64) }
      documents.set(id, next); await route.fulfill({ json: next })
    }
  })
  await page.goto('/e2e/widgets/harness.html')
  await page.getByLabel('Document ID', { exact: true }).fill('unicode-proof')
  await page.getByRole('button', { name: 'New', exact: true }).click()
  await page.getByRole('button', { name: 'Add text', exact: true }).click()
  await page.getByRole('button', { name: 'Edit full text', exact: true }).click()
  await page.getByLabel('Full text', { exact: true }).fill(sample)
  await expect(page.getByLabel('Full text', { exact: true })).toHaveValue(sample)
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByRole('status').first()).toHaveText('Saved revision 1.')
  await page.getByRole('button', { name: 'Load', exact: true }).click()
  await expect(page.getByRole('status').first()).toHaveText('Saved revision 1.')
  await page.locator('button[data-layer-id]').first().click()
  await expect(page.getByLabel('Full text', { exact: true })).toHaveValue(sample)
  await expect(page.getByRole('button', { name: 'Use short text', exact: true })).toBeDisabled()
  const pending = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download widget definition', exact: true }).click()
  const file = await pending, actual = JSON.parse(await readFile(await file.path(), 'utf8'))
  expect(actual).toEqual([...documents.values()][0].graph)
  expect(actual.formats.la.els[0].text).toBe(sample)
  const report = testInfo.outputPath('unicode-proof.json')
  await writeFile(report, JSON.stringify({ sample, actual, fixture: true }))
  await testInfo.attach('unicode-proof.json', { path: report, contentType: 'application/json' })
  await page.getByLabel('Full text', { exact: true }).scrollIntoViewIfNeeded()
  const still = testInfo.outputPath('unicode-editor.png')
  await page.screenshot({ path: still })
  await testInfo.attach('unicode-editor.png', { path: still, contentType: 'image/png' })
})
