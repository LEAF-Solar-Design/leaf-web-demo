import { test, expect } from './_harness.mjs'
import { readFile } from 'node:fs/promises'
import { LOCAL_IDENTITY } from '../walk/fixtures.mjs'

// Match the walk fixture's browser tenant as well as its setup requests.
test.use({ extraHTTPHeaders: { 'X-Tenant-Id': LOCAL_IDENTITY.tenant } })

// TEMPORARY DIAGNOSTIC (root rulings 3d57336c, 74b9d521; removed before landing). On an unexpected outcome only, append
// the sanitized snapshot as an extra per-test error (the original failure stays first and keeps its status), because the
// CI failed-log producer forwards only per-test error text. Payload bounded to 3000 characters.
let paletteDiagnostic = null
test.afterEach(async ({}, testInfo) => {
  if (paletteDiagnostic && testInfo.status !== testInfo.expectedStatus) {
    throw new Error('PALETTE_DIAG ' + JSON.stringify(paletteDiagnostic).slice(0, 3000))
  }
})

test('command palette returns a server row for a seeded drawing name', async ({ page }) => {
  // Same private upload recipe as e2e/walk/fixtures.mjs open-private-drawing.
  const name = 'studio-s7a-search.dxf'
  const uploaded = await page.request.post('/api/drawings/upload', {
    headers: { 'X-Tenant-Id': LOCAL_IDENTITY.tenant },
    multipart: { file: { name, mimeType: 'application/dxf',
      buffer: await readFile(new URL('../fixtures/distinctive-panel.dxf', import.meta.url)) } },
    timeout: 60_000,
  })
  expect(uploaded.status()).toBe(202)
  const receipt = await uploaded.json()
  expect(receipt.drawing_id).toBeTruthy()
  await expect.poll(async () => {
    const status = await page.request.get(`/api/drawings/${receipt.drawing_id}/upload-status`, {
      headers: { 'X-Tenant-Id': LOCAL_IDENTITY.tenant }, timeout: 30_000,
    })
    if (!status.ok()) return `http-${status.status()}`
    const body = await status.json()
    expect(body.status).not.toBe('failed')
    return body.status
  }, { timeout: 60_000 }).toBe('ready')
  const response = await page.goto(`/app?drawing=${encodeURIComponent(receipt.drawing_id)}`)
  expect(response.ok()).toBe(true)
  await expect(page.locator('.studio-shell[data-scene="app"][data-mode="console"]')).toBeVisible()

  // site/WorkspaceEnvironment.jsx chooser, then PromptBox.jsx scope picker.
  // The chooser mounts after the shell, so wait for it: an instant count() misses
  // it and its backdrop then intercepts every click on the command bar.
  const environment = page.locator('section.workspace-environment')
  await expect(environment).toBeAttached()
  if (await environment.getAttribute('data-entered') !== 'true') {
    const resume = environment.getByRole('button', { name: 'Continue workspace', exact: true })
    const describe = environment.getByRole('button', { name: 'Describe what to create', exact: true })
    await expect(resume.or(describe).first()).toBeVisible()
    if (await resume.isVisible()) await resume.click()
    else await describe.click()
    await expect(environment).toHaveAttribute('data-entered', 'true')
  }
  const prompt = page.getByTestId('command-bar')
  await expect(prompt).toBeVisible()
  await page.locator('.bar-scope[aria-haspopup="listbox"]').click()
  const scopeOption = page.getByRole('listbox', { name: 'Scope', exact: true })
    .getByRole('option').filter({ hasText: /^find/ })
  // TEMPORARY DIAGNOSTIC (removed before landing): sanitized hit-target evidence only, no page text or HTML.
  paletteDiagnostic = await scopeOption.evaluate((target) => {
    const describe = (node) => {
      const style = getComputedStyle(node)
      return { tag: node.tagName.toLowerCase(), role: node.getAttribute('role'), cls: String(node.className || '').slice(0, 60),
        position: style.position, zIndex: style.zIndex, pointerEvents: style.pointerEvents, overflow: style.overflow }
    }
    const chain = (node) => { const out = []; for (let n = node; n && n !== document.documentElement && out.length < 8; n = n.parentElement) out.push(describe(n)); return out }
    const rect = target.getBoundingClientRect()
    const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)
    return { viewport: { width: innerWidth, height: innerHeight },
      targetRect: { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) },
      hitIsTarget: hit === target || target.contains(hit), targetChain: chain(target), hitChain: hit ? chain(hit) : null }
  }).catch((error) => ({ error: String(error && error.message).slice(0, 160) }))
  await scopeOption.click()
  await prompt.fill(name)
  const results = page.getByRole('listbox', { name: 'Search results', exact: true })
  await expect(results).toBeVisible()

  // components/PromptBox.jsx renders find-results-listbox / Search results
  // with data-kind rows. lib/palette.js exposes tools, versions, sessions,
  // projects and drawing objects, but no drawing-by-name result control.
  // server/routers/search.py explicitly names drawings-by-name as a gap:
  // there is no drawing-list/name index. Uploading a drawing cannot add it.
  const reason = 'Missing drawing-by-name result control in the command palette: PromptBox.jsx and lib/palette.js have no drawing row; server/routers/search.py explicitly lacks the drawings-by-name index, so a seeded drawing name cannot yield the required server result'
  test.fail(true, reason)
  // A real named row is required; Ask Leaf and local tool suggestions cannot
  // make an empty server search count as success.
  await expect(results.locator('.pb-result[data-kind]:not([data-kind="ask"])')
    .filter({ hasText: name })).not.toHaveCount(0, { timeout: 5000 })
})
