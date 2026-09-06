import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from '../catProofFixture.mjs'

// Use the same live API fixture and /try entry as version-restore.spec.mjs.
// Telemetry is not a drawing mutation; every other non-GET request counts.
async function mountUnifiedHistory(page, {
  restoredHeadReadable = true,
  holdRestore = false,
  denyRestore = false,
} = {}) {
  const state = makeCatProofState()
  const originalRows = catProofResponse({
    method: 'GET', path: '/api/drawings/cat-panels/versions', query: {},
  }, state).body.versions
  const versions = [...originalRows]
  const mutations = []
  const restoreRequests = []
  const headReads = []
  let latest = 2
  let restoredIntake = state.base
  let releaseRestore
  const restoreGate = new Promise((resolve) => { releaseRestore = resolve })

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const path = url.pathname
    const body = request.postData() ? request.postDataJSON() : {}
    if (method !== 'GET' && path !== '/api/telemetry') mutations.push(`${method} ${path}`)
    let result

    if (path === '/api/drawings/cat-panels/versions' && method === 'GET') {
      result = { status: 200, body: {
        drawing_id: 'cat-panels', head: state.head, latest,
        checkout: state.checkout, versions,
      } }
    } else if (/^\/api\/drawings\/cat-panels\/versions\/[12]\/restore$/.test(path) && method === 'POST') {
      restoreRequests.push(path)
      if (holdRestore) await restoreGate
      if (denyRestore) {
        result = { status: 403, body: { detail: 'Checkout capability required.' } }
      } else {
        const target = Number(path.split('/').at(-2))
        const parent = state.head
        state.head = ++latest
        restoredIntake = target === 2 ? state.cat : state.base
        versions.push({ v: state.head, parent, tool: 'restore', note: `restore of version ${target}` })
        result = { status: 200, body: {
          drawing_id: 'cat-panels', restored_from: target,
          head: state.head, latest,
          restored_head_readable: restoreRequests.length > 1 || restoredHeadReadable,
          new_version: { drawing_id: 'cat-panels', version: state.head, parent },
        } }
      }
    } else if (path === '/api/drawings/cat-panels/intake' && method === 'GET' && latest > 2) {
      const requested = url.searchParams.get('version')
      const version = requested && requested !== 'head' ? Number(requested) : state.head
      if (!requested || requested === 'head') headReads.push(version)
      result = { status: 200, body: {
        drawing_id: 'cat-panels', version, head: state.head, latest,
        intake: version === state.head ? restoredIntake : version === 2 ? state.cat : state.base,
      } }
    } else {
      result = catProofResponse({ method, path, body, query: Object.fromEntries(url.searchParams) }, state)
    }

    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try?proof=1')
  await expect(page.getByTestId('operator-phase')).toContainText(/ready/i, { timeout: 15_000 })
  await page.getByRole('tab', { name: /^Versions/ }).click()
  await expect(page.getByTestId('try-version-v2')).toBeVisible()
  return { mutations, restoreRequests, headReads, releaseRestore }
}

