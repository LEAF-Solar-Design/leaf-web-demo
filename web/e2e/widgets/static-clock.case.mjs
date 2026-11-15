import { test, expect } from '@playwright/test'

test('literal production preview stays idle across two freshness periods', async ({ page }, testInfo) => {
  const requests = []
  await page.route('**/api/**', async route => {
    requests.push(new URL(route.request().url()).pathname)
    if (route.request().method() === 'POST' && new URL(route.request().url()).pathname.endsWith('/save')) {
      await route.fulfill({ json: { revision: 1, sha256: 'b'.repeat(64) } })
    } else await route.fulfill({ status: 404, json: { detail: 'isolated fixture' } })
  })
  await page.addInitScript(() => {
    const now = Date.now
    window.__widgetClockProof = { dateReads: 0, fiveSecondSchedules: 0 }
    Date.now = () => { window.__widgetClockProof.dateReads++; return now() }
    const timeout = window.setTimeout
    window.setTimeout = (callback, delay, ...args) => {
      if (Number(delay) === 5000) window.__widgetClockProof.fiveSecondSchedules++
      return timeout(callback, delay, ...args)
    }
  })
  await page.goto('/e2e/widgets/harness.html')
  await page.getByLabel('Document ID', { exact: true }).fill('static-clock-proof')
  await page.getByRole('button', { name: 'New', exact: true }).click()
  await page.getByRole('button', { name: 'Add text', exact: true }).click()
  await page.getByLabel('Text', { exact: true }).fill('Static clock proof')
  for (const [name, value] of [['X', 14], ['Y', 14], ['W', 330], ['H', 24]]) {
    const control = page.getByLabel(name, { exact: true })
    await control.fill(String(value)); await control.press('Enter')
  }
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByRole('status').first()).toHaveText('Saved revision 1.')
  const preview = page.getByRole('region', { name: 'Live widget preview', exact: true }).getByRole('img')
  await expect(preview.locator('text')).toHaveText('Static clock proof')
  await expect(preview.locator('text')).toHaveCSS('fill', 'rgb(255, 255, 255)')
  // No actions during the observation window. The isolated production fixture
  // has no other freshness clock; this is not a frame-rate benchmark.
  await page.waitForTimeout(250)
  const before = await page.evaluate(() => ({ ...window.__widgetClockProof }))
  const requestCount = requests.length
  await page.waitForTimeout(11000)
  const after = await page.evaluate(() => ({ ...window.__widgetClockProof }))
  const proof = { seconds: 11, dateReads: after.dateReads - before.dateReads,
    fiveSecondSchedules: after.fiveSecondSchedules - before.fiveSecondSchedules,
    apiRequests: requests.slice(requestCount), fixture: true, productionBuild: true }
  await testInfo.attach('static-clock-proof.json', { body: JSON.stringify(proof), contentType: 'application/json' })
  // Browser-wide Date.now includes automation calls; record, never attribute it.
  expect(proof.fiveSecondSchedules).toBe(0)
  expect(proof.apiRequests).toEqual([])
  await expect(preview.locator('text')).toHaveText('Static clock proof')

})
