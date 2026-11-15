import { test, expect } from '@playwright/test'
import { readFile } from 'node:fs/promises'

test('keyboard-only editing saves, reopens and downloads the exact widget', async ({ page }, testInfo) => {
  const documents = new Map(), errors = [], actions = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    const match = path.match(/^\/api\/lab\/docs\/([^/]+)\/(head|save)$/)
    expect(match).not.toBeNull()
    const [, id, operation] = match, stored = documents.get(id)
    if (operation === 'head') await route.fulfill({ status: stored ? 200 : 404, json: stored ?? { detail: 'not found' } })
    else {
      const body = request.postDataJSON()
      expect(body.kind).toBe('widget')
      expect(body.expected_head).toBe(stored?.revision ?? null)
      const next = { graph: body.graph, kind: 'widget', revision: (stored?.revision ?? 0) + 1, sha256: 'a'.repeat(64) }
      documents.set(id, next); await route.fulfill({ json: next })
    }
  })
  async function tabTo(target, name) {
    await expect(target).toBeEnabled()
    for (let steps = 0; steps < 80; steps++) {
      if (await target.evaluate(node => node === document.activeElement)) {
        await expect(target).toBeFocused(); actions.push(name); return
      }
      await page.keyboard.press('Tab')
    }
    throw new Error(`Keyboard focus could not reach ${name}`)
  }
  async function typeInto(target, name, text) {
    await tabTo(target, name)
    await page.keyboard.press('ControlOrMeta+A')
    await page.keyboard.type(text)
  }
  await page.goto('/e2e/widgets/harness.html')
  // Initial focus is support setup. Every showcased action uses real keyboard input.
  const id = page.getByLabel('Document ID', { exact: true })
  await id.focus(); await expect(id).toBeFocused(); await page.keyboard.type('keyboard-proof')
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: 'Load', exact: true })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: 'New', exact: true })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.locator('.widget-interaction [data-element-id]')).toHaveCount(0)
  await tabTo(page.getByRole('button', { name: 'Add text', exact: true }), 'Add text')
  await page.keyboard.press('Enter')
  const text = 'Keyboard saved content'
  await typeInto(page.getByLabel('Text', { exact: true }), 'Text', text)
  for (const [name, value] of [['X', '14'], ['Y', '14'], ['W', '330'], ['H', '24']]) {
    await typeInto(page.getByLabel(name, { exact: true }), name, value); await page.keyboard.press('Enter')
  }
  await tabTo(page.getByRole('button', { name: 'Save', exact: true }), 'Save')
  await page.keyboard.press('Enter')
  await expect(page.getByRole('status').first()).toHaveText('Saved revision 1.')
  await tabTo(page.getByRole('button', { name: 'Load', exact: true }), 'Load')
  await page.keyboard.press('Enter')
  await expect(page.getByRole('status').first()).toHaveText('Saved revision 1.')
  await expect(page.getByRole('img', { name: 'Widget preview', exact: true }).locator('text')).toHaveText(text)
  await tabTo(page.getByRole('button', { name: 'Download widget definition', exact: true }), 'Download widget definition')
  const downloaded = page.waitForEvent('download')
  await page.keyboard.press('Enter')
  const file = await downloaded
  const actual = JSON.parse(await readFile(await file.path(), 'utf8'))
  expect(actual).toEqual([...documents.values()][0].graph)
  expect(actual.formats.la.els[0].text).toBe(text)
  expect(actual.formats.la.els[0].color).toBe('slate')
  expect(errors).toEqual([])
  const report = testInfo.outputPath('keyboard-proof.json')
  await import('node:fs/promises').then(({ writeFile }) => writeFile(report, JSON.stringify({ actions, actual, fixture: true, keyboardOnlyAfterInitialFocus: true })))
  await testInfo.attach('keyboard-proof.json', { path: report, contentType: 'application/json' })
  const still = testInfo.outputPath('keyboard-widget.png')
  await page.screenshot({ path: still })
  await testInfo.attach('keyboard-widget.png', { path: still, contentType: 'image/png' })
})
