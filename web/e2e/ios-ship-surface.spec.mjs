import { expect, test } from '@playwright/test'
import { CAT_PROJECT, CAT_PROJECT_VERSION, catProofResponse, makeCatProofState } from './catProofFixture.mjs'

// iOS ship-surface browser proof. Runs ONLY under playwright.ios.config.mjs,
// which serves the same /app from two Vite instances: one built with
// VITE_IOS_SURFACE=0 (project ios-flag-off) and one with VITE_IOS_SURFACE=1
// (project ios-flag-on). The flag is folded at build time (src/ios/flag.js),
// so the dormant/live split below is the real build fence, not a mock.
//
// Every walk is the same honest path: a signed-in console opens the Cat Roof
// workspace project on the Browser board, selects its canonical drawing
// version, then switches to the iOS tab, where useIosShipController owns
// readiness, launch, following and reload recovery. Only /api is mocked.

const API = 'http://leaf-proof.invalid/api/**'
const TENANT = 'cat-litmus-tenant'
const PROJECT_ID = CAT_PROJECT.project_id
const REVISION = CAT_PROJECT_VERSION.version_id
const POINTER_KEY = `leaf.ios-ship.pointer:${TENANT}:${PROJECT_ID}`
const IOS_PATH = /^\/api\/(?:ios-ship|ios-surface)\/|^\/api\/projects\/[^/]+\/ios\//
const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' }

const APPROVED_LAUNCH = Object.freeze({
  approval_id: 'ios-approval-0001',
  revision: REVISION,
  source_revision: 'a'.repeat(40),
  source_sha256: 'b'.repeat(64),
  bundle_identifier: 'ai.leafdesign.catroof',
  marketing_version: '1.0.0',
  build_number: '7',
})

function readinessRecord(patch = {}) {
  return {
    record_kind: 'leaf.ios-ship-readiness.v1',
    project_id: PROJECT_ID,
    healthy: true,
    launchable: true,
    grant_status: 'healthy',
    dispatch_available: true,
    reported_at: '2026-09-30T12:00:00Z',
    approved_launch: { ...APPROVED_LAUNCH },
    ...patch,
  }
}

const RECEIPT = Object.freeze({
  kind: 'leaf.ios-testflight-receipt.v1',
  receipt_id: 'ios-receipt-0001',
  created_at: '2026-09-30T12:20:00Z',
  ...APPROVED_LAUNCH,
  app_store_connect_result: { status: 'uploaded', build_id: 'asc-build-0007', beta_group: 'internal', uploaded_at: '2026-09-30T12:19:00Z' },
})

// Default iOS lane answers. A test overrides any of them; an iOS path nobody
// answers is a 404, never a silent success.
function iosLane(overrides = {}) {
  return {
    readiness: () => ({ status: 200, body: { readiness: readinessRecord() } }),
    sources: () => ({ status: 200, body: { ok: true, sources: [], approvals: [], can_approve: false } }),
    surface: () => ({ status: 200, body: { status: 'unavailable' } }),
    ...overrides,
  }
}

function answerIos(lane, call) {
  const { method, path } = call
  if (method === 'GET' && path === '/api/ios-ship/readiness') return lane.readiness?.(call)
  if (method === 'GET' && path === `/api/projects/${PROJECT_ID}/ios/sources`) return lane.sources?.(call)
  if (method === 'GET' && path === '/api/ios-surface/status') return lane.surface?.(call)
  if (method === 'POST' && path === '/api/ios-ship/launch') return lane.launch?.(call)
  if (method === 'GET' && path.startsWith('/api/ios-ship/executions/')) return lane.execution?.(call)
  if (method === 'GET' && path.startsWith('/api/ios-ship/receipts/')) return lane.receipt?.(call)
  return null
}

