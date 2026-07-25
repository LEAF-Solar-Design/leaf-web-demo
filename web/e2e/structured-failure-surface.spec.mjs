import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

test('retryable failures require a fresh approval and nonretryable failures stay final', async ({ page }) => {
  test.setTimeout(180_000)
  const state = makeCatProofState()
  const submissions = []
  const jobs = new Map()

  await page.addInitScript(() => localStorage.setItem('leaf.org_id', 'cat-proof-org'))
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }

    if (url.pathname === '/api/run' && request.method() === 'POST' && body.tool === 'count-panels') {
      const attempt = submissions.length + 1
      const jobId = `failure-job-${attempt}`
      submissions.push({ body, headers: await request.allHeaders() })
      const error = attempt === 1
        ? { error_code: 'aps_timeout', message: 'APS did not finish before the deadline.', retryable: true }
        : attempt === 3
          ? { error_code: 'invalid_geometry', message: 'The drawing geometry is not valid for this tool.', retryable: false }
          : null
      jobs.set(jobId, error
        ? { job_id: jobId, status: 'failed', tool: 'count-panels', elapsed_ms: 240, error }
        : {
            job_id: jobId, status: 'complete', tool: 'count-panels', elapsed_ms: 120,
            result: {
              ok: true, tool: 'count-panels', version: '1.0.0', timing_ms: 120,
              cost: { engine_seconds: 0.12, usd_est: 0 }, error: null,
              degraded_mode: false, overlay: null, result: { count: state.count },
            },
          })
      await route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ job_id: jobId }) })
      return
    }

    const jobMatch = url.pathname.match(/^\/api\/jobs\/(failure-job-\d+)$/)
    if (jobMatch && request.method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(jobs.get(jobMatch[1])) })
      return
    }
    if (/^\/api\/jobs\/failure-job-\d+\/stream$/.test(url.pathname)) {
      await route.fulfill({ status: 204, body: '' })
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

  const stageCountPanels = async () => {
    await page.getByRole('tab', { name: /Catalog/ }).click()
    await page.getByRole('button', { name: /count-panels/i }).click()
    await page.getByRole('button', { name: 'Review & run' }).click()
    await expect(page.getByRole('button', { name: 'Run count-panels' })).toBeVisible()
  }

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await stageCountPanels()
  await page.getByRole('button', { name: 'Run count-panels' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('Request failed', { timeout: 15_000 })
  await page.getByRole('tab', { name: 'Execution' }).click()

  const result = page.getByTestId('catalog-run-result')
  await expect(result).toContainText('Failed')
  await expect(result).toContainText('APS did not finish before the deadline.')
  await expect(result).toContainText('aps_timeout')
  await expect(page.getByTestId('version-head')).toContainText('Version 1')
  await page.getByRole('button', { name: 'Details' }).click()
  const details = page.getByRole('dialog', { name: 'Run details' })
  await expect(details).toContainText('error code aps_timeout')
  await expect(details).toContainText('error message APS did not finish before the deadline.')
  await expect(details).toContainText('retryable yes')
  await details.getByRole('button', { name: 'Close details' }).click()

  await result.getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByRole('button', { name: 'Run count-panels' })).toBeVisible()
  expect(submissions).toHaveLength(1)
  await page.getByRole('button', { name: 'Run count-panels' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('Tool run complete', { timeout: 15_000 })
  await page.getByRole('tab', { name: 'Execution' }).click()
  await expect(result).toContainText('Passed')
  expect(submissions).toHaveLength(2)
  expect(submissions[0].headers['idempotency-key']).toBeTruthy()
  expect(submissions[1].headers['idempotency-key']).toBeTruthy()
  expect(submissions[1].headers['idempotency-key']).not.toBe(submissions[0].headers['idempotency-key'])

  await page.reload()
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await stageCountPanels()
  await page.getByRole('button', { name: 'Run count-panels' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('Request failed', { timeout: 15_000 })
  await page.getByRole('tab', { name: 'Execution' }).click()
  await expect(result).toContainText('invalid_geometry')
  await expect(result.getByRole('button', { name: 'Retry' })).toHaveCount(0)
  expect(submissions).toHaveLength(3)
})