test('unified history cancels without a mutation and confirms once to show the new head', async ({ page }) => {
  const observed = await mountUnifiedHistory(page, { holdRestore: true })
  const history = page.getByRole('region', { name: 'Version history' })
  const source = history.getByTestId('try-version-v2')
  await expect(history.getByTestId('try-version-v1')).toContainText('head')
  await expect(history.getByTestId('try-version-v1').getByRole('button', { name: 'Restore', exact: true })).toHaveCount(0)

  // Seat the readable source first so a successful restore must also retire
  // the preview and seat the new head, rather than only update the row label.
  await source.getByRole('button').first().click()
  await expect(page.getByTestId('try-preview-write-lock')).toBeVisible()
  const before = observed.mutations.length
  await source.getByRole('button', { name: 'Restore', exact: true }).click()
  await expect(source.getByRole('button', { name: 'Restore v2', exact: true })).toBeVisible()
  expect(observed.mutations.slice(before)).toEqual([])
  await source.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(source.getByRole('button', { name: 'Restore', exact: true })).toBeVisible()
  expect(observed.mutations.slice(before)).toEqual([])

  await source.getByRole('button', { name: 'Restore', exact: true }).click()
  // Two clicks in one browser turn exercise the synchronous submit guard.
  await source.getByRole('button', { name: 'Restore v2', exact: true }).evaluate((button) => {
    button.click()
    button.click()
  })
  await expect.poll(() => observed.restoreRequests.length).toBe(1)
  await expect(source.getByRole('button', { name: /Restoring/ })).toBeDisabled()
  observed.releaseRestore()

  const newHead = history.getByTestId('try-version-v3')
  await expect(newHead).toContainText('head')
  await expect(newHead.getByRole('button', { name: 'Restore', exact: true })).toHaveCount(0)
  await expect(page.getByTestId('try-preview-write-lock')).toHaveCount(0)
  expect(observed.headReads).toContain(3)
  expect(observed.mutations.slice(before)).toEqual(['POST /api/drawings/cat-panels/versions/2/restore'])
  await page.getByRole('tab', { name: /^Execution/ }).click()
  await expect(page.locator('.tc-event').filter({ hasText: 'Version head' })).toContainText('v3')
  await expect(page.getByTestId('unreadable-head-lock')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Undo', exact: true })).toBeEnabled()
})

test('unified history still recovers an unreadable committed head', async ({ page }) => {
  const observed = await mountUnifiedHistory(page, { restoredHeadReadable: false })
  const source = page.getByTestId('try-version-v2')
  await source.getByRole('button', { name: 'Restore', exact: true }).click()
  await source.getByRole('button', { name: 'Restore v2', exact: true }).click()
  await expect(page.getByTestId('try-version-v3')).toContainText('head')
  await page.getByRole('tab', { name: /^Execution/ }).click()
  await expect(page.getByTestId('unreadable-head-lock')).toHaveAttribute('data-head', '3')
  await expect(page.getByRole('button', { name: 'Undo', exact: true })).toBeDisabled()
  expect(observed.headReads).toEqual([])

  await page.getByRole('tab', { name: /^Versions/ }).click()
  const recovery = page.getByTestId('try-version-v1')
  await recovery.getByRole('button', { name: 'Recover', exact: true }).click()
  await recovery.getByRole('button', { name: 'Recover from v1', exact: true }).click()
  await expect(page.getByTestId('try-version-v4')).toContainText('head')
  expect(observed.restoreRequests).toEqual([
    '/api/drawings/cat-panels/versions/2/restore',
    '/api/drawings/cat-panels/versions/1/restore',
  ])
  expect(observed.headReads).toContain(4)
  await page.getByRole('tab', { name: /^Execution/ }).click()
  await expect(page.getByTestId('unreadable-head-lock')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Undo', exact: true })).toBeEnabled()
})

test('guest sandbox restore never reaches the server', async ({ page }) => {
  const restoreRequests = []
  page.on('request', (request) => {
    if (/^\/api\/drawings\/[^/]+\/versions\/[^/]+\/restore\/?$/.test(new URL(request.url()).pathname)) {
      restoreRequests.push(request.url())
    }
  })
  await page.context().clearCookies()
  await page.addInitScript(() => localStorage.removeItem('leaf.jwt'))
  await page.goto('/try?demo=1')
  await expect(page.getByTestId('operator-phase')).toContainText(/ready/i, { timeout: 15_000 })
  await expect(page.locator('.tc-caption')).toContainText('Interactive local demo')

  // Seed a readable non-head version in the real local chain. The restore
  // itself must go through the visitor's confirmation controls below.
  await page.evaluate(async () => {
    const versions = await import('/src/mock/mockVersions.js')
    versions.applyDelete()
    versions.undo()
  })
  await page.getByRole('tab', { name: /^Versions/ }).click()
  const history = page.getByRole('region', { name: 'Version history' })
  await expect(history.getByTestId('try-version-v1')).toContainText('head')
  const source = history.getByTestId('try-version-v2')
  await source.getByRole('button').first().click()
  await expect(page.getByTestId('try-preview-write-lock')).toBeVisible()
  await source.getByRole('button', { name: 'Restore', exact: true }).click()
  await source.getByRole('button', { name: 'Restore v2', exact: true }).click()
  await expect(history.getByTestId('try-version-v3')).toContainText('head')
  await expect(page.getByTestId('try-preview-write-lock')).toHaveCount(0)
  const restored = await page.evaluate(async () => {
    const versions = await import('/src/mock/mockVersions.js')
    const history = versions.list()
    return {
      head: history.head,
      latest: history.latest,
      versions: history.versions.map(({ v, parent, tool }) => ({ v, parent, tool })),
      matchesSource: JSON.stringify(versions.headIntake()) === JSON.stringify(versions.intakeAt(2)),
    }
  })
  expect(restored).toEqual({
    head: 3,
    latest: 3,
    versions: [
      { v: 1, parent: null, tool: 'base' },
      { v: 2, parent: 1, tool: 'delete-marked-panel' },
      { v: 3, parent: 1, tool: 'restore' },
    ],
    matchesSource: true,
  })
  expect(restoreRequests).toEqual([])
})

test('unified restore preserves the restore service checkout denial', async ({ page }) => {
  const observed = await mountUnifiedHistory(page, { denyRestore: true })
  const history = page.getByRole('region', { name: 'Version history' })
  const source = history.getByTestId('try-version-v2')
  await source.getByRole('button', { name: 'Restore', exact: true }).click()
  await source.getByRole('button', { name: 'Restore v2', exact: true }).click()
  await expect(history.getByRole('alert')).toHaveText('POST /api/drawings/cat-panels/versions/2/restore -> 403: Checkout capability required.')
  await expect(history.getByTestId('try-version-v1')).toContainText('head')
  await expect(history.getByTestId('try-version-v3')).toHaveCount(0)
  expect(observed.restoreRequests).toHaveLength(1)
  expect(observed.headReads).toEqual([])
})