// Signed-in, org-bound console over the cat proof fixture, with the iOS lane
// answered by `lane`. Returns every non-preflight API call it saw.
async function installApi(page, lane = null) {
  const state = makeCatProofState()
  const calls = []
  await page.addInitScript(() => {
    localStorage.setItem('leaf.jwt', 'fixture-token')
    localStorage.setItem('leaf.org_id', 'cat-proof-org')
  })
  await page.route(API, async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    const call = { method, path: url.pathname, query: Object.fromEntries(url.searchParams), body, headers: request.headers() }
    if (method !== 'OPTIONS') calls.push(call)
    const result = method !== 'OPTIONS' && IOS_PATH.test(url.pathname)
      ? (lane && answerIos(lane, call)) || { status: 404, body: { error: { message: `unhandled iOS route ${method} ${url.pathname}` } } }
      : catProofResponse({ method, path: url.pathname, body, query: call.query }, state)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: CORS,
    })
  })
  return calls
}

const iosCalls = (calls) => calls.filter((call) => IOS_PATH.test(call.path))

async function seedPointer(page, executionId) {
  await page.evaluate(([key, value]) => sessionStorage.setItem(key, JSON.stringify(value)),
    [POINTER_KEY, { execution_id: executionId, revision: REVISION, at: '2026-09-30T12:00:00Z' }])
}

const readPointer = (page) => page.evaluate((key) => sessionStorage.getItem(key), POINTER_KEY)

// Browser board -> open Cat Roof -> select its canonical version. Leaves the
// console on the Browser surface with project and revision both concrete.
async function openProjectRevision(page, calls) {
  await page.goto('/app?surface=browser')
  await page.getByRole('region', { name: 'Workspace projects', exact: true })
    .getByRole('list', { name: 'Projects', exact: true })
    .getByRole('button', { name: CAT_PROJECT.name, exact: true })
    .click({ timeout: 30_000 })
  const version = page.locator('.studio-ground .workspace-summary select')
  await expect(version).toBeVisible({ timeout: 20_000 })
  await version.selectOption(REVISION)
  await expect(version).toHaveValue(REVISION)
  // The workspace really is live: hydrated from the API with the bearer.
  expect(calls.some((call) => call.method === 'GET' && call.path === `/api/projects/${PROJECT_ID}`
    && call.headers.authorization === 'Bearer fixture-token')).toBe(true)
}

async function showIosSurface(page) {
  await page.getByRole('tab', { name: 'iOS', exact: true }).click()
  await expect(page.getByRole('tab', { name: 'iOS', exact: true })).toHaveAttribute('aria-selected', 'true')
  const device = page.locator('.studio-ground [data-ground="ios"]')
  await expect(device).toBeVisible({ timeout: 20_000 })
  return device
}

const launchButton = (device) => device.getByRole('button', { name: 'Launch TestFlight build', exact: true })
const ribbonLaunch = (page) => page.getByTestId('drafting-ribbon').locator('[data-tool="ship:launch"]')

test.beforeEach(async ({}, testInfo) => {
  test.skip(typeof testInfo.project.metadata?.iosSurface !== 'boolean',
    'iOS ship-surface proof runs only under playwright.ios.config.mjs')
})

test('@flag-off the iOS surface stays dormant and sends no readiness, status, source or execution request', async ({ page }) => {
  const calls = await installApi(page, iosLane())
  await openProjectRevision(page, calls)
  // A stored execution pointer must not be read either while dormant.
  await seedPointer(page, 'ios-exec-dormant')
  const device = await showIosSurface(page)

  // The build fence renders the dormant placeholder (flag.js:11), never a state.
  const dormant = page.locator('section.ios-surface-dormant')
  await expect(dormant).toHaveCount(1)
  await expect(dormant).toHaveAttribute('data-state', 'dormant')
  await expect(dormant).toContainText(/iOS setup status isn.t available yet\./)
  await expect(page.locator('section.ios-surface:not(.ios-surface-dormant)')).toHaveCount(0)
  await expect(device).toHaveAttribute('data-state', 'idle')
  await expect(device.getByTestId('device-state')).toHaveText('idle')
  await expect(launchButton(device)).toHaveCount(0)
  await expect(ribbonLaunch(page)).toBeDisabled()

  // Project and revision are both concrete, so a live build would read now.
  // React effects fire within a frame; this window is generous on purpose.
  await page.waitForTimeout(1_500)
  expect(iosCalls(calls)).toEqual([])
  expect(JSON.parse(await readPointer(page))?.execution_id).toBe('ios-exec-dormant')
})

