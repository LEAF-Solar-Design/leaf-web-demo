import { expect, test } from '@playwright/test'

test('a signed-out visitor can ask to be notified', async ({ page }) => {
  const requests = []
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    requests.push({ method: request.method(), path: url.pathname, body: request.postDataJSON?.() })
    if (url.pathname === '/api/session') {
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: { message: 'sign in required' } }) })
      return
    }
    if (url.pathname === '/api/demand') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, stored: true, duplicate: false }) })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/try')
  await expect(page.getByTestId('demand-capture-card')).toBeVisible()
  await page.getByLabel('Work email').fill('person@example.com')
  await page.getByLabel('What are you trying to automate?').fill('Solar stringing reviews')
  await page.getByRole('button', { name: 'Notify me' }).click()

  await expect(page.getByText('Thanks, we’ll notify you.')).toBeVisible()
  expect(requests.filter((request) => request.path === '/api/demand')).toEqual([
    { method: 'POST', path: '/api/demand', body: { email: 'person@example.com', interest: 'Solar stringing reviews', org: null } },
  ])
})
