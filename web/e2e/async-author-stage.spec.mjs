import { expect, test } from '@playwright/test'
import { AUTHORED_TOOL, catProofResponse, makeCatProofState } from './catProofFixture.mjs'

const SURFACES = [
  { name: 'unified surface', path: '/try?proof=1' },
  { name: 'console surface', path: '/app?dev=1&proof=1' },
]

for (const surface of SURFACES) {
  test(`${surface.name} resumes one exact authored revision after reload`, async ({ page }) => {
    test.setTimeout(90_000)
    const proofState = makeCatProofState()
    proofState.authorPublished = true
    const submissions = []
    const publicationBodies = []
    let pollReads = 0
    let mayComplete = false
    let releasePublication
    const publicationBarrier = new Promise((resolve) => { releasePublication = resolve })

    await page.addInitScript(() => localStorage.setItem('leaf.org_id', 'cat-proof-org'))
    await page.route('http://leaf-proof.invalid/api/**', async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      let body = {}
      if (request.postData()) {
        try { body = request.postDataJSON() } catch { body = {} }
      }
      const headers = {
        'access-control-allow-origin': '*',
        'access-control-allow-headers': '*',
      }

      if (url.pathname === '/api/author/stage' && request.method() === 'POST') {
        submissions.push(body)
        await route.fulfill({
          status: 202,
          contentType: 'application/json',
          headers,
          body: JSON.stringify({
            contract: 'leaf.customization.v1',
            change_set_id: 'async-change-0001',
            status: 'accepted',
            poll_url: '/api/author/stages/async-change-0001',
            retry_after_ms: 250,
          }),
        })
        return
      }
      if (url.pathname === '/api/author/publication-requests' && request.method() === 'POST') {
        publicationBodies.push(body)
        if (publicationBodies.length === 1) await publicationBarrier
      }

      if (url.pathname === '/api/author/stages/async-change-0001' && request.method() === 'GET') {
        pollReads += 1
        if (!mayComplete) {
          await route.fulfill({
            status: 200,
            contentType: 'application/json',
            headers,
            body: JSON.stringify({
              contract: 'leaf.customization.v1',
              change_set_id: 'async-change-0001',
              status: 'running',
              progress: 'authoring tool source',
              retry_after_ms: 250,
            }),
          })
          return
        }
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          headers,
          body: JSON.stringify({
            contract: 'leaf.customization.v1',
            change_set_id: 'async-change-0001',
            status: 'staged',
            receipt: {
              contract: 'leaf.customization.v1',
              change_set_id: 'async-change-0001',
              state: 'staged',
            },
            result: {
              tool: AUTHORED_TOOL,
              preview: 'Repaired the exact existing custom tool.',
              code: 'def run(ctx):\n    return repaired(ctx)\n',
              source: 'harness',
              static_scan: [],
              validation: { status: 'passed' },
              diff_summary: 'Updates the bound tool without adding another catalog entry.',
            },
          }),
        })
        return
      }

      const result = catProofResponse({
        method: request.method(),
        path: url.pathname,
        body,
        query: Object.fromEntries(url.searchParams),
      }, proofState)
      await route.fulfill({
        status: result.status,
        contentType: result.body == null ? undefined : 'application/json',
        body: result.body == null ? '' : JSON.stringify(result.body),
        headers,
      })
    })

    await page.goto(surface.path)
    const catalogTab = page.getByRole('tab', { name: /Catalog/ })
    if (await catalogTab.count()) await catalogTab.click()
    const customFamily = page.getByRole('button', { name: /Custom authored tools/ })
    await expect(customFamily).toBeVisible({ timeout: 15_000 })
    if (await customFamily.getAttribute('aria-expanded') === 'false') await customFamily.click()
    const toolCard = page.locator('.tool-card').filter({ hasText: AUTHORED_TOOL.name })
    await toolCard.locator('.tool-head').click()
    await toolCard.getByRole('button', { name: 'Revise' }).click()

    await expect(page.getByLabel('Tool to revise')).toHaveValue(AUTHORED_TOOL.name)
    await page.getByLabel('What should the tool do?').fill('repair its generated geometry without creating a duplicate')
    await page.getByRole('button', { name: 'Generate revision' }).click()
    await expect(page.getByText(/authoring tool source/i)).toBeVisible({ timeout: 10_000 })
    await expect.poll(() => submissions.length).toBe(1)
    const pointerBeforeReload = await page.evaluate(() => JSON.parse(localStorage.getItem('leaf.inflightAuthor.v1')))
    expect(pointerBeforeReload).toMatchObject({
      idempotency_key: submissions[0].idempotency_key,
      description: 'repair its generated geometry without creating a duplicate',
      target_tool_name: AUTHORED_TOOL.name,
      change_set_id: 'async-change-0001',
      poll_url: '/api/author/stages/async-change-0001',
    })

    const readsBeforeReload = pollReads
    await page.reload()
    await expect(page.getByLabel('Tool to revise')).toHaveValue(AUTHORED_TOOL.name, { timeout: 15_000 })
    await expect(page.getByText(/Reconnecting to authoring|Authoring with the agent/)).toBeVisible()
    await expect.poll(() => pollReads).toBeGreaterThan(readsBeforeReload)
    expect(submissions).toHaveLength(1)

    mayComplete = true
    await expect(page.locator('.authored')).toContainText('Repaired the exact existing custom tool.', { timeout: 10_000 })
    expect(submissions).toHaveLength(1)
    expect(submissions[0]).toMatchObject({
      target_tool_name: AUTHORED_TOOL.name,
      description: 'repair its generated geometry without creating a duplicate',
      mode: 'build',
    })
    expect(submissions[0].idempotency_key).toBeTruthy()
    await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.inflightAuthor.v1'))).not.toBeNull()

    await page.reload()
    await expect(page.locator('.authored')).toContainText('Repaired the exact existing custom tool.', { timeout: 30_000 })
    const terminalPointer = await page.evaluate(() => JSON.parse(localStorage.getItem('leaf.inflightAuthor.v1')))
    expect(terminalPointer).toMatchObject({
      idempotency_key: submissions[0].idempotency_key,
      change_set_id: 'async-change-0001',
      terminal_staged: true,
    })
    const generateRevision = page.getByRole('button', { name: 'Generate revision' })
    const cancelRevision = page.getByRole('button', { name: 'Cancel revision' })
    await expect(generateRevision).toBeDisabled()
    await expect(cancelRevision).toBeDisabled()
    await generateRevision.evaluate((button) => button.click())
    await page.waitForTimeout(100)
    expect(submissions).toHaveLength(1)
    expect(await page.evaluate(() => JSON.parse(localStorage.getItem('leaf.inflightAuthor.v1')).idempotency_key)).toBe(terminalPointer.idempotency_key)

    const publicationRequest = page.getByRole('button', { name: 'Request publication' }).click()
    await expect.poll(() => publicationBodies.length).toBe(1)
    await expect(generateRevision).toBeDisabled()
    await expect(cancelRevision).toBeDisabled()
    await generateRevision.evaluate((button) => button.click())
    expect(submissions).toHaveLength(1)
    expect(await page.evaluate(() => JSON.parse(localStorage.getItem('leaf.inflightAuthor.v1')).idempotency_key)).toBe(terminalPointer.idempotency_key)
    releasePublication()
    await publicationRequest
    await expect(page.getByText(/Awaiting independent approval/)).toBeVisible()
    expect(publicationBodies[0]).toEqual({ change_set_id: 'async-change-0001' })
    proofState.independentApproved = true
    await page.getByRole('button', { name: 'Check approval & resume' }).click()
    await expect(page.getByRole('button', { name: 'Run it now' })).toBeVisible()
    await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.inflightAuthor.v1'))).toBeNull()
    await expect(cancelRevision).toBeEnabled()
    await cancelRevision.click()
    await expect(page.getByLabel('What should the tool do?')).toBeEnabled()
    await page.getByLabel('What should the tool do?').fill('author another tool after publication')
    await expect(page.getByRole('button', { name: 'Generate tool' })).toBeEnabled()

    if (await catalogTab.count()) await catalogTab.click()
    const endFamily = page.getByRole('button', { name: /Custom authored tools/ })
    if (await endFamily.getAttribute('aria-expanded') === 'false') await endFamily.click()
    await expect(page.locator('.tool-card').filter({ hasText: AUTHORED_TOOL.name })).toHaveCount(1)
  })
}

