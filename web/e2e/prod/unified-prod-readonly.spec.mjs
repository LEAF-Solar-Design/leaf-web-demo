import { expect, test } from './prodFixtures.mjs'
import {
  assertProdResponse,
  requireFixtureProject,
  resolveExpectedSha,
  resolveProdBaseUrl,
  validateProdConfig,
} from './prodConfig.mjs'

const USER_AGENT = 'leaf-prod-readonly-smoke/1.0'
const prodUrl = (path) => new URL(path, resolveProdBaseUrl()).href

async function get(request, path, headers = {}) {
  const response = await request.get(prodUrl(path), {
    headers: { 'User-Agent': USER_AGENT, Authorization: '', ...headers },
  })
  assertProdResponse(response.url())
  return response
}

test('production-like unified route is reachable without mutation', async ({ page }) => {
  const response = await page.goto(prodUrl('/try'))
  assertProdResponse(response.url())
  assertProdResponse(page.url())
  await expect(page).toHaveURL(/\/try(?:\?|$)/)
  await expect(page.locator('body')).not.toContainText('Internal Server Error')
})

test('production app health reports its served source', async ({ request }) => {
  const response = await get(request, '/api/health')
  expect(response.ok()).toBeTruthy()
  const health = await response.json()
  expect(health.ok).toBe(true)
  expect(health.source_sha).toMatch(/^[a-f0-9]{40}$/i)
  test.info().annotations.push({ type: 'served-app-sha', description: health.source_sha })
  const expected = resolveExpectedSha()
  if (expected) expect(health.source_sha).toBe(expected)
})

test('production web health reports its served source', async ({ request }) => {
  const response = await get(request, '/health.json')
  expect(response.ok()).toBeTruthy()
  const health = await response.json()
  expect(health.service).toBe('leaf-platform-web')
  expect(health.source_sha).toMatch(/^[a-f0-9]{40}$/i)
  test.info().annotations.push({ type: 'served-web-sha', description: health.source_sha })
  const expected = resolveExpectedSha()
  if (expected) expect(health.source_sha).toBe(expected)
})

test('production deployment identity answers without a bearer', async ({ request }) => {
  const response = await get(request, '/api/deployment-identity')
  test.info().annotations.push({ type: 'deployment-identity-status', description: String(response.status()) })
  expect([401, 200]).toContain(response.status())
})

test('production auth ladder rejects absent and invalid bearers', async ({ request }) => {
  const absent = await get(request, '/api/capabilities')
  expect(absent.status()).toBe(401)
  const invalid = await get(request, '/api/capabilities', { Authorization: 'Bearer invalid' })
  expect([401, 403]).toContain(invalid.status())
})

test('production app displays its served build stamp', async ({ page }) => {
  test.setTimeout(60000)
  const response = await page.goto(prodUrl('/app?demo=1'))
  assertProdResponse(response.url())
  assertProdResponse(page.url())
  await expect(page.locator('text=/build \\d{4}-\\d{2}-\\d{2}/')).toBeVisible({ timeout: 30000 })
  assertProdResponse(page.url())
})

test('production app renders the studio shell', async ({ page }) => {
  test.setTimeout(60000)
  const response = await page.goto(prodUrl('/app?demo=1'))
  assertProdResponse(response.url())
  assertProdResponse(page.url())
  const shell = page.locator('.studio-shell[data-scene="app"]')
  await expect(shell).toHaveCount(1, { timeout: 30000 })
  // Attached, not visible: the region carries aria-hidden until the ground node attaches.
  const drawing = shell.locator('[role="region"][aria-label="Drawing"]')
  await expect(drawing).toHaveCount(1)
  await expect(drawing).toBeAttached()
  assertProdResponse(page.url())
})

test('production app and web serve the expected candidate', async ({ request }) => {
  const expected = resolveExpectedSha()
  expect(expected).toMatch(/^[0-9a-f]{40}$/)
  const app = await get(request, '/api/health')
  expect(app.ok()).toBeTruthy()
  const appHealth = await app.json()
  const web = await get(request, '/health.json')
  expect(web.ok()).toBeTruthy()
  const webHealth = await web.json()
  test.info().annotations.push({ type: 'expected-sha', description: expected })
  expect(appHealth.source_sha).toBe(expected)
  expect(webHealth.source_sha).toBe(expected)
})

