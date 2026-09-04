import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

test('a completed write keeps its receipt and recovers a failed viewer refresh without rerunning', async ({ page }) => {
  test.setTimeout(150_000)
  const state = makeCatProofState()
  let failNextHeadRefresh = false
  let failedRefreshes = 0
  let submissions = 0
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    if (url.pathname === '/api/run' && request.method() === 'POST' && body.tool === 'arrange-panels-as-cat') {
      submissions += 1
      failNextHeadRefresh = true
      await route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ job_id: 'refresh-job-0001' }) })
      return
    }
    if (url.pathname === '/api/jobs/refresh-job-0001' && request.method() === 'GET') {
      state.head = 2
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          job_id: 'refresh-job-0001', status: 'complete', tool: 'arrange-panels-as-cat', elapsed_ms: 200,
          result: {
            ok: true, tool: 'arrange-panels-as-cat', version: '1.0.0', timing_ms: 200,
            cost: null, error: null, degraded_mode: false, overlay: null,
            result: { panels_preserved: state.count, new_version: { drawing_id: 'cat-panels', version: 2, parent: 1 } },
          },
        }),
      })
      return
    }
    if (url.pathname === '/api/jobs/refresh-job-0001/stream') {
      await route.fulfill({ status: 204, body: '' })
      return
    }
    if (
      failNextHeadRefresh && state.head === 2 &&
      url.pathname === '/api/drawings/cat-panels/intake' &&
      url.searchParams.get('version') === 'head'
    ) {
      failNextHeadRefresh = false
      failedRefreshes += 1
      await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { message: 'viewer refresh unavailable' } }) })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, state)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByRole('tab', { name: /Catalog/ }).click()
  await page.getByRole('button', { name: /arrange-panels-as-cat/i }).click()
  await page.getByRole('button', { name: 'Review & run' }).click()
  await page.getByRole('button', { name: 'Run arrange-panels-as-cat' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('Cat version ready', { timeout: 45_000 })
  await page.getByRole('tab', { name: 'Execution' }).click()

  const refreshFailure = page.locator('.tc-refresh-failure')
  await expect(refreshFailure).toContainText('previous version is still shown')
  await expect(page.getByTestId('version-head')).toContainText('Version 1')
  await expect(page.getByTestId('catalog-run-result')).toContainText('Passed')
  expect(submissions).toBe(1)
  expect(failedRefreshes).toBe(1)

  await refreshFailure.getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByTestId('version-head')).toContainText('Version 2')
  await expect(refreshFailure).toHaveCount(0)
  expect(submissions).toBe(1)
})