test('a lost acceptance response replays the exact idempotent revision request', async ({ page }) => {
  const proofState = makeCatProofState()
  proofState.authorPublished = true
  const submissions = []
  const saved = {
    idempotency_key: 'lost-response-key-0001',
    description: 'repair the existing tool after a lost response',
    target_tool_name: AUTHORED_TOOL.name,
    change_set_id: null,
    poll_url: null,
    retry_after_ms: null,
    created_at: Date.now(),
    expires_at: Date.now() + 60_000,
    account_scope: 'tenant:cat-litmus-tenant|org:cat-proof-org|subject:anonymous',
  }
  await page.addInitScript((pointer) => {
    localStorage.setItem('leaf.org_id', 'cat-proof-org')
    localStorage.setItem('leaf.inflightAuthor.v1', JSON.stringify(pointer))
  }, saved)
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    const headers = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' }
    if (url.pathname === '/api/author/stage' && request.method() === 'POST') {
      submissions.push(body)
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        headers,
        body: JSON.stringify({
          contract: 'leaf.customization.v1',
          change_set_id: 'recovered-change-0001',
          status: 'accepted',
          poll_url: '/api/author/stages/recovered-change-0001',
          retry_after_ms: 250,
        }),
      })
      return
    }
    if (url.pathname === '/api/author/stages/recovered-change-0001') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers,
        body: JSON.stringify({
          status: 'staged',
          change_set_id: 'recovered-change-0001',
          receipt: { contract: 'leaf.customization.v1', change_set_id: 'recovered-change-0001', state: 'staged' },
          result: {
            tool: AUTHORED_TOOL,
            preview: 'Recovered the accepted revision without creating another.',
            code: 'def run(ctx):\n    return recovered(ctx)\n',
            source: 'harness',
            static_scan: [],
          },
        }),
      })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname, body }, proofState)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers,
    })
  })

  await page.goto('/try?proof=1')
  await expect(page.locator('.authored')).toContainText('Recovered the accepted revision', { timeout: 15_000 })
  expect(submissions).toHaveLength(1)
  expect(submissions[0]).toMatchObject({
    idempotency_key: saved.idempotency_key,
    description: saved.description,
    target_tool_name: saved.target_tool_name,
    mode: 'build',
  })
  await expect(page.getByLabel('Tool to revise')).toHaveValue(AUTHORED_TOOL.name)
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.inflightAuthor.v1'))).not.toBeNull()
})

