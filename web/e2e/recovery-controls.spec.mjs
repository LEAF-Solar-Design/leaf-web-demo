// stu2-d lane 3: browser proof for recovery and unavailable controls.
//
// One state matrix over four controls, each driven through the real dev
// server with mocked APIs:
//   - JobInbox: a notice with a recorded action retries exactly once per
//     click; a notice without one shows a disabled Retry with its reason and
//     dispatches nothing.
//   - CustomizePanel: no entry and no drawer without platform_customize; with
//     it, an empty proposal is refused locally, 403 and 503 refusals each cost
//     exactly one request, and an awaiting_cosign record offers no Land.
//   - DrawingUploadControl: a policy with enabled false (and a failed policy
//     read) disables the upload button and the controller refuses the file
//     without dispatching an upload (createDrawingUploadController.js:50).
//   - OperatorEntry: hidden after a 401, 404 or 503 probe, shown after a 200
//     (OperatorEntry.jsx:5). probeOperatorConsole caches at module scope, so
//     every probe state runs in its own test, which is its own fresh page.
//
// Run from web/ with LEAF_NATIVE_GATE_WORKER set so the dev server port
// (5185 + 100 * N) does not collide with parallel lanes.

import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

const API = 'http://leaf-proof.invalid/api/**'
const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' }
const SETTLE_MS = 750

// Routes every API call through the cat proof fixture unless `override`
// answers it first. Returns the log of non-preflight requests as
// "METHOD /path" strings so a test can count dispatches exactly.
async function routeApi(page, override = () => null) {
  const state = makeCatProofState()
  const log = []
  await page.route(API, async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const path = url.pathname
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    if (method !== 'OPTIONS') log.push(`${method} ${path}`)
    const result = (method === 'OPTIONS' ? null : override({ method, path, body }))
      ?? catProofResponse({ method, path, body, query: Object.fromEntries(url.searchParams) }, state)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: CORS,
    })
  })
  return log
}

const count = (log, entry) => log.filter((line) => line === entry).length

async function signIn(page) {
  await page.addInitScript(() => {
    localStorage.setItem('leaf.jwt', 'fixture-token')
    localStorage.setItem('leaf.coach.dismissed.v1', '1')
    localStorage.setItem('leaf.org_id', 'cat-proof-org')
  })
}

async function openConsole(page, query = '') {
  await page.goto(`/app?surface=browser${query}`)
  await expect(page.locator('[data-ground="browser"]')).toBeVisible({ timeout: 20_000 })
}

test.describe('JobInbox recovery', () => {
  test('a recorded retry dispatches once per click and a notice without one dispatches nothing', async ({ page }) => {
    test.setTimeout(90_000)
    await signIn(page)
    const log = await routeApi(page, ({ path }) => (
      path.startsWith('/api/recovery-proof/') ? { status: 200, body: { ok: true } } : null
    ))
    await openConsole(page)
    const inbox = page.locator('aside.job-inbox')
    await expect(inbox).toBeVisible({ timeout: 20_000 })

    // Push onto the SAME singleton bus the app reads (vite serves the source
    // module at one URL, so this import is the app's own instance).
    await page.evaluate(async () => {
      const { notificationBus } = await import('/src/lib/notifications.js')
      window.__recoveryRetries = 0
      notificationBus.push({ kind: 'warn', text: 'Recovery proof: export has no follow-up' })
      notificationBus.push({
        kind: 'error',
        text: 'Recovery proof: count-panels failed',
        action: {
          label: 'Re-run count-panels',
          onClick: () => {
            window.__recoveryRetries += 1
            fetch('http://leaf-proof.invalid/api/recovery-proof/retry').catch(() => {})
          },
        },
      })
    })

    const retryRow = inbox.locator('.inbox-row').filter({ hasText: 'Recovery proof: count-panels failed' })
    const plainRow = inbox.locator('.inbox-row').filter({ hasText: 'Recovery proof: export has no follow-up' })
    await expect(retryRow).toHaveCount(1)
    await expect(plainRow).toHaveCount(1)

    // Retry with a recorded action: enabled, and one activation is one
    // dispatch. At 1600x1000 on the Browser surface the board's .ground-desk
    // takes the pointer over the expanded inbox, so the row is driven the way
    // JobInbox.jsx:12 documents for keyboard users: focus plus Enter on the
    // native button, which fires the same onClick a pointer press would.
    const retry = retryRow.getByRole('button', { name: 'Retry · Re-run count-panels', exact: true })
    await expect(retry).toBeEnabled()
    await expect(retryRow.locator('.lock-note')).toHaveCount(0)
    await retry.press('Enter')
    await expect.poll(() => count(log, 'GET /api/recovery-proof/retry')).toBe(1)
    await page.waitForTimeout(SETTLE_MS)
    expect(await page.evaluate(() => window.__recoveryRetries)).toBe(1)
    expect(count(log, 'GET /api/recovery-proof/retry')).toBe(1)

    // No recorded action: disabled with its reason in prose, and even a
    // forced click event dispatches nothing.
    const dead = plainRow.getByRole('button', { name: 'Retry', exact: true })
    await expect(dead).toBeDisabled()
    await expect(plainRow.locator('.lock-note')).toHaveText(
      'This notice recorded no follow-up action, so there is nothing here to retry.',
    )
    await dead.dispatchEvent('click')
    await page.waitForTimeout(SETTLE_MS)
    expect(await page.evaluate(() => window.__recoveryRetries)).toBe(1)
    expect(log.filter((line) => line.includes('/api/recovery-proof/'))).toEqual(['GET /api/recovery-proof/retry'])

    // Dismiss hides the row from this view only; nothing is dispatched.
    await retryRow.getByRole('button', { name: 'Dismiss', exact: true }).press('Enter')
    await expect(retryRow).toHaveCount(0)
    await expect(plainRow).toHaveCount(1)
    expect(log.filter((line) => line.includes('/api/recovery-proof/'))).toEqual(['GET /api/recovery-proof/retry'])
  })
})

