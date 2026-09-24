import { expect, test } from '@playwright/test'
import { assertResponseOnAllowedOrigin } from './stagingConfig.mjs'
import { oneShellOn, resolveExpectedSha } from '../prod/prodConfig.mjs'

// The staging twin of the production candidate checks in
// e2e/prod/unified-prod-readonly.spec.mjs: the staged app and web must both
// serve the candidate being promoted, and the served runtime flags must turn
// oneShell on under the client's own rule. Read-only, no Authorization header,
// no receipt. The host allowlist is enforced by globalSetup and by
// assertResponseOnAllowedOrigin on every response.

test('staging app and web serve the expected candidate', async ({ request }) => {
  const expected = resolveExpectedSha()
  test.skip(!expected, 'LEAF_E2E_EXPECTED_SHA is not set')
  const app = await request.get('/api/health', { timeout: 10_000 })
  assertResponseOnAllowedOrigin(app)
  expect(app.ok()).toBeTruthy()
  const appHealth = await app.json()
  const web = await request.get('/health.json', { timeout: 10_000 })
  assertResponseOnAllowedOrigin(web)
  expect(web.ok()).toBeTruthy()
  const webHealth = await web.json()
  test.info().annotations.push({ type: 'expected-sha', description: expected })
  expect(appHealth.source_sha).toBe(expected)
  expect(webHealth.source_sha).toBe(expected)
})

test('staging runtime flags turn oneShell on', async ({ request }) => {
  const response = await request.get('/runtime-flags.js', { timeout: 10_000 })
  assertResponseOnAllowedOrigin(response)
  expect(response.ok()).toBeTruthy()
  expect(oneShellOn(await response.text())).toBe(true)
})
