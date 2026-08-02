import { expect, test } from '@playwright/test'
import { AUTHORED_TOOL, catProofResponse, makeCatProofState } from './catProofFixture.mjs'

const SURFACES = [
  { name: 'unified surface', path: '/try?proof=1' },
  { name: 'console surface', path: '/app?dev=1&proof=1' },
]

for (const surface of SURFACES) {
  test(`${surface.name} resumes one exact authored revision after reload`, async ({ page }) => {
    test.setTimeout(60_000)
    const proofState = makeCatProofState()
    proofState.authorPublished = true
    const submissions = []
    let pollReads = 0
    let mayComplete = false

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
            poll_url: '/api/author/stage/async-change-0001',
            retry_after_ms: 250,
          }),
        })
        return
      }

      if (url.pathname === '/api/author/stage/async-change-0001' && request.method() === 'GET') {
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
            result: {
              receipt: {
                contract: 'leaf.customization.v1',
                change_set_id: 'async-change-0001',
                state: 'staged',
              },
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
      poll_url: '/api/author/stage/async-change-0001',
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
    await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.inflightAuthor.v1'))).toBeNull()

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
          poll_url: '/api/author/stage/recovered-change-0001',
          retry_after_ms: 250,
        }),
      })
      return
    }
    if (url.pathname === '/api/author/stage/recovered-change-0001') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers,
        body: JSON.stringify({
          status: 'staged',
          change_set_id: 'recovered-change-0001',
          result: {
            receipt: { contract: 'leaf.customization.v1', change_set_id: 'recovered-change-0001', state: 'staged' },
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
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.inflightAuthor.v1'))).toBeNull()
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
    poll_url: '/api/author/stage/failed-change-0001',
    retry_after_ms: 250,
    created_at: Date.now(),
  })
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const headers = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' }
    if (url.pathname === '/api/author/stage' && request.method() === 'POST') submissions += 1
    if (url.pathname === '/api/author/stage/failed-change-0001') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers,
        body: JSON.stringify({
          status: 'failed',
          change_set_id: 'failed-change-0001',
          error: { error_code: 'AUTHOR_FAILED', message: 'Generated source did not pass validation.', retryable: true },
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