async function listProjects(request) {
  const response = await get(request, '/api/projects', {
    Authorization: `Bearer ${process.env.LEAF_E2E_PROD_ACCEPTANCE_TOKEN}`,
  })
  expect(response.status()).toBe(200)
  const body = await response.json()
  expect(body !== null && typeof body === 'object' && !Array.isArray(body)).toBe(true)
  expect(Array.isArray(body.projects)).toBe(true)
  return body.projects
}

test.describe('CAD engine NOTICE', () => {
  test.use({ prodAuthenticated: true })

// Keep the test declaration at column zero for the inventory source contract.
test('P-062 production CAD edit surface shows the engine NOTICE', async ({ page, request }) => {
  test.setTimeout(60000)
  const projects = await listProjects(request)
  const project = requireFixtureProject(projects, validateProdConfig().projectId)
  const origin = new URL(resolveProdBaseUrl()).origin
  await page.addInitScript(({ origin, token }) => {
    if (window.location.origin === origin) localStorage.setItem('leaf.jwt', token)
  }, { origin, token: process.env.LEAF_E2E_PROD_ACCEPTANCE_TOKEN })
  try {
    // Follow the P-146 project board path, then its canonical drawing selector.
    const response = await page.goto(new URL('/app?surface=browser', origin).href)
    assertProdResponse(response.url())
    expect(response.ok()).toBe(true)
    const projectButton = page.getByRole('region', { name: 'Workspace projects', exact: true })
      .getByRole('list', { name: 'Projects', exact: true })
      .getByRole('button', { name: project.name, exact: true })
    await expect(projectButton).toHaveCount(1, { timeout: 30000 })
    const [opened] = await Promise.all([
      page.waitForResponse((result) =>
        result.url() === new URL(`/api/projects/${encodeURIComponent(project.project_id)}`, origin).href
        && result.request().method() === 'GET', { timeout: 30000 }),
      projectButton.click({ timeout: 30000 }),
    ])
    assertProdResponse(opened.url())
    expect(opened.status()).toBe(200)
    const workspace = await opened.json()
    expect(workspace.project.project_id).toBe(project.project_id)
    expect(Array.isArray(workspace.drawing_versions)).toBe(true)
    const [drawing] = [...workspace.drawing_versions].sort((a, b) =>
      (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0)
      || (b.seq || 0) - (a.seq || 0)
      || String(a.version_id).localeCompare(String(b.version_id)))
    expect(drawing, 'acceptance project must already contain a saved drawing version; no upload is allowed').toBeDefined()
    expect(typeof drawing.version_id).toBe('string')
    expect(drawing.version_id.length > 0).toBe(true)
    const version = page.getByRole('combobox', { name: 'Canonical drawing version', exact: true })
    await expect(version).toBeVisible({ timeout: 30000 })
    await version.selectOption(drawing.version_id)
    await expect(version).toHaveValue(drawing.version_id)
    const cadTab = page.getByRole('tab', { name: 'CAD', exact: true })
    await cadTab.click()
    await expect(cadTab).toHaveAttribute('aria-selected', 'true')
    // App.jsx opens the existing import pane locally; no file is selected.
    await page.getByRole('button', { name: 'Open a DXF in the browser engine', exact: true }).click()
    const surface = page.getByRole('region', { name: 'CAD editing surface', exact: true })
    await expect(surface).toBeVisible({ timeout: 30000 })
    const notice = surface.getByTestId('cad-edit-engine-notice')
    await expect(notice).toBeVisible({ timeout: 30000 })
    // Stable identifying substrings of server/routers/capabilities.py CAD_ENGINE_NOTICE.
    await expect(notice).toContainText('This product includes acadrust')
    await expect(notice).toContainText('licensed under the Mozilla Public License 2.0.')
    assertProdResponse(page.url())
  } finally {
    await page.close()
  }
})
})