test('@flag-on a full readiness contract makes the approved revision launchable', async ({ page }) => {
  const calls = await installApi(page, iosLane())
  await openProjectRevision(page, calls)
  // On the Browser surface the ship controller is off: nothing read yet.
  expect(calls.filter((call) => call.path === '/api/ios-ship/readiness')).toEqual([])
  const device = await showIosSurface(page)

  await expect(device).toHaveAttribute('data-state', 'ready', { timeout: 20_000 })
  await expect(page.locator('section.ios-surface-dormant')).toHaveCount(0)
  await expect(launchButton(device)).toBeEnabled()
  await expect(ribbonLaunch(page)).toBeEnabled()
  for (const rung of ['source', 'grant', 'executor']) {
    await expect(device.locator(`[data-rung="${rung}"]`)).toHaveAttribute('data-state', 'ready')
  }
  await expect(device.locator('[data-rung="build"]')).toHaveAttribute('data-state', 'missing')
  await expect(device.getByRole('alert')).toHaveCount(0)

  const reads = calls.filter((call) => call.path === '/api/ios-ship/readiness')
  expect(reads.length).toBeGreaterThan(0)
  for (const read of reads) {
    expect(read.query).toEqual({ project_id: PROJECT_ID, revision: REVISION })
    expect(read.headers.authorization).toBe('Bearer fixture-token')
    expect(read.headers['x-tenant-id']).toBe(TENANT)
  }
  expect(calls.filter((call) => call.path === '/api/ios-ship/launch')).toEqual([])
})

test('@flag-on a launch progresses from running through the build stage to a delivered receipt', async ({ page }) => {
  const execution = { current: { status: 'running', stage: 'MAC_ALLOCATED' } }
  const calls = await installApi(page, iosLane({
    launch: () => ({ status: 200, body: { ok: true, execution: { execution_id: 'ios-exec-0001', status: 'running', stage: 'MAC_ALLOCATED', updated_at: '2026-09-30T12:01:00Z' } } }),
    execution: ({ path }) => path === '/api/ios-ship/executions/ios-exec-0001'
      ? { status: 200, body: { ok: true, execution: { execution_id: 'ios-exec-0001', updated_at: '2026-09-30T12:02:00Z', ...execution.current } } }
      : null,
    receipt: ({ path }) => path === `/api/ios-ship/receipts/${RECEIPT.receipt_id}`
      ? { status: 200, body: { ok: true, receipt: RECEIPT } }
      : null,
  }))
  await openProjectRevision(page, calls)
  const device = await showIosSurface(page)
  await expect(device).toHaveAttribute('data-state', 'ready', { timeout: 20_000 })

  await launchButton(device).click()
  await expect(device).toHaveAttribute('data-state', 'running', { timeout: 15_000 })
  await expect(device.locator('.device-stage')).toHaveText('MAC_ALLOCATED')
  await expect(device.locator('[data-rung="build"]')).toHaveAttribute('data-state', 'busy')
  await expect(device.locator('[data-rung="delivery"]')).toHaveAttribute('data-state', 'busy')
  await expect(launchButton(device)).toHaveCount(0)
  await expect(ribbonLaunch(page)).toBeDisabled()

  // The launch carried the exact approved record and its idempotency key.
  const launches = calls.filter((call) => call.path === '/api/ios-ship/launch')
  expect(launches).toHaveLength(1)
  expect(launches[0].body).toEqual({ project_id: PROJECT_ID, ...APPROVED_LAUNCH })
  expect(launches[0].headers['idempotency-key']).toBe(`ios-ship:${PROJECT_ID}:${APPROVED_LAUNCH.approval_id}`)
  // Reload recovery: the execution pointer is stored for this revision.
  await expect.poll(async () => JSON.parse(await readPointer(page))).toMatchObject({ execution_id: 'ios-exec-0001', revision: REVISION })

  // The follower polls; the stage advances only as the lane reports it.
  execution.current = { status: 'running', stage: 'BUILT' }
  await expect(device.locator('.device-stage')).toHaveText('BUILT', { timeout: 15_000 })
  await expect(device).toHaveAttribute('data-state', 'running')

  execution.current = { status: 'succeeded', stage: 'RECEIPT', receipt_id: RECEIPT.receipt_id }
  await expect(device).toHaveAttribute('data-state', 'succeeded', { timeout: 15_000 })
  await expect(device.locator('[data-rung="build"]')).toHaveAttribute('data-state', 'ready')
  await expect(device.locator('[data-rung="delivery"]')).toHaveAttribute('data-state', 'ready', { timeout: 15_000 })
  expect(calls.some((call) => call.path === `/api/ios-ship/receipts/${RECEIPT.receipt_id}`
    && call.query.project_id === PROJECT_ID)).toBe(true)
  // A settled execution releases its pointer.
  await expect.poll(() => readPointer(page)).toBeNull()
  await expect(device.getByRole('alert')).toHaveCount(0)
  expect(calls.filter((call) => call.path === '/api/ios-ship/launch')).toHaveLength(1)
})

