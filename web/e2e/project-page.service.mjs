import { readFile } from 'node:fs/promises'
import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' }
const API = 'http://leaf-proof.invalid'

async function openProject(page, name) {
  await page.getByRole('button', { name: 'Project', exact: true }).click()
  const sidebar = page.getByRole('region', { name: 'Project sidebar', exact: true })
  const projects = sidebar.getByRole('region', { name: 'Workspace projects', exact: true })
  const project = projects.getByRole('button', { name, exact: true })
  const selected = page.getByRole('button', { name: `Projects: change project. Project ${name}`, exact: true })
  await expect(selected.or(project)).toBeVisible({ timeout: 30_000 })
  if (!await selected.isVisible()) {
    await project.click({ timeout: 30_000 })
    await page.getByRole('button', { name: 'Project', exact: true }).click()
  }
  await expect(selected).toBeVisible({ timeout: 30_000 })
  const editor = sidebar.getByRole('region', { name: 'Page editor', exact: true })
  await expect(editor).toBeVisible()
  return editor
}

// Exercise the same optional design controls in both editing surfaces, without
// replacing the existing portfolio/photo/native and full control regressions.
async function design(inspector, variant) {
  await inspector.getByLabel('Elements / Outline', { exact: true }).selectOption({ label: 'Headline' })
  for (const [label, value] of Object.entries({ Font: variant ? 'sans' : 'serif', Weight: variant ? 'medium' : 'bold', Alignment: variant ? 'left' : 'center' })) {
    await inspector.getByLabel(label, { exact: true }).selectOption(value)
  }
  for (const [label, value] of Object.entries({ 'Text size': variant ? '36' : '40', 'Text color': variant ? '#eeeeee' : '#222222', 'Line height': '1.4', 'Letter spacing': variant ? '1' : '2' })) {
    await inspector.getByLabel(label, { exact: true }).fill(value)
  }
  await inspector.getByLabel('Italic', { exact: true }).setChecked(!variant)
  await inspector.getByLabel('Underline', { exact: true }).setChecked(!variant)
  await inspector.getByLabel('Hidden', { exact: true }).check()
  await inspector.getByLabel('Hidden', { exact: true }).uncheck()
  await inspector.getByLabel('Elements / Outline', { exact: true }).selectOption({ label: 'Page appearance' })
  for (const [label, value] of Object.entries({ 'Page theme': variant ? 'dark' : 'cream', 'Page alignment': variant ? 'left' : 'center', Font: variant ? 'sans' : 'serif', 'Text scale': variant ? 'regular' : 'large', Spacing: 'airy', 'Content width': 'narrow' })) {
    await inspector.getByLabel(label, { exact: true }).selectOption(value)
  }
  for (const [label, value] of Object.entries({ Gap: '24', Padding: '32', 'Maximum width': '900', Background: variant ? '#111111' : '#fff8ee', Text: variant ? '#eeeeee' : '#222222', Muted: '#888888', Accent: '#aa6600' })) {
    await inspector.getByLabel(label, { exact: true }).fill(value)
  }
}

function expectedDocument(run, variant) {
  return {
    label: `Label ${variant ? 'B' : 'A'} ${run}`,
    headline: `Smoke ${variant ? 'B' : 'A'} ${run}`,
    description: `Description ${variant ? 'B' : 'A'} ${run}`,
    status: `Status ${variant ? 'B' : 'A'} ${run}`,
    theme: variant ? 'dark' : 'cream', alignment: variant ? 'left' : 'center',
    textStyles: { headline: { font: variant ? 'sans' : 'serif', size: variant ? 36 : 40,
      weight: variant ? 'medium' : 'bold', color: variant ? '#eeeeee' : '#222222',
      align: variant ? 'left' : 'center', lineHeight: 1.4, letterSpacing: variant ? 1 : 2,
      ...(!variant ? { italic: true, underline: true } : {}) } },
    appearance: { font: variant ? 'sans' : 'serif', scale: variant ? 'regular' : 'large',
      spacing: 'airy', width: 'narrow', gap: 24, padding: 32, maxWidth: 900,
      palette: { background: variant ? '#111111' : '#fff8ee', text: variant ? '#eeeeee' : '#222222', muted: '#888888', accent: '#aa6600' } },
  }
}

