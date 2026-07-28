import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

test('session 401 renders one calm gate and disables execution', async ({ page }) => {
  const state = makeCatProofState()
  const calls = []
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    calls.push({ method: request.method(), path: url.pathname })
    if (url.pathname === '/api/session') {
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: { message: 'sign in required' } }) })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })
  await page.goto('/try')
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeDisabled()
  await expect(page.getByText(/drawing backend is unavailable/i)).toHaveCount(0)
  await page.getByRole('textbox', { name: 'Command bar' }).fill('hello')
  await page.getByRole('textbox', { name: 'Command bar' }).press('Enter')
  await page.waitForTimeout(250)
  expect(calls.some((call) => call.path === '/api/nl-prompt')).toBe(false)
  expect(calls.filter((call) => call.path !== '/api/session' && !call.path.startsWith('/api/site/'))).toEqual([])
  await expect(page.getByText(/Could not route the request/i)).toHaveCount(0)
  await expect(page).toHaveURL(/\/try$/)
})

test('a valid first login can create its owner workspace and reach Claude mounts', async ({ page }) => {
  const state = makeCatProofState()
  const calls = []
  let provisioned = false
  await page.addInitScript(() => localStorage.setItem('leaf.jwt', 'fixture-token'))
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    calls.push({ method: request.method(), path: url.pathname, authorization: request.headers().authorization })
    if (url.pathname === '/api/session' && !provisioned) {
      await route.fulfill({
        status: 403,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'verified subject has no active platform tenant authority' }),
      })
      return
    }
    if (url.pathname === '/api/orgs' && request.method() === 'POST') {
      provisioned = true
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ org: { org_id: 'cat-proof-org', name: 'My solar workspace', tier: 'hosted_starter', status: 'active' } }),
      })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname, body: request.postDataJSON?.() }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByRole('heading', { name: 'Create your Leaf workspace' })).toBeVisible()
  await expect(page.getByText(/drawing backend is unavailable/i)).toHaveCount(0)
  await page.getByRole('textbox', { name: 'Workspace name' }).fill('My solar workspace')
  await page.getByRole('button', { name: 'Create workspace' }).click()

  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByRole('tab', { name: 'Trust' }).click()
  await expect(page.getByRole('button', { name: /Claude accounts/ })).toBeVisible()
  expect(calls.filter((call) => call.path === '/api/orgs')).toEqual([
    { method: 'POST', path: '/api/orgs', authorization: 'Bearer fixture-token' },
  ])
  expect(calls.filter((call) => call.path === '/api/session').length).toBeGreaterThanOrEqual(2)
})

test('a malformed Auth0 tenant claim cannot enter workspace bootstrap', async ({ page }) => {
  const state = makeCatProofState()
  await page.addInitScript(() => localStorage.setItem('leaf.jwt', 'fixture-token'))
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/session') {
      await route.fulfill({
        status: 403,
        contentType: 'application/json',
        body: JSON.stringify({ detail: "token verified but missing tenant claim 'https://leafdesign.ai/tenant_id'" }),
      })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByRole('heading', { name: 'Create your Leaf workspace' })).toHaveCount(0)
  await expect(page.getByText(/drawing backend is unavailable/i)).toBeVisible()
})

test('Explore the demo keeps the CAD operator on try and runs without private APIs', async ({ page }) => {
  const state = makeCatProofState()
  const calls = []
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    calls.push({ method: request.method(), path: url.pathname })
    if (url.pathname === '/api/session') {
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: { message: 'sign in required' } }) })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible()
  await page.getByRole('button', { name: 'Explore the demo' }).click()
  await expect(page).toHaveURL(/\/try\?demo=1$/)
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await expect(page.getByText('Interactive demo')).toBeVisible()
  await expect(page.getByText('Ask Claude for the cat edit')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled()

  calls.length = 0
  await page.getByRole('textbox', { name: 'Command bar' }).fill('hello')
  await page.getByRole('textbox', { name: 'Command bar' }).press('Enter')
  await expect(page.getByTestId('demo-conversation')).toContainText('hello')
  await expect(page.getByTestId('demo-conversation')).toContainText('interactive Leaf CAD demo')
  await expect(page.getByRole('textbox', { name: 'Command bar' })).toHaveValue('')
  await expect(page.getByRole('button', { name: 'Run count-by-layer' })).toHaveCount(0)
  expect(calls.filter((call) => call.path.startsWith('/api/') && !call.path.startsWith('/api/site/'))).toEqual([])

  await page.getByRole('textbox', { name: 'Command bar' }).fill('count panels per layer')
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await expect(page.getByTestId('demo-conversation')).toContainText('count-by-layer')
  await expect(page.getByRole('button', { name: 'Run count-by-layer' })).toBeVisible()
  await page.getByRole('button', { name: 'Run count-by-layer' }).click()
  await expect(page.getByRole('tab', { name: 'Execution' })).toBeVisible()
  await page.getByRole('tab', { name: 'Execution' }).click()
  await expect(page.getByTestId('catalog-run-result')).toContainText('count-by-layer', { timeout: 15_000 })
  expect(calls.filter((call) => call.path.startsWith('/api/') && !call.path.startsWith('/api/site/'))).toEqual([])
})

