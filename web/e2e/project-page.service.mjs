import { readFile } from 'node:fs/promises'
import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' }
const API = 'http://leaf-proof.invalid'

// Implicit labels contain option text in Playwright's getByLabel lookup.
// Match the actual combobox's label prefix within its existing inspector.
const selectControl = (scope, label) => scope.getByRole('combobox', {
  name: new RegExp(`^${label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?:\\s|$)`),
})

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

// The service boundary needs representative edits in each surface. The existing
// mocked editor regressions retain the complete design-control matrix.
function expectedDocument(run, variant) {
  return {
    label: `Label ${variant ? 'B' : 'A'} ${run}`,
    headline: `Smoke ${variant ? 'B' : 'A'} ${run}`,
    description: `Description ${variant ? 'B' : 'A'} ${run}`,
    status: `Status ${variant ? 'B' : 'A'} ${run}`,
    theme: variant ? 'cream' : 'dark', alignment: variant ? 'left' : 'center',
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
        'authorization': Object.hasOwn(headers, 'authorization'),
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
    const body = await response.json()
    expect(body.page).toMatchObject({ revision, document })
    expect(body.page.document).toEqual(document)
  }
  const run = ready.project_name.slice(-8)
  const a = expectedDocument(run, false), b = expectedDocument(run, true)
  const toolbar = page.locator('.canvas-toolbar')
  const preview = page.locator('[data-ground="browser-asset"] .project-page')
  const style = async value => {
    await toolbar.getByRole('button', { name: 'Settings', exact: true }).click()
    await selectControl(page.getByRole('region', { name: 'Settings', exact: true }), 'Editing style').selectOption(value)
    // Sidebar selection opens the Project drawer over the canvas. Dismiss it
    // through its normal control before returning to Settings and canvas Edit.
    // Selecting the preview headline below opens the same Sidebar for editing.
    if (value === 'sidebar') {
      await page.getByRole('button', { name: 'Close Project', exact: true }).click()
      await expect(page.getByRole('region', { name: 'Project sidebar', exact: true })).toBeHidden()
    }
    await page.getByRole('button', { name: 'Close settings', exact: true }).click()
    await toolbar.getByRole('button', { name: 'Edit', exact: true }).click()
    await expect(toolbar.getByRole('button', { name: 'Edit', exact: true })).toHaveAttribute('aria-pressed', 'true')
  }
  const visiblePreview = async document => {
    await expect(preview).toBeVisible()
    for (const field of ['label', 'headline', 'description', 'status']) {
      await expect(preview.locator(`.project-page__${field}`)).toHaveText(document[field])
    }
    await expect(preview).toHaveAttribute('data-theme', document.theme)
    await expect(preview).toHaveAttribute('data-alignment', document.alignment)
  }
  const openSidebarForHeadline = async editor => {
    // Opening a saved page activates its preview in View mode and dismisses
    // Project. Enter Edit before preview selection can reopen the Sidebar.
    await toolbar.getByRole('button', { name: 'Edit', exact: true }).click()
    await expect(toolbar.getByRole('button', { name: 'Edit', exact: true })).toHaveAttribute('aria-pressed', 'true')
    await preview.getByRole('button', { name: 'Edit headline', exact: true }).click()
    await expect(editor).toBeVisible()
    await expect(editor.getByRole('textbox', { name: 'Headline', exact: true })).toBeFocused()
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
    const properties = page.locator('.canvas-properties')
    await expect(properties).toBeVisible()
    await selectControl(properties, 'Elements / Outline').selectOption({ label: 'Page appearance' })
    await selectControl(properties, 'Page theme').selectOption(a.theme)
    await page.locator('.canvas-properties').getByRole('button', { name: 'Close properties', exact: true }).click()
    await toolbar.getByRole('button', { name: 'Save page', exact: true }).click()
    await expect(toolbar.getByRole('status')).toHaveText('Saved')
    expect(calls.filter(call => call.method === 'PUT')).toMatchObject([{ status: 200, revision: 1, expectedRevision: 0, document: a }])
    await readback(1, a)

    await page.reload()
    editor = await openProject(page, ready.project_name)
    await editor.getByRole('button', { name: 'Open page', exact: true }).click()
    // Reload starts in View mode. Assert rendered content, not an input in a
    // Sidebar that the selected environment style can hide.
    await visiblePreview(a)
    await readback(1, a)
    expect(calls.filter(call => call.method === 'GET' && call.path === pagePath).at(-1)).toMatchObject({ status: 200, revision: 1, document: a })
    await style('sidebar')
    await openSidebarForHeadline(editor)
    for (const [field, label] of [['label', 'Page label'], ['headline', 'Headline'], ['description', 'Description'], ['status', 'Status text']]) {
      await editor.getByRole('textbox', { name: label, exact: true }).fill(b[field])
    }
    await editor.getByRole('combobox', { name: 'Theme', exact: true }).selectOption(b.theme)
    await editor.getByRole('combobox', { name: 'Alignment', exact: true }).selectOption(b.alignment)
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
    await visiblePreview(b)
    await readback(2, b)
    expect(calls.filter(call => call.method === 'GET' && call.path === pagePath).at(-1)).toMatchObject({ status: 200, revision: 2, document: b })
    // Keep the persisted style: do not select Sidebar again after reload.
    await openSidebarForHeadline(editor)
    for (const [field, label] of [['label', 'Page label'], ['headline', 'Headline'], ['description', 'Description'], ['status', 'Status text']]) {
      await expect(editor.getByRole('textbox', { name: label, exact: true })).toBeVisible()
      await expect(editor.getByRole('textbox', { name: label, exact: true })).toHaveValue(b[field])
    }
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
