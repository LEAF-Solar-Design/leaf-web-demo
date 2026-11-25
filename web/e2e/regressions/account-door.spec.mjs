import { test, expect } from './_harness.mjs'

// The cover page's account door replaced the waitlist form (rehearsal issue 1): a signed-out
// visitor gets "Sign in / Create account" and "Create your Studio account", both opening /app,
// where the first sign-in creates the workspace.
test('signed-out cover page offers the account door and opens /app', async ({ page, stack }) => {
  const response = await page.goto('/')
  expect(response.ok()).toBe(true)
  expect(new URL(page.url()).origin).toBe(stack.baseURL)
  await expect(page.getByRole('form', { name: 'Register interest', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Sign in / Create account', exact: true })).toBeVisible()
  const create = page.getByRole('button', { name: 'Create your Studio account', exact: true })
  await expect(create).toBeVisible()
  await create.click()
  await expect.poll(() => new URL(page.url()).pathname).toBe('/app')
})
