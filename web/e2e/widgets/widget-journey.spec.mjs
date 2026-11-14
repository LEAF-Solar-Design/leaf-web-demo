import { test, expect } from '@playwright/test'
import { readFile } from 'node:fs/promises'

const fixture = '/e2e/widgets/harness.html'
const sample = ['SAMPLE portfolio', 'Total: 1010', 'Today: +10 (+1%)', 'Open positions: 3']

async function documentStore(page) {
  const documents = new Map(), calls = []
  let refusal = null
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    calls.push({ method: request.method(), path })
    const match = path.match(/^\/api\/lab\/docs\/([^/]+)\/(head|save)$/)
    if (!match) throw new Error(`Unexpected fixture API request: ${path}`)
    const [, id, operation] = match
    const stored = documents.get(id)
    if (operation === 'head') {
      await route.fulfill({ status: stored ? 200 : 404, json: stored ?? { detail: 'not found' } })
    } else {
      const body = request.postDataJSON()
      expect(body.kind).toBe('widget')
      expect(body.graph.schema).toBe('widget-doc.v1')
      const status = refusal ?? (body.expected_head !== (stored?.revision ?? null) ? 409 : 200)
      if (status !== 200) {
        await route.fulfill({ status, json: { detail: 'Synthetic document store refusal' } })
        return
      }
      const next = { graph: body.graph, kind: 'widget', revision: (stored?.revision ?? 0) + 1, sha256: 'a'.repeat(64) }
      documents.set(id, next)
      await route.fulfill({ json: next })
    }
  })
  return { documents, calls, refuse: status => { refusal = status } }
}

async function blank(page, name = 'summary') {
  await page.getByLabel('Document ID', { exact: true }).fill(name)
  await page.getByRole('button', { name: 'New', exact: true }).click()
  await expect(page.locator('.widget-interaction [data-element-id]')).toHaveCount(0)
}
async function field(page, name, value) {
  const control = page.getByLabel(name, { exact: true })
  await control.fill(String(value)); await control.press('Enter')
}
async function addText(page, value, y) {
  await page.getByRole('button', { name: 'Add text', exact: true }).click()
  await page.getByLabel('Text', { exact: true }).fill(value)
  for (const [name, number] of [['X', 14], ['Y', y], ['W', 330], ['H', 24]]) await field(page, name, number)
}
async function download(page) {
  const pending = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download widget definition', exact: true }).click()
  const file = await pending
  expect(file.suggestedFilename()).toBe('widget-summary.json')
  return JSON.parse(await readFile(await file.path(), 'utf8'))
}
async function geometry(page) {
  return page.locator('.widget-interaction [data-element-id]').last().evaluate(node =>
    ['x', 'y', 'width', 'height'].map(key => Number(node.getAttribute(key))))
}