async function mountUnreadableTry(page, { restoreStatus = null, holdRecoveryRead = false } = {}) {
  const state = makeCatProofState()
  let submissions = 0
  let postCommitHeadReads = 0
  let restoreAttempts = 0
  let releaseRecoveryRead = () => {}
  let markRecoveryReadStarted = () => {}
  const recoveryReadStarted = new Promise((resolve) => { markRecoveryReadStarted = resolve })
  const recoveryReadGate = new Promise((resolve) => { releaseRecoveryRead = resolve })
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    if (url.pathname === '/api/run' && request.method() === 'POST' && body.tool === 'arrange-panels-as-cat') {
      submissions += 1
      await route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ job_id: `unreadable-job-000${submissions}` }) })
      return
    }
    const unreadableJob = url.pathname.match(/^\/api\/jobs\/unreadable-job-000([12])$/)
    if (unreadableJob && request.method() === 'GET') {
      const sequence = Number(unreadableJob[1])
      const parent = sequence === 1 ? 1 : 3
      const version = parent + 1
      state.head = version
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          job_id: `unreadable-job-000${sequence}`, status: 'complete', tool: 'arrange-panels-as-cat', elapsed_ms: 200,
          result: {
            ok: true, tool: 'arrange-panels-as-cat', version: '1.0.0', timing_ms: 200,
            cost: null, error: null, degraded_mode: false, overlay: null,
            result: {
              panels_preserved: state.count,
              new_version: { drawing_id: 'cat-panels', version, parent },
              new_version_readable: false,
            },
          },
        }),
      })
      return
    }
    if (/^\/api\/jobs\/unreadable-job-000[12]\/stream$/.test(url.pathname)) {
      await route.fulfill({ status: 204, body: '' })
      return
    }
    if (url.pathname === '/api/drawings/cat-panels/versions/1/restore' && request.method() === 'POST') {
      restoreAttempts += 1
      if (restoreStatus) {
        await route.fulfill({
          status: restoreStatus,
          contentType: 'application/json',
          body: JSON.stringify({ error: { message: restoreStatus === 403 ? 'checkout required' : 'source intake unreadable' } }),
        })
        return
      }
      const parent = state.head
      state.head = 3
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          drawing_id: 'cat-panels', restored_from: 1,
          new_version: { drawing_id: 'cat-panels', version: 3, parent },
          head: 3, latest: 3, restored_head_readable: true,
        }),
      })
      return
    }
    if (url.pathname === '/api/drawings/cat-panels/versions' && request.method() === 'GET') {
      const versions = [
        { v: 1, parent: null, tool: 'base', note: 'Original drawing' },
        { v: 2, parent: 1, tool: 'arrange-panels-as-cat', note: 'Unreadable committed head' },
      ]
      if (state.head >= 3) versions.push({ v: 3, parent: 2, tool: 'restore', note: 'Recovered from version 1' })
      if (state.head >= 4) versions.push({ v: 4, parent: 3, tool: 'arrange-panels-as-cat', note: 'Second unreadable head' })
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        // Deliberately stale manifest head. ToolCast must use controller head.
        body: JSON.stringify({ drawing_id: 'cat-panels', head: 1, latest: state.head, checkout: null, versions }),
      })
      return
    }
    if (state.head === 3 && url.pathname === '/api/drawings/cat-panels/intake' && url.searchParams.get('version') === 'head') {
      if (holdRecoveryRead) {
        markRecoveryReadStarted()
        await recoveryReadGate
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ drawing_id: 'cat-panels', intake: state.base, version: 3, head: 3, latest: 3 }),
      })
      return
    }
    if (state.head === 4 && url.pathname === '/api/drawings/cat-panels/intake' && url.searchParams.get('version') === 'head') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ drawing_id: 'cat-panels', intake: state.cat, version: 4, head: 4, latest: 4 }),
      })
      return
    }
    if (state.head === 2 && url.pathname === '/api/drawings/cat-panels/intake') {
      postCommitHeadReads += 1
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, state)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByRole('tab', { name: /Catalog/ }).click()
  await page.getByRole('button', { name: /^arrange-panels-as-cat User/i }).click()
  await page.getByRole('button', { name: 'Review & run' }).click()
  await page.getByRole('button', { name: 'Run arrange-panels-as-cat' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('Cat version ready', { timeout: 45_000 })
  await page.getByRole('tab', { name: 'Execution' }).click()
  await expect(page.getByTestId('catalog-run-result')).toContainText('Passed')

  return {
    postCommitHeadReads: () => postCommitHeadReads,
    releaseRecoveryRead,
    recoveryReadStarted,
    restoreAttempts: () => restoreAttempts,
    submissions: () => submissions,
  }
}

test('an unreadable /try head locks every write path, recovers history, and handles another unreadable commit', async ({ page }) => {
  test.setTimeout(150_000)
  const observed = await mountUnreadableTry(page, { holdRecoveryRead: true })

  const lock = page.getByTestId('unreadable-head-lock')
  await expect(lock).toHaveAttribute('data-head', '2')
  await expect(lock).toContainText('Version 2 was created')
  await expect(page.getByTestId('version-head')).toContainText('Version 2')
  await expect(page.getByRole('button', { name: 'Undo' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Redo' })).toBeDisabled()
  expect(observed.submissions()).toBe(1)
  expect(observed.postCommitHeadReads()).toBe(0)

  await page.getByRole('tab', { name: /Catalog/ }).click()
  await expect(page.getByRole('button', { name: 'Review & run' })).toBeDisabled()

  const command = page.getByLabel('Command bar', { exact: true })
  await command.fill('/arrange-panels-as-cat')
  await page.locator('.tc-run').click()
  await expect(page.getByRole('button', { name: 'Run arrange-panels-as-cat' })).toHaveCount(0)
  await expect(page.locator('.tc-operator-error')).toContainText('Editing is locked')

  await command.fill('Please plan a safe cat layout')
  await page.locator('.tc-run').click()
  await page.getByRole('tab', { name: 'Operator' }).click()
  await expect(page.getByRole('button', { name: 'Editing locked' })).toBeDisabled()
  expect(observed.submissions()).toBe(1)

  await page.getByRole('tab', { name: /Versions/ }).click()
  const current = page.getByTestId('try-version-v2')
  await expect(current).toContainText('head')
  await expect(current.getByRole('button', { name: 'Recover', exact: true })).toHaveCount(0)
  const historical = page.getByTestId('try-version-v1')
  await historical.getByRole('button', { name: 'Recover', exact: true }).click()
  await historical.getByRole('button', { name: 'Recover from v1' }).click()
  await observed.recoveryReadStarted
  await expect(historical.getByRole('button', { name: 'Recover', exact: true })).toBeDisabled()
  expect(observed.restoreAttempts()).toBe(1)
  expect(observed.submissions()).toBe(1)

  observed.releaseRecoveryRead()
  await expect(lock).toHaveCount(0)
  await expect(page.getByTestId('version-head')).toContainText('Version 3')

  await page.getByRole('tab', { name: /Catalog/ }).click()
  await page.getByRole('button', { name: /^arrange-panels-as-cat User/i }).click()
  await expect(page.getByRole('button', { name: 'Review & run' })).toBeEnabled()
  await page.getByRole('button', { name: 'Review & run' }).click()
  await page.getByRole('button', { name: 'Run arrange-panels-as-cat' }).click()
  await page.getByRole('tab', { name: 'Execution' }).click()
  await expect(page.getByTestId('unreadable-head-lock')).toHaveAttribute('data-head', '4', { timeout: 45_000 })
  expect(observed.submissions()).toBe(2)

  await page.getByTestId('unreadable-head-lock').getByRole('button', { name: 'Retry loading' }).click()
  await expect(page.getByTestId('unreadable-head-lock')).toHaveCount(0)
  await expect(page.getByTestId('version-head')).toContainText('Version 4')
})

for (const restoreStatus of [403, 422]) {
  test(`a ${restoreStatus} recovery failure keeps the /try write lock and does not rerun the tool`, async ({ page }) => {
    test.setTimeout(150_000)
    const observed = await mountUnreadableTry(page, { restoreStatus })
    await page.getByRole('tab', { name: /Versions/ }).click()
    const historical = page.getByTestId('try-version-v1')
    await historical.getByRole('button', { name: 'Recover', exact: true }).click()
    await historical.getByRole('button', { name: 'Recover from v1' }).click()

    await expect(page.getByRole('alert')).toContainText(new RegExp(`${restoreStatus}|${restoreStatus === 403 ? 'checkout' : 'source intake'}`, 'i'))
    await expect(page.getByTestId('version-head')).toContainText('Version 2')
    await expect(historical.getByRole('button', { name: 'Recover from v1' })).toBeEnabled()
    await page.getByRole('tab', { name: /Catalog/ }).click()
    await expect(page.getByRole('button', { name: 'Review & run' })).toBeDisabled()
    expect(observed.restoreAttempts()).toBe(1)
    expect(observed.submissions()).toBe(1)
  })
}
