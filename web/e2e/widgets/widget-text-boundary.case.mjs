import { test, expect } from '@playwright/test'
import { readFile, writeFile } from 'node:fs/promises'

test('native text input preserves complete Unicode at the UTF16 limit', async ({ page }, testInfo) => {
  let saved, requests = 0
  await page.route('**/api/**', async route => {
    const request = route.request()
    expect(new URL(request.url()).pathname).toBe('/api/lab/docs/text-boundary/save')
    requests += 1
    const body = request.postDataJSON()
    expect(body.expected_head).toBeNull()
    expect(body.kind).toBe('widget')
    saved = body.graph
    await route.fulfill({ json: { revision: 1, kind: 'widget', graph: saved } })
  })
  await page.goto('/e2e/widgets/harness.html')
  await page.getByLabel('Document ID', { exact: true }).fill('text-boundary')
  await page.getByRole('button', { name: 'New', exact: true }).click()
  await page.getByRole('button', { name: 'Add text', exact: true }).click()
  await page.getByRole('button', { name: 'Edit full text', exact: true }).click()
  const field = page.getByLabel('Full text', { exact: true }), observations = []
  const emoji = '\u{1F600}', before = 'a'.repeat(511)
  await field.fill(before)
  await field.press('End')
  await page.keyboard.insertText(emoji)
  const blocked = await field.inputValue()
  observations.push({ insertion: 'emoji after511 units', value: blocked, units: blocked.length })
  expect(blocked).toBe(before)
  await field.fill('a'.repeat(510))
  await field.press('End')
  await page.keyboard.insertText(emoji)
  const complete = 'a'.repeat(510) + emoji
  await expect(field).toHaveValue(complete)
  observations.push({ insertion: 'emoji after510 units', value: await field.inputValue(), units: complete.length })
  await field.fill(before)
  await field.press('End')
  await page.keyboard.insertText('\u0301')
  const combining = before + '\u0301'
  await expect(field).toHaveValue(combining)
  observations.push({ insertion: 'combining mark after511 units', value: await field.inputValue(), units: combining.length })
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByRole('status').first()).toHaveText('Saved revision 1.')
  const pending = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download widget definition', exact: true }).click()
  const file = await pending, actual = JSON.parse(await readFile(await file.path(), 'utf8'))
  expect(actual).toEqual(saved)
  expect(actual.formats.la.els[0].text).toBe(combining)
  expect(requests).toBe(1)
  const proof = testInfo.outputPath('text-boundary-proof.json')
  await writeFile(proof, JSON.stringify({ observations, actual, requests, fixture: true }, null, 2))
  await testInfo.attach('text-boundary-proof.json', { path: proof, contentType: 'application/json' })
})