test.describe('CustomizePanel refusal', () => {
  test('without platform_customize neither the entry nor the deep-linked drawer mounts', async ({ page }) => {
    test.setTimeout(90_000)
    await signIn(page)
    const log = await routeApi(page)
    await openConsole(page, '&customize=1')
    await expect.poll(() => count(log, 'GET /api/entitlements')).toBeGreaterThan(0)
    await page.waitForTimeout(SETTLE_MS)
    await expect(page.locator('header.top').getByRole('button', { name: 'Customize', exact: true })).toHaveCount(0)
    await expect(page.getByRole('dialog', { name: 'Platform self-edit' })).toHaveCount(0)
    expect(log.filter((line) => line.includes('/api/platform/customize'))).toEqual([])
  })

  test('an entitled admin sees each refusal once and no Land on an awaiting co-sign record', async ({ page }) => {
    test.setTimeout(90_000)
    await signIn(page)
    const replies = [
      { status: 403, body: { detail: 'admin tier required', entitlement_required: 'platform_customize' } },
      { status: 503, body: { detail: 'entitlements policy unavailable', entitlement_required: 'platform_customize' } },
      {
        status: 200,
        body: {
          change_id: '22222222-2222-4222-8222-222222222222',
          title: 'Recovery proof edit',
          state: 'awaiting_cosign',
          branch: 'platform-customize/recovery-proof',
          commit_sha: 'a'.repeat(40),
          fundamental_paths: ['web/src/recovery-proof.txt'],
        },
      },
    ]
    const log = await routeApi(page, ({ method, path }) => {
      if (path === '/api/entitlements') {
        return {
          status: 200,
          body: { tier: 'admin', entitlements: { run_read: true, run_write: true, build: true, converse: true, platform_customize: true } },
        }
      }
      if (path === '/api/platform/customize' && method === 'POST') return replies.shift() ?? { status: 500, body: { detail: 'unexpected extra proposal' } }
      return null
    })
    const proposals = () => count(log, 'POST /api/platform/customize')
    // The entitled header entry mounts, but at 1600x1000 on the Browser
    // surface the product-surface tabs (#product-surface-tab-solar, -ios)
    // take the pointer over it, so the drawer is reached through the
    // ?customize=1 deep link App.jsx:234 supports. Drawer buttons are driven
    // with focus plus Enter on the native button for the same reason.
    await openConsole(page, '&customize=1')

    const entry = page.locator('header.top').getByRole('button', { name: 'Customize', exact: true })
    await expect(entry).toBeVisible({ timeout: 20_000 })
    const drawer = page.getByRole('dialog', { name: 'Platform self-edit' })
    await expect(drawer).toBeVisible({ timeout: 20_000 })
    const propose = drawer.getByRole('button', { name: 'Propose', exact: true })
    const alert = drawer.getByRole('alert')

    // Empty proposal: refused in the browser, nothing dispatched.
    await propose.press('Enter')
    await expect(alert).toContainText('A title and at least one edit with a path are required.')
    await page.waitForTimeout(SETTLE_MS)
    expect(proposals()).toBe(0)

    await drawer.getByLabel('Title', { exact: true }).fill('Recovery proof edit')
    await drawer.getByLabel('Path 1', { exact: true }).fill('web/src/recovery-proof.txt')
    await drawer.getByLabel('Content (full file)', { exact: true }).fill('proof\n')

    // 403: the tier refusal, one request.
    await propose.press('Enter')
    await expect(alert).toContainText('Admin tier required for platform self-edit.')
    await expect(propose).toBeEnabled()
    expect(proposals()).toBe(1)

    // 503 with the entitlement marker: the retryable policy outage, one more.
    await propose.press('Enter')
    await expect(alert).toContainText('The entitlements policy is unavailable right now. Try again shortly.')
    await expect(propose).toBeEnabled()
    expect(proposals()).toBe(2)

    // The retry lands: an awaiting_cosign record renders a calm hold with no
    // Land or Merge control, because co-sign never happens in the browser.
    await propose.press('Enter')
    await expect(drawer.getByText('Awaiting co-sign', { exact: true })).toBeVisible()
    await expect(drawer.getByText(/Fundamental paths need an independent co-sign before landing/)).toBeVisible()
    await expect(alert).toHaveCount(0)
    await expect(drawer.getByRole('button', { name: 'Land', exact: true })).toHaveCount(0)
    await expect(drawer.getByRole('button', { name: 'Merge', exact: true })).toHaveCount(0)
    await page.waitForTimeout(SETTLE_MS)
    expect(proposals()).toBe(3)
    expect(log.filter((line) => /\/api\/platform\/customize\/.+\/(land|merge)$/.test(line))).toEqual([])
  })
})