// Launch requires the FULL readiness contract (iosShipReadiness.js
// validateIosShipReadiness): each record below drops or bends one term and
// must leave the lane unlaunchable without ever posting a launch.
const MALFORMED = [
  {
    name: 'an approved launch missing its source digest',
    record: () => readinessRecord({ approved_launch: { ...APPROVED_LAUNCH, source_sha256: undefined } }),
    phase: 'unavailable',
  },
  {
    name: 'an unavailable dispatch',
    record: () => readinessRecord({ dispatch_available: false }),
    phase: 'unavailable',
  },
  {
    name: 'a foreign record kind',
    record: () => readinessRecord({ record_kind: 'leaf.ios-ship-readiness.v0' }),
    phase: 'unavailable',
  },
  {
    name: 'another project',
    record: () => readinessRecord({ project_id: 'other-project' }),
    phase: 'unavailable',
  },
  {
    name: 'an approval for another revision',
    record: () => readinessRecord({ approved_launch: { ...APPROVED_LAUNCH, revision: 'cat-version-2' } }),
    phase: 'setup-required',
  },
]

for (const { name, record, phase } of MALFORMED) {
  test(`@flag-on a malformed readiness record (${name}) never offers a launch`, async ({ page }) => {
    const calls = await installApi(page, iosLane({
      readiness: () => ({ status: 200, body: { readiness: record() } }),
    }))
    await openProjectRevision(page, calls)
    const device = await showIosSurface(page)

    await expect(device).toHaveAttribute('data-state', phase, { timeout: 20_000 })
    expect(calls.some((call) => call.path === '/api/ios-ship/readiness')).toBe(true)
    await expect(launchButton(device)).toHaveCount(0)
    await expect(ribbonLaunch(page)).toBeDisabled()
    await expect(device.locator('[data-rung="build"]')).toHaveAttribute('data-state', 'missing')
    expect(calls.filter((call) => call.path === '/api/ios-ship/launch')).toEqual([])
  })
}

test('@flag-on a lost execution clears its stored pointer and leaves the lane launchable', async ({ page }) => {
  const calls = await installApi(page, iosLane({
    execution: ({ path }) => path === '/api/ios-ship/executions/ios-exec-lost'
      ? { status: 404, body: { error: { message: 'execution not found' } } }
      : null,
  }))
  await openProjectRevision(page, calls)
  await seedPointer(page, 'ios-exec-lost')
  const device = await showIosSurface(page)

  // useIosShipController.js:154: a 404 on the stored execution drops the
  // pointer instead of following a ghost or raising an error.
  await expect.poll(() => readPointer(page), { timeout: 15_000 }).toBeNull()
  const reads = calls.filter((call) => call.path.startsWith('/api/ios-ship/executions/'))
  expect(reads.length).toBeGreaterThan(0)
  for (const read of reads) {
    expect(read.path).toBe('/api/ios-ship/executions/ios-exec-lost')
    expect(read.query).toEqual({ project_id: PROJECT_ID })
  }
  await expect(device).toHaveAttribute('data-state', 'ready', { timeout: 20_000 })
  await expect(device.locator('[data-rung="build"]')).toHaveAttribute('data-state', 'missing')
  await expect(device.locator('.device-stage')).toHaveCount(0)
  await expect(device.getByRole('alert')).toHaveCount(0)
  await expect(launchButton(device)).toBeEnabled()
  expect(calls.filter((call) => call.path === '/api/ios-ship/launch')).toEqual([])
})