test('a bounded failed poll clears recovery state without staging a tool', async ({ page }) => {
  const proofState = makeCatProofState()
  let submissions = 0
  await page.addInitScript((pointer) => {
    localStorage.setItem('leaf.org_id', 'cat-proof-org')
    localStorage.setItem('leaf.inflightAuthor.v1', JSON.stringify(pointer))
  }, {
    idempotency_key: 'failed-author-key-0001',
    description: 'repair the existing tool',
    target_tool_name: AUTHORED_TOOL.name,
    change_set_id: 'failed-change-0001',
    poll_url: '/api/author/stages/failed-change-0001',
    retry_after_ms: 250,
    created_at: Date.now(),
    expires_at: Date.now() + 60_000,
    account_scope: 'tenant:cat-litmus-tenant|org:cat-proof-org|subject:anonymous',
  })
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const headers = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' }
    if (url.pathname === '/api/author/stage' && request.method() === 'POST') submissions += 1
    if (url.pathname === '/api/author/stages/failed-change-0001') {
      // Real producer shape (customization_service.stage_status_change):
      // reason_code + retryable always, message when a reason was captured.
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers,
        body: JSON.stringify({
          contract: 'leaf.customization-stage-job.v1',
          status: 'failed',
          phase: 'failed',
          change_set_id: 'failed-change-0001',
          error: { reason_code: 'customization_author_job_failed', retryable: true, message: 'Generated source did not pass validation.' },
          retry_after_ms: 250,
        }),
      })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname }, proofState)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers,
    })
  })

  await page.goto('/try?proof=1')
  await expect(page.getByText(/Generated source did not pass validation/)).toBeVisible({ timeout: 15_000 })
  await expect(page.locator('.authored')).toHaveCount(0)
  expect(submissions).toBe(0)
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.inflightAuthor.v1'))).toBeNull()
})

