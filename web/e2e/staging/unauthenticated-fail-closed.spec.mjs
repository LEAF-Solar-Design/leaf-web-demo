import { expect, test } from '@playwright/test'

// Renamed from guest-run-fail-closed.spec.mjs. This is a pure security
// regression guard, not a capability-ledger proof: it deliberately writes no
// receipt and claims no row. The ledger's ID-01 requires authenticated,
// signed-out, AND expired-session-recovery proof; RN-01 requires read,
// write, build, solve, failure, AND retry proof. An anonymous refusal check
// evidences neither row on its own, so neither is claimed here.
//
// This also never calls a mutating endpoint. The previous version POSTed to
// /api/run: if auth were ever disabled or regressed on a deployed revision,
// that POST could enqueue a real job before the status assertion ran. The
// refusal is instead proven two ways that cannot mutate anything: (a) the
// real HTML-disabled state of the UI controls that would need to enable
// mutation, and (b) an unauthenticated GET against a protected read
// endpoint, which must return exactly 401 with error_code exactly
// UNAUTHENTICATED. A generic 403 from a WAF or proxy is treated as a
// FAILURE, not a pass, because it would prove only that something in front
// of the app is blocking the request, not that the app's own auth gate is
// intact.
test('a signed-out visitor on staging cannot reach a mutating or protected action', async ({ page, request }) => {
  await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })

  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible({ timeout: 15_000 })
  const runButton = page.getByRole('button', { name: 'Run', exact: true })
  await expect(runButton).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Upload DWG or DXF' })).toBeEnabled()

  await expect(page.locator('#workspace-tab-catalog')).toBeDisabled()
  await expect(page.locator('#workspace-tab-author')).toBeDisabled()
  await expect(page.locator('#workspace-tab-workspace')).toBeDisabled()
  await expect(page.locator('#operations-tab-versions')).toBeDisabled()
  await expect(page.locator('#operations-tab-jobs')).toBeDisabled()
  await expect(page.locator('#operations-tab-trust')).toBeDisabled()

  // Non-mutating: a GET against a protected read endpoint, no Authorization
  // header, no guest session header. Must fail closed with the app's own
  // auth error, not merely "some non-200".
  const denied = await request.get('/api/tools')
  expect(denied.status()).toBe(401)
  const deniedBody = await denied.json()
  expect(deniedBody?.ok).toBe(false)
  expect(deniedBody?.error?.error_code).toBe('UNAUTHENTICATED')
})
