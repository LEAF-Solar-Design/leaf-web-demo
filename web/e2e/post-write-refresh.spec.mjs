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

test('a committed unreadable write locks mutations without a doomed head refresh', async ({ page }) => {
  test.setTimeout(150_000)
  const state = makeCatProofState()
  let submissions = 0
  let postCommitHeadReads = 0
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    if (url.pathname === '/api/run' && request.method() === 'POST' && body.tool === 'arrange-panels-as-cat') {
      submissions += 1
      await route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ job_id: 'unreadable-job-0001' }) })
      return
    }
    if (url.pathname === '/api/jobs/unreadable-job-0001' && request.method() === 'GET') {
      state.head = 2
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          job_id: 'unreadable-job-0001', status: 'complete', tool: 'arrange-panels-as-cat', elapsed_ms: 200,
          result: {
            ok: true, tool: 'arrange-panels-as-cat', version: '1.0.0', timing_ms: 200,
            cost: null, error: null, degraded_mode: false, overlay: null,
            result: {
              panels_preserved: state.count,
              new_version: { drawing_id: 'cat-panels', version: 2, parent: 1 },
              new_version_readable: false,
            },
          },
        }),
      })
      return
    }
    if (url.pathname === '/api/jobs/unreadable-job-0001/stream') {
      await route.fulfill({ status: 204, body: '' })
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
  await page.getByRole('button', { name: /arrange-panels-as-cat/i }).click()
  await page.getByRole('button', { name: 'Review & run' }).click()
  await page.getByRole('button', { name: 'Run arrange-panels-as-cat' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('Cat version ready', { timeout: 45_000 })
  await page.getByRole('tab', { name: 'Execution' }).click()
  await expect(page.getByTestId('catalog-run-result')).toContainText('Passed')

  const lock = page.getByTestId('unreadable-head-lock')
  await expect(lock).toHaveAttribute('data-head', '2')
  await expect(lock).toContainText('Version 2 was created')
  await expect(page.getByTestId('version-head')).toContainText('Version 2')
  await expect(page.getByRole('button', { name: 'Undo' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Redo' })).toBeDisabled()
  expect(submissions).toBe(1)
  expect(postCommitHeadReads).toBe(0)
})
