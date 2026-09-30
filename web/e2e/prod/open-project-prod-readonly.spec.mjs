import { expect, test } from '@playwright/test'
import { assertProdResponse, requireProdTarget, resolveProdBaseUrl } from './prodConfig.mjs'

const USER_AGENT = 'leaf-prod-readonly-smoke/1.0'
test.use({
  userAgent: USER_AGENT,
  extraHTTPHeaders: { 'User-Agent': USER_AGENT, Authorization: '' },
  storageState: { cookies: [], origins: [] },
  trace: 'off',
  screenshot: 'off',
})

test.beforeEach(() => {
  test.skip(!process.env.LEAF_E2E_PROD_BASE_URL, 'LEAF_E2E_PROD_BASE_URL is not set')
  test.skip(!process.env.LEAF_E2E_PROD_ACCEPTANCE_TOKEN, 'LEAF_E2E_PROD_ACCEPTANCE_TOKEN is not set')
  requireProdTarget()
  resolveProdBaseUrl()
})

async function get(request, path) {
  const response = await request.get(new URL(path, resolveProdBaseUrl()).href, {
    headers: {
      'User-Agent': USER_AGENT,
      Authorization: `Bearer ${process.env.LEAF_E2E_PROD_ACCEPTANCE_TOKEN}`,
    },
    maxRedirects: 0,
  })
  assertProdResponse(response.url())
  return response
}

// platform/api.py:403 list_projects returns { projects: [...] }.
async function listProjects(request) {
  const response = await get(request, '/api/projects')
  expect(response.status()).toBe(200)
  const body = await response.json()
  expect(body !== null && typeof body === 'object' && !Array.isArray(body)).toBe(true)
  expect(Array.isArray(body.projects)).toBe(true)
  return body.projects
}

test('P-146 production projects are readable', async ({ request }) => {
  const projects = await listProjects(request)
  test.info().annotations.push({ type: 'project-count', description: String(projects.length) })
})

test('P-146 production project board renders read-only', async ({ page, request }) => {
  test.setTimeout(60000)
  const projects = await listProjects(request)
  test.info().annotations.push({ type: 'project-count', description: String(projects.length) })
  test.skip(projects.length === 0, 'no project to render')
  const project = projects[0]
  expect(typeof project.project_id).toBe('string')
  expect(project.project_id.length > 0).toBe(true)
  expect(typeof project.name).toBe('string')
  const origin = new URL(resolveProdBaseUrl()).origin
  const reads = new Set(['GET', 'HEAD', 'OPTIONS'])
  const requests = []
  page.on('request', (sent) => {
    requests.push({ origin: new URL(sent.url()).origin, method: sent.method() })
  })
  // auth.js/isSignedIn and api.js/authHeaders both read this origin's leaf.jwt.
  await page.addInitScript(({ origin, token }) => {
    if (window.location.origin === origin) localStorage.setItem('leaf.jwt', token)
  }, { origin, token: process.env.LEAF_E2E_PROD_ACCEPTANCE_TOKEN })
  await page.route('**/*', async (route) => {
    const sent = route.request()
    const headers = { ...sent.headers() }
    delete headers.authorization
    if (new URL(sent.url()).origin !== origin) {
      await route.continue({ headers })
      return
    }
    if (!reads.has(sent.method())) {
      await route.abort('blockedbyclient')
      return
    }
    headers.authorization = `Bearer ${process.env.LEAF_E2E_PROD_ACCEPTANCE_TOKEN}`
    // Do not let an authenticated request carry its header across a redirect.
    const response = await route.fetch({ headers, maxRedirects: 0 })
    await route.fulfill({ response })
  })
  try {
    // SiteRoot routes /app to the console; productSurfaces selects Browser's board.
    // createWorkspaceController opens projects through the list, not a deep link.
    const response = await page.goto(new URL('/app?surface=browser', origin).href)
    assertProdResponse(response.url())
    assertProdResponse(page.url())
    expect(response.ok()).toBe(true)
    const [opened] = await Promise.all([
      page.waitForResponse((result) =>
        result.url() === new URL(`/api/projects/${encodeURIComponent(project.project_id)}`, origin).href
        && result.request().method() === 'GET', { timeout: 30000 }),
      page.getByRole('region', { name: 'Workspace projects', exact: true })
        .getByRole('list', { name: 'Projects', exact: true })
        .getByRole('button', { name: project.name, exact: true }).first().click({ timeout: 30000 }),
    ])
    assertProdResponse(opened.url())
    expect(opened.status()).toBe(200)
    const workspace = await opened.json()
    expect(workspace.project.project_id).toBe(project.project_id)
    const board = page.getByRole('region', { name: 'Project workspace', exact: true })
    await expect(board).toBeVisible({ timeout: 30000 })
    await expect(board).toHaveAttribute('data-project-state', 'project', { timeout: 30000 })
    await expect(page.getByText('You’re not signed in', { exact: true })).not.toBeVisible()
    await expect(page.getByText('Sign in or explore the demo to load a drawing.', { exact: true })).not.toBeVisible()
    await expect(page.getByRole('heading', { name: 'Something went wrong', exact: true })).not.toBeVisible()
    await expect(page.locator('.ground-note')).not.toBeVisible()
    assertProdResponse(page.url())
    await expect(page).toHaveURL(new URL('/app?surface=browser', origin).href)
    test.info().annotations.push({ type: 'rendered-route', description: '/app?surface=browser' })
  } finally {
    // Include unload beacons in the read-only assertion, with no headers recorded.
    await page.close()
    expect(requests.filter((sent) => sent.origin === origin && !reads.has(sent.method()))
      .map((sent) => sent.method()), 'production requests must be read-only').toEqual([])
  }
})

test('P-058 production solar_template_beta is on', async ({ request }) => {
  // server/routers/templates.py:223 exposes solar_template_beta through this read.
  const response = await get(request, '/api/templates')
  if (response.status() === 404) {
    const body = await response.json()
    if (body.error?.message === 'solar template beta is not enabled') {
      throw new Error('solar_template_beta is off: solar template beta is not enabled')
    }
  }
  expect(response.status(), 'solar_template_beta must be on').toBe(200)
  const body = await response.json()
  expect(body !== null && typeof body === 'object' && !Array.isArray(body)).toBe(true)
  expect(Array.isArray(body.templates)).toBe(true)
  test.info().annotations.push({ type: 'feature-flag', description: 'solar_template_beta=on' })
})

test('P-058 production conv_durable is on', async ({ request }) => {
  const projects = await listProjects(request)
  test.skip(projects.length === 0, 'no project to probe')
  const projectId = projects[0].project_id
  expect(typeof projectId).toBe('string')
  expect(projectId.length > 0).toBe(true)
  // server/routers/conversations.py:320-341 exposes conv_durable through a read-only GET.
  const response = await get(request, `/api/projects/${encodeURIComponent(projectId)}/conversations/recovery/tail?limit=1`)
  if (response.status() === 404) {
    const body = await response.json()
    if (body.error?.message === 'conversation persistence is not enabled') {
      throw new Error('conv_durable is off: conversation persistence is not enabled')
    }
  }
  expect(response.status(), 'conv_durable must be on').toBe(200)
  test.info().annotations.push({ type: 'feature-flag', description: 'conv_durable=on' })
})
