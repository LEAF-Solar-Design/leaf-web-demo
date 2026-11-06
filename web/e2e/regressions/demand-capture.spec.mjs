import { test, expect } from './_harness.mjs'

test('demand form saves interest and confirms it visibly', async ({ page, stack }) => {
  const response = await page.goto('/')
  expect(response.ok()).toBe(true)
  expect(new URL(page.url()).origin).toBe(stack.baseURL)
  // site/LandingCast.jsx: named form, email input, submit button and status.
  // api.js submitDemandCapture sends the real POST /api/demand.
  const form = page.getByRole('form', { name: 'Register interest', exact: true })
  await expect(form).toBeVisible()
  const saved = page.waitForResponse((reply) => reply.request().method() === 'POST'
    && new URL(reply.url()).origin === stack.baseURL
    && new URL(reply.url()).pathname === '/api/demand', { timeout: 15_000 })
  await form.getByLabel('Want a workspace that uses your own API key?', { exact: true }).fill('studio-s7a@example.test')
  await form.getByRole('button', { name: 'Register interest', exact: true }).click()
  const reply = await saved
  expect(reply.fromServiceWorker()).toBe(false)
  expect(reply.status()).toBeGreaterThanOrEqual(200)
  expect(reply.status()).toBeLessThan(300)
  expect(reply.request().postDataJSON()).toMatchObject({
    email: 'studio-s7a@example.test', interest: 'Workspace with your own API key',
  })
  await expect(form.getByRole('status')).toHaveText('Interest saved. No payment required.')
})