test.describe('DrawingUploadControl policy refusal', () => {
  const DXF = { name: 'recovery-proof.dxf', mimeType: 'application/dxf', buffer: Buffer.from('0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n') }

  async function openTry(page, policyReply) {
    const log = await routeApi(page, ({ path }) => (path === '/api/site/guest-upload-policy' ? policyReply : null))
    await page.goto('/try?proof=1')
    await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
    await expect.poll(() => count(log, 'GET /api/site/guest-upload-policy')).toBeGreaterThan(0)
    return log
  }

  async function refusesFile(page, log) {
    const control = page.locator('.drawing-upload')
    await control.getByLabel('Drawing file').setInputFiles(DXF)
    await expect(control.getByRole('alert')).toHaveText('Drawing uploads are not available on this deployment.')
    // The command bar's drop path reaches the same controller and is refused the same way.
    await page.locator('.tc-bar').evaluate((bar) => {
      const transfer = new DataTransfer()
      transfer.items.add(new File(['0\nEOF\n'], 'recovery-drop.dxf', { type: 'application/dxf' }))
      bar.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: transfer }))
    })
    await page.waitForTimeout(SETTLE_MS)
    await expect(control.getByRole('alert')).toHaveText('Drawing uploads are not available on this deployment.')
    expect(count(log, 'POST /api/drawings/upload')).toBe(0)
  }

  test('a disabled policy disables the button and the controller refuses the file without uploading', async ({ page }) => {
    test.setTimeout(90_000)
    const log = await openTry(page, {
      status: 200,
      body: { enabled: false, retention_hours: 24, max_bytes: 1024 * 1024, accepted: ['.dwg', '.dxf'], extract_live: false, dxf_local_ok: true },
    })
    const control = page.locator('.drawing-upload')
    await expect(control.getByRole('button', { name: 'Upload DWG or DXF', exact: true })).toBeDisabled()
    await expect(control.locator('.drawing-upload-note')).toHaveText('Uploads unavailable')
    await expect(control.getByRole('alert')).toHaveCount(0)
    await refusesFile(page, log)
  })

  test('a failed policy read reads as unavailable and still dispatches no upload', async ({ page }) => {
    test.setTimeout(90_000)
    const log = await openTry(page, { status: 503, body: { detail: 'upload policy unavailable' } })
    const control = page.locator('.drawing-upload')
    const alert = control.getByRole('alert')
    await expect(alert).toHaveText('Uploads are unavailable right now.')
    await expect(alert).toHaveAttribute('title', 'GET /api/site/guest-upload-policy -> 503')
    await expect(control.getByRole('button', { name: 'Upload DWG or DXF', exact: true })).toBeDisabled()
    await refusesFile(page, log)
  })
})

test.describe('OperatorEntry probe', () => {
  for (const status of [401, 404, 503]) {
    test(`a ${status} probe hides the operator entry and is never repeated`, async ({ page }) => {
      test.setTimeout(90_000)
      await signIn(page)
      const log = await routeApi(page, ({ path }) => (
        path === '/api/operator/sessions' ? { status, body: { detail: `operator probe ${status}` } } : null
      ))
      const probed = page.waitForResponse((response) => response.url().endsWith('/api/operator/sessions'))
      await openConsole(page)
      expect((await probed).status()).toBe(status)
      await page.waitForTimeout(SETTLE_MS)
      await expect(page.getByRole('button', { name: 'Open operator console', exact: true })).toHaveCount(0)
      expect(count(log, 'GET /api/operator/sessions')).toBe(1)
    })
  }

  test('a 200 probe shows the operator entry after exactly one probe', async ({ page }) => {
    test.setTimeout(90_000)
    await signIn(page)
    const log = await routeApi(page, ({ path }) => (
      path === '/api/operator/sessions' ? { status: 200, body: { sessions: [] } } : null
    ))
    await openConsole(page)
    const entry = page.getByRole('button', { name: 'Open operator console', exact: true })
    await expect(entry).toBeVisible({ timeout: 20_000 })
    await expect(entry).toHaveText('Operator')
    await page.waitForTimeout(SETTLE_MS)
    expect(count(log, 'GET /api/operator/sessions')).toBe(1)
  })
})
