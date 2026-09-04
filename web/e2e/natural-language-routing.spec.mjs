import { expect, test } from '@playwright/test'
import { REQUEST, catProofResponse, makeCatProofState } from './catProofFixture.mjs'

async function install(page, override) {
  const state = makeCatProofState()
  const calls = []
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    calls.push({ method: request.method(), path: url.pathname, body })
    if (await override?.({ route, request, url, body, state, calls })) return
    const result = catProofResponse({ method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })
  return { state, calls }
}

test('ordinary language routes to a confident catalog proposal before any execution', async ({ page }) => {
  const evidence = await install(page, async ({ route, url }) => {
    if (url.pathname !== '/api/nl-prompt') return false
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ lane: 'run', tool: 'count-panels', params: {}, confidence: 0.94, rationale: 'Direct catalog match.', alternatives: [] }),
    })
    return true
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByLabel('Command bar', { exact: true }).fill('Count every panel in this drawing')
  await page.getByRole('button', { name: 'Run', exact: true }).click()

  await expect(page.getByRole('button', { name: 'Run count-panels' })).toBeVisible()
  expect(evidence.calls.filter((call) => call.path === '/api/nl-prompt')).toHaveLength(1)
  expect(evidence.calls.some((call) => call.path === '/api/sessions')).toBe(false)
  expect(evidence.calls.some((call) => call.path === '/api/run')).toBe(false)
})

test('the cat request is classified before Claude receives the same request and hint', async ({ page }) => {
  const evidence = await install(page)
  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByLabel('Command bar', { exact: true }).fill(REQUEST)
  await page.getByRole('button', { name: 'Run', exact: true }).click()

  await expect(page.getByTestId('operator-surface').getByRole('button', { name: 'Approve' })).toBeVisible({ timeout: 12_000 })
  const routeCall = evidence.calls.find((call) => call.path === '/api/nl-prompt')
  const messageCall = evidence.calls.find((call) => call.path === '/api/sessions/cat-session/messages')
  expect(routeCall?.body).toEqual({ text: REQUEST })
  expect(messageCall?.body.text).toBe(REQUEST)
  expect(messageCall?.body.classifier_hint).toMatchObject({ lane: 'build', tool: null, confidence: 0.42 })
  expect(evidence.calls.indexOf(routeCall)).toBeLessThan(evidence.calls.indexOf(messageCall))
})

test('router transport loss falls back visibly to local catalog matching', async ({ page }) => {
  const entitlementLoaded = page.waitForResponse((response) => response.url().endsWith('/api/entitlements'))
  const evidence = await install(page, async ({ route, url }) => {
    if (url.pathname === '/api/entitlements') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ tier: 'catalog-only', entitlements: { run_read: true, run_write: true, build: true, converse: false } }),
      })
      return true
    }
    if (url.pathname === '/api/nl-prompt') {
      await route.abort('connectionfailed')
      return true
    }
    return false
  })

  await page.goto('/try')
  await entitlementLoaded
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByLabel('Command bar', { exact: true }).fill('Inspect this drawing for unusual geometry')
  await page.getByRole('button', { name: 'Run', exact: true }).click()

  const resolver = page.getByRole('listbox', { name: 'Route resolver' })
  await expect(resolver).toContainText('Routing service unavailable. Using local catalog matching.')
  await expect(resolver).toContainText('count-panels')
  const options = resolver.getByRole('option')
  await expect(options).toHaveCount(2)
  await options.first().focus()
  await page.keyboard.press('ArrowDown')
  await expect(options.nth(1)).toBeFocused()
  expect(evidence.calls.some((call) => call.path === '/api/sessions')).toBe(false)
  expect(evidence.calls.some((call) => call.path === '/api/run')).toBe(false)
})

test('no match survives Claude quota failure as an honest resolver', async ({ page }) => {
  const evidence = await install(page, async ({ route, url }) => {
    if (url.pathname === '/api/nl-prompt') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ lane: 'run', tool: null, params: {}, confidence: 0.1, rationale: 'No registered capability matched.', alternatives: [] }),
      })
      return true
    }
    if (url.pathname === '/api/sessions') {
      await route.fulfill({
        status: 429,
        contentType: 'application/json',
        body: JSON.stringify({ error: { error_code: 'llm_quota_exhausted', message: 'quota exhausted' } }),
      })
      return true
    }
    return false
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByLabel('Command bar', { exact: true }).fill('Do the unusual thing')
  await page.getByRole('button', { name: 'Run', exact: true }).click()

  await expect(page.getByText('AI paused. Your built tools keep working.')).toBeVisible()
  await expect(page.getByRole('listbox', { name: 'Route resolver' })).toContainText('No matching capability')
  expect(evidence.calls.filter((call) => call.path === '/api/sessions')).toHaveLength(1)
  expect(evidence.calls.some((call) => call.path === '/api/run')).toBe(false)
})