test('signed-in demo URL keeps the CAD surface and uses the live conversation session', async ({ page }) => {
  const state = makeCatProofState()
  const calls = []
  await page.addInitScript(() => localStorage.setItem('leaf.jwt', 'fixture-token'))
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    calls.push({ method: request.method(), path: url.pathname })
    const result = catProofResponse({ method: request.method(), path: url.pathname, body: request.postDataJSON?.() }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try?demo=1')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await expect(page.getByTestId('demo-conversation')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeEnabled()
  await page.getByRole('textbox', { name: 'Command bar' }).fill('hello from the mounted account')
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  await expect.poll(() => calls.some((call) => call.method === 'POST' && call.path === '/api/sessions')).toBe(true)
  await expect.poll(() => calls.some((call) => call.method === 'POST' && call.path === '/api/sessions/cat-session/messages')).toBe(true)
})

test('a Claude grant-required response does not expire the separate Leaf login', async ({ page }) => {
  const state = makeCatProofState()
  await page.addInitScript(() => localStorage.setItem('leaf.jwt', 'fixture-token'))
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() === 'POST' && url.pathname === '/api/sessions/cat-session/messages') {
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({
          grant_required: true,
          error: { error_code: 'GRANT_REQUIRED', message: 'mount a Claude account', retryable: false },
          degraded_mode: false,
        }),
      })
      return
    }
    const result = catProofResponse({
      method: request.method(),
      path: url.pathname,
      body: request.postDataJSON?.(),
    }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByRole('textbox', { name: 'Command bar' }).fill('hello from a Leaf session')
  await page.getByRole('button', { name: 'Run', exact: true }).click()

  await expect(page.getByText('Chat needs a linked Claude account.')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toHaveCount(0)
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.jwt'))).toBe('fixture-token')
})

test('an Auth0 callback aimed at try does not boot the legacy app', async ({ page }) => {
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })
  await page.goto('/try?code=fixture-code&state=fixture-state')
  await expect(page.getByTestId('operator-surface')).toBeVisible({ timeout: 15_000 })
  await expect(page).toHaveURL(/\/try\?code=fixture-code&state=fixture-state$/)
  await expect(page.getByRole('tablist', { name: 'Workspace panels' })).toBeVisible()
})

test('a mid-session 401 latches the whole scene into one gate and stops job polling', async ({ page }) => {
  const state = makeCatProofState()
  let expired = false
  let jobReads = 0
  await page.addInitScript(() => localStorage.setItem('leaf.jwt', 'fixture-token'))
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/jobs') jobReads += 1
    if (expired && (url.pathname === '/api/jobs' || (url.pathname.endsWith('/checkout') && request.method() === 'POST'))) {
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: { message: 'session expired' } }) })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  expired = true
  await page.getByRole('button', { name: 'Take edit lock' }).click()
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeDisabled()
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.jwt'))).toBeNull()
  await page.waitForTimeout(3_500)
  const settledReads = jobReads
  await page.waitForTimeout(3_000)
  expect(jobReads).toBe(settledReads)
})

test('sign out is available for any stored session and returns to the gate', async ({ page }) => {
  const state = makeCatProofState()
  await page.addInitScript(() => {
    if (sessionStorage.getItem('leaf.session.seeded')) return
    sessionStorage.setItem('leaf.session.seeded', '1')
    localStorage.setItem('leaf.jwt', 'fixture-token')
  })
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/session' && !request.headers().authorization) {
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: { message: 'sign in required' } }) })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByRole('tab', { name: 'Trust' }).click()
  await page.getByRole('button', { name: 'Account details' }).click()
  await page.getByRole('dialog', { name: 'Account details' }).getByRole('button', { name: 'Sign out' }).click()
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible({ timeout: 15_000 })
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.jwt'))).toBeNull()
})