test('a harness job failure surfaces the server reason instead of eternal pending', async ({ page }) => {
  const proofState = makeCatProofState()
  let polls = 0
  await page.addInitScript(() => localStorage.setItem('leaf.org_id', 'cat-proof-org'))
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const headers = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' }
    if (url.pathname === '/api/author/stage' && request.method() === 'POST') {
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        headers,
        body: JSON.stringify({
          contract: 'leaf.customization-stage-job.v1',
          change_set_id: 'dead-authoring-0001',
          status: 'queued',
          poll_url: '/api/author/stages/dead-authoring-0001',
          retry_after_ms: 250,
        }),
      })
      return
    }
    if (url.pathname === '/api/author/stages/dead-authoring-0001' && request.method() === 'GET') {
      polls += 1
      // First poll: the job runs. Then the harness authoring job dies and the
      // worker records the terminal failure with the harness's reason string.
      const body = polls > 1
        ? {
            contract: 'leaf.customization-stage-job.v1',
            change_set_id: 'dead-authoring-0001',
            status: 'failed',
            phase: 'failed',
            error: {
              reason_code: 'customization_author_job_failed',
              retryable: true,
              message: 'Agent SDK auth failure: oauth_org_not_allowed',
            },
            retry_after_ms: 250,
          }
        : {
            contract: 'leaf.customization-stage-job.v1',
            change_set_id: 'dead-authoring-0001',
            status: 'running',
            phase: 'authoring',
            retry_after_ms: 250,
          }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers,
        body: JSON.stringify(body),
      })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname }, proofState)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers,
    })
  })

  await page.goto('/try?proof=1')
  await page.getByRole('tab', { name: 'Author' }).click()
  await page.getByLabel('What should the tool do?').fill('count panels near the ridge line')
  await page.getByRole('button', { name: 'Generate tool' }).click()
  // The terminal failure reaches the operator as a red failure carrying the
  // server's reason string — not silence, not an eternal authoring spinner,
  // and not the calm "temporarily unavailable" gate.
  await expect(page.getByText(/Agent SDK auth failure: oauth_org_not_allowed/)).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText(/author the tool/)).toBeVisible()
  await expect(page.getByText(/Authoring service is temporarily unavailable/)).toHaveCount(0)
  await expect(page.getByText(/Authoring with the agent/)).toHaveCount(0)
  await expect(page.locator('.authored')).toHaveCount(0)
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.inflightAuthor.v1'))).toBeNull()
})

test('a cross-origin poll URL is rejected without forwarding account authority', async ({ page }) => {
  const proofState = makeCatProofState()
  let foreignRequests = 0
  await page.addInitScript(() => localStorage.setItem('leaf.org_id', 'cat-proof-org'))
  await page.route('https://foreign.invalid/**', async (route) => {
    foreignRequests += 1
    await route.abort()
  })
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const headers = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' }
    if (url.pathname === '/api/author/stage' && request.method() === 'POST') {
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        headers,
        body: JSON.stringify({
          contract: 'leaf.customization.v1',
          change_set_id: 'foreign-change-0001',
          status: 'accepted',
          poll_url: 'https://foreign.invalid/steal-authority',
          retry_after_ms: 250,
        }),
      })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname }, proofState)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers,
    })
  })

  await page.goto('/try?proof=1')
  await page.getByRole('tab', { name: 'Author' }).click()
  await page.getByLabel('What should the tool do?').fill('make a safe read-only inspection tool')
  await page.getByRole('button', { name: 'Generate tool' }).click()
  await expect(page.getByText(/invalid status address/)).toBeVisible({ timeout: 10_000 })
  expect(foreignRequests).toBe(0)
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.inflightAuthor.v1'))).toBeNull()
})