test('sample editing, pointer history, readable defaults and save/reload preserve the actual export', async ({ page }, testInfo) => {
  const store = await documentStore(page), errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(fixture)
  await blank(page)
  const started = Date.now()
  for (const [index, value] of sample.entries()) await addText(page, value, 14 + index * 32)
  await expect(page.getByRole('combobox', { name: 'Color', exact: true })).toHaveValue('slate')
  const board = page.getByTestId('widget-surface')
  await board.scrollIntoViewIfNeeded()
  await expect(page.getByRole('img', { name: 'Widget preview', exact: true }).locator('text')).toHaveText(sample)
  await expect(page.getByRole('img', { name: 'Widget preview', exact: true }).locator('text').first()).toHaveCSS('fill', 'rgb(244, 244, 245)')
  const before = await geometry(page)
  const target = page.locator('.widget-interaction [data-element-id]').last()
  const bounds = await target.boundingBox()
  await page.mouse.move(bounds.x + 100, bounds.y + 12)
  await page.mouse.down(); await page.mouse.move(bounds.x + 106, bounds.y + 12, { steps: 8 }); await page.mouse.up()
  const moved = await geometry(page)
  expect(moved).not.toEqual(before)
  await page.getByRole('button', { name: 'Undo', exact: true }).click()
  expect(await geometry(page)).toEqual(before)
  await page.getByRole('button', { name: 'Redo', exact: true }).click()
  expect(await geometry(page)).toEqual(moved)
  await page.locator('[data-layer-id]').last().click()
  const handle = await page.locator('[data-handle="e"]').boundingBox()
  await page.mouse.move(handle.x + 3, handle.y + 3)
  await page.mouse.down(); await page.mouse.move(handle.x - 9, handle.y + 3, { steps: 8 }); await page.mouse.up()
  const resized = await geometry(page)
  expect(resized[2]).toBeLessThan(moved[2])
  await page.getByRole('button', { name: 'Undo', exact: true }).click()
  expect(await geometry(page)).toEqual(moved)
  await page.getByRole('button', { name: 'Redo', exact: true }).click()
  expect(await geometry(page)).toEqual(resized)
  const baseline = await download(page)
  expect(baseline.formats.la.h).toBe(160)
  expect(baseline.formats.la.els.map(element => element.color)).toEqual(sample.map(() => 'slate'))
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByRole('status').first()).toHaveText('Saved revision 1.')
  expect(store.documents.size).toBe(1)
  await page.reload()
  await expect(page.getByRole('status').first()).toHaveText('Saved revision 1.')
  expect(await download(page)).toEqual(baseline)
  await expect(page.getByRole('button', { name: 'Request publication' })).toHaveCount(0)
  expect(errors).toEqual([])
  await testInfo.attach('fixture-proof.json', { body: JSON.stringify({ evidence: 'isolated browser with fixture document API', elapsedMs: Date.now() - started, requests: store.calls, doc: baseline }), contentType: 'application/json' })
  await testInfo.attach('readable-widget.png', { body: await board.screenshot(), contentType: 'image/png' })
})

for (const status of [409, 503]) test(`save ${status} retains edits and supports explicit recovery`, async ({ page }) => {
  const store = await documentStore(page)
  await page.goto(fixture); await blank(page); await addText(page, 'Saved baseline', 14)
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByRole('status').first()).toHaveText('Saved revision 1.')
  await page.getByLabel('Text', { exact: true }).fill('Unsaved recovery text')
  store.refuse(status)
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveText('Synthetic document store refusal')
  await expect(page.getByLabel('Text', { exact: true })).toHaveValue('Unsaved recovery text')
  expect([...store.documents.values()][0].graph.formats.la.els[0].text).toBe('Saved baseline')
  await expect(page.getByRole('button', { name: 'Download widget definition' })).toBeDisabled()
  store.refuse(null)
  if (status === 409) {
    await page.getByRole('button', { name: 'Reload', exact: true }).click()
    await expect(page.getByRole('status').first()).toHaveText('Saved revision 1.')
    expect((await download(page)).formats.la.els[0].text).toBe('Saved baseline')
  } else {
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByRole('status').first()).toHaveText('Saved revision 2.')
    expect((await download(page)).formats.la.els[0].text).toBe('Unsaved recovery text')
  }
})

test('same document name stays isolated across fixture projects', async ({ page }) => {
  const store = await documentStore(page)
  await page.goto(fixture); await blank(page); await addText(page, 'Project A sample', 14)
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByRole('status').first()).toHaveText('Saved revision 1.')
  await page.getByLabel('Fixture project').selectOption('regression-b')
  await expect(page.getByRole('textbox', { name: 'Document ID' })).toHaveValue('')
  await blank(page); await addText(page, 'Project B sample', 14)
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByRole('status').first()).toHaveText('Saved revision 1.')
  expect(store.documents.size).toBe(2)
  await page.getByLabel('Fixture project').selectOption('regression-a')
  await expect(page.getByRole('status').first()).toHaveText('Saved revision 1.')
  expect((await download(page)).formats.la.els[0].text).toBe('Project A sample')
})