test('page_service_save_reload_two_styles', async ({ page, request, baseURL }, testInfo) => {
  const started = Date.now()
  let ready
  await expect.poll(async () => {
    try { ready = JSON.parse(await readFile(`${process.env.LEAF_PAGE_SMOKE_DIR}/ready.json`, 'utf8')); return ready.schema }
    catch { return null }
  }, { timeout: 10_000 }).toBe('leaf.page-smoke.v1')
  const worker = process.env.LEAF_NATIVE_GATE_WORKER ?? '0'
  expect(ready.api_origin).toBe(`http://127.0.0.1:${8189 + 100 * Number(worker)}`)
  const pagePath = `/api/projects/${ready.project_id}/page`
  const calls = [], foreign = [], state = makeCatProofState()
  const project = { project_id: ready.project_id, name: ready.project_name, status: 'active' }
  const token = `proof.${Buffer.from(JSON.stringify({ iss: 'https://page-smoke.invalid/', sub: ready.binding_id })).toString('base64url')}.fixture`
  await page.addInitScript(({ ready, token }) => {
    // Only a UI account-scope fixture. The real API never receives this token.
    localStorage.setItem('leaf.jwt', token)
    localStorage.setItem('leaf.org_id', ready.org_id)
    localStorage.setItem('leaf.actor_binding_id', ready.binding_id)
  }, { ready, token })
  await page.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url()), method = req.method()
    if (url.origin === baseURL) return route.continue()
    if (url.origin !== API) {
      foreign.push({ method, origin: url.origin, path: url.pathname })
      return route.abort()
    }
    if (method === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
    if ((url.pathname === '/api/projects' && method === 'GET') ||
        (url.pathname === pagePath && ['GET', 'PUT'].includes(method))) {
      const headers = await req.allHeaders()
      expect(headers['x-org-id']).toBe(ready.org_id)
      const browserActor = headers['x-actor-binding-id'] ?? null
      if (url.pathname === pagePath) expect(browserActor).toBe(ready.binding_id)
      // The legacy project-list client sends org only; disclose the adapter's
      // actor addition separately. Page identity headers are never repaired.
      if (url.pathname === '/api/projects') headers['x-actor-binding-id'] = ready.binding_id
      delete headers.authorization
      delete headers.cookie
      delete headers.host
      const response = await route.fetch({ url: `${ready.api_origin}${url.pathname}${url.search}`, headers, maxRedirects: 0, timeout: 30_000 })
      const body = await response.json()
      calls.push({ method, path: url.pathname, status: response.status(),
        org: headers['x-org-id'], actor: headers['x-actor-binding-id'], browserActor,
        authorization: Object.hasOwn(headers, 'authorization'),
        revision: body.page?.revision, document: body.page?.document,
        expectedRevision: method === 'PUT' ? req.postDataJSON().expected_revision : undefined })
      return route.fulfill({ response, headers: { ...response.headers(), ...CORS } })
    }
    let reply
    if (method === 'GET' && url.pathname === `/api/projects/${ready.project_id}`) {
      reply = { status: 200, body: { project, drawing_versions: [], jobs: [], built_tools: [], drawing_artifacts: [] } }
    } else if (method === 'GET' && url.pathname === `/api/orgs/${ready.org_id}`) {
      reply = { status: 200, body: { org: { org_id: ready.org_id, name: ready.project_name, status: 'active' } } }
    } else if (method === 'GET' && url.pathname === '/api/campaigns') {
      reply = { status: 200, body: { campaigns: [] } }
    } else {
      reply = catProofResponse({ method, path: url.pathname, body: req.postData() ? req.postDataJSON() : {}, query: Object.fromEntries(url.searchParams) }, state)
      if (url.pathname === '/api/session') reply.body.org_id = ready.org_id
    }
    return route.fulfill({ status: reply.status, headers: CORS, contentType: 'application/json', body: reply.body == null ? '' : JSON.stringify(reply.body) })
  })
  const directHeaders = { 'X-Org-Id': ready.org_id, 'X-Actor-Binding-Id': ready.binding_id }
  const readback = async (revision, document) => {
    const response = await request.get(`${ready.api_origin}${pagePath}`, { headers: directHeaders, maxRedirects: 0 })
    expect(response.status()).toBe(200)
    expect((await response.json()).page).toMatchObject({ revision, document })
    expect((await response.json()).page.document).toEqual(document)
  }
  const run = ready.project_name.slice(-8)
  const a = expectedDocument(run, false), b = expectedDocument(run, true)
  const toolbar = page.locator('.canvas-toolbar')
  const preview = page.locator('[data-ground="browser-asset"] .project-page')
  const style = async value => {
    await toolbar.getByRole('button', { name: 'Settings', exact: true }).click()
    await page.getByLabel('Editing style', { exact: true }).selectOption(value)
    await page.getByRole('button', { name: 'Close settings', exact: true }).click()
    await toolbar.getByRole('button', { name: 'Edit', exact: true }).click()
  }
  try {
    await page.goto('/app?surface=browser')
    let editor = await openProject(page, ready.project_name)
    await editor.getByRole('button', { name: 'Create a page', exact: true }).click()
    await style('environment')
    for (const field of ['label', 'headline', 'description', 'status']) {
      await preview.getByRole('textbox', { name: field, exact: true }).fill(a[field])
    }
    await toolbar.getByRole('button', { name: 'Elements / Outline', exact: true }).click()
    await design(page.locator('.canvas-properties'), false)
    await page.locator('.canvas-properties').getByRole('button', { name: 'Close properties', exact: true }).click()
    await toolbar.getByRole('button', { name: 'Save page', exact: true }).click()
    await expect(toolbar.getByRole('status')).toHaveText('Saved')
    expect(calls.filter(call => call.method === 'PUT')).toMatchObject([{ status: 200, revision: 1, expectedRevision: 0, document: a }])
    await readback(1, a)

    await page.reload()
    editor = await openProject(page, ready.project_name)
    await editor.getByRole('button', { name: 'Open page', exact: true }).click()
    await expect(preview).toHaveAttribute('data-theme', 'cream')
    await expect(preview).toHaveAttribute('data-alignment', 'center')
    await expect(editor.getByRole('textbox', { name: 'Headline', exact: true })).toHaveValue(a.headline)
    expect(calls.filter(call => call.method === 'GET' && call.path === pagePath).at(-1)).toMatchObject({ status: 200, revision: 1, document: a })
    await style('sidebar')
    await preview.getByRole('button', { name: 'Edit headline', exact: true }).click()
    await expect(editor.getByRole('textbox', { name: 'Headline', exact: true })).toBeFocused()
    for (const [field, label] of [['label', 'Page label'], ['headline', 'Headline'], ['description', 'Description'], ['status', 'Status text']]) {
      await editor.getByRole('textbox', { name: label, exact: true }).fill(b[field])
    }
    await editor.locator('summary').filter({ hasText: /^Element design$/ }).click()
    await design(editor.getByRole('region', { name: 'Element properties', exact: true }), true)
    await editor.getByRole('button', { name: 'Save page', exact: true }).click()
    await expect(editor.locator('.project-page-editor__save-row').getByRole('status')).toHaveText('Saved')
    expect(calls.filter(call => call.method === 'PUT')).toMatchObject([
      { status: 200, revision: 1, expectedRevision: 0, document: a },
      { status: 200, revision: 2, expectedRevision: 1, document: b },
    ])
    await readback(2, b)
    await page.reload()
    editor = await openProject(page, ready.project_name)
    await editor.getByRole('button', { name: 'Open page', exact: true }).click()
    await expect(editor.getByRole('textbox', { name: 'Headline', exact: true })).toHaveValue(b.headline)
    await expect(preview).toHaveAttribute('data-theme', 'dark')
    await expect(preview).toHaveAttribute('data-alignment', 'left')
    expect(calls.filter(call => call.method === 'GET' && call.path === pagePath).at(-1)).toMatchObject({ status: 200, revision: 2, document: b })
    const stale = await request.put(`${ready.api_origin}${pagePath}`, {
      headers: directHeaders, maxRedirects: 0, data: { expected_revision: 1, document: a },
    })
    expect(stale.status()).toBe(409)
    await readback(2, b)
    expect(calls.some(call => call.method === 'GET' && call.path === '/api/projects' && call.status === 200)).toBe(true)
    expect(foreign).toEqual([])
    for (const call of calls) {
      expect(call.org).toBe(ready.org_id)
      expect(call.actor).toBe(ready.binding_id)
      expect(call.authorization).toBe(false)
    }
  } finally {
    await testInfo.attach('page-service-wire', { body: Buffer.from(JSON.stringify({ calls, foreign, browser_ms: Date.now() - started })), contentType: 'application/json' })
  }
})