for (const invalid of [
  { name: 'another account', account_scope: 'tenant:cat-litmus-tenant|org:other-org|subject:anonymous', expires_at: Date.now() + 60_000 },
  { name: 'an expired request', account_scope: 'tenant:cat-litmus-tenant|org:cat-proof-org|subject:anonymous', expires_at: Date.now() - 1 },
]) {
  test(`clears an inflight author pointer from ${invalid.name} without replay`, async ({ page }) => {
    const proofState = makeCatProofState()
    let authorRequests = 0
    await page.addInitScript((pointer) => {
      localStorage.setItem('leaf.org_id', 'cat-proof-org')
      localStorage.setItem('leaf.inflightAuthor.v1', JSON.stringify(pointer))
    }, {
      idempotency_key: 'invalid-scope-key',
      description: 'must not replay',
      target_tool_name: AUTHORED_TOOL.name,
      change_set_id: null,
      poll_url: null,
      retry_after_ms: null,
      created_at: Date.now(),
      expires_at: invalid.expires_at,
      account_scope: invalid.account_scope,
    })
    await page.route('http://leaf-proof.invalid/api/**', async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      if (url.pathname === '/api/author/stage') authorRequests += 1
      const result = catProofResponse({ method: request.method(), path: url.pathname }, proofState)
      await route.fulfill({
        status: result.status,
        contentType: result.body == null ? undefined : 'application/json',
        body: result.body == null ? '' : JSON.stringify(result.body),
        headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
      })
    })
    await page.goto('/try?proof=1')
    await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
    expect(authorRequests).toBe(0)
    await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.inflightAuthor.v1'))).toBeNull()
  })
}

for (const invalid of [
  { name: 'wrong same-origin path', poll_url: '/api/jobs/guarded-change', status_id: 'guarded-change', receipt_id: null },
  { name: 'mismatched status id', poll_url: '/api/author/stages/guarded-change', status_id: 'other-change', receipt_id: null },
  { name: 'mismatched receipt id', poll_url: '/api/author/stages/guarded-change', status_id: 'guarded-change', receipt_id: 'other-change' },
]) {
  test(`rejects ${invalid.name} before publication`, async ({ page }) => {
    const proofState = makeCatProofState()
    let wrongPathReads = 0
    await page.addInitScript((pointer) => {
      localStorage.setItem('leaf.org_id', 'cat-proof-org')
      localStorage.setItem('leaf.inflightAuthor.v1', JSON.stringify(pointer))
    }, {
      idempotency_key: 'guarded-key',
      description: 'guard exact change set identity',
      target_tool_name: AUTHORED_TOOL.name,
      change_set_id: 'guarded-change',
      poll_url: invalid.poll_url,
      retry_after_ms: 250,
      created_at: Date.now(),
      expires_at: Date.now() + 60_000,
      account_scope: 'tenant:cat-litmus-tenant|org:cat-proof-org|subject:anonymous',
    })
    await page.route('http://leaf-proof.invalid/api/**', async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      const headers = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' }
      if (url.pathname === '/api/jobs/guarded-change') wrongPathReads += 1
      if (url.pathname === '/api/author/stages/guarded-change') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          headers,
          body: JSON.stringify({
            status: 'staged',
            change_set_id: invalid.status_id,
            receipt: {
              contract: 'leaf.customization.v1',
              change_set_id: invalid.receipt_id || 'guarded-change',
              state: 'staged',
            },
            result: { tool: AUTHORED_TOOL, code: 'def run(ctx): pass', source: 'harness', static_scan: [] },
          }),
        })
        return
      }
      const result = catProofResponse({ method: request.method(), path: url.pathname }, proofState)
      await route.fulfill({
        status: result.status,
        contentType: result.body == null ? undefined : 'application/json',
        body: result.body == null ? '' : JSON.stringify(result.body),
        headers,
      })
    })
    await page.goto('/try?proof=1')
    await expect(page.getByText(/invalid status address|did not match the accepted change set/)).toBeVisible({ timeout: 15_000 })
    expect(wrongPathReads).toBe(0)
    await expect(page.locator('.authored')).toHaveCount(0)
    await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.inflightAuthor.v1'))).toBeNull()
  })
}
