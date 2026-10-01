import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

// Browser proof for the MOUNTED project lifecycle panel: web/src/site/
// ToolCast.jsx renders ProjectLifecyclePanel on the Project tab when the build
// carries VITE_LIFECYCLE_UI=1 (playwright.lifecycle.config.mjs sets it). The
// HTTP-only companion, e2e/lifecycle.spec.mjs, proves tenant isolation against
// staging; this file proves the clicks: clone, export and delete each send the
// exact request the platform routes expect and render the server's receipt.
//
// Every API call is answered by page.route below. The cat proof fixture serves
// the session and drawing so the workspace is operable; the project routes are
// served here with UUID ids, because web/src/projects/api.js refuses any
// non-UUID id before it reaches the wire. Any http(s) request to a host other
// than 127.0.0.1 or the mocked API origin is aborted and fails the test.
//
// Run from web/: npx playwright test --config playwright.lifecycle.config.mjs
// HONEST LIMIT: mocks cannot prove tenant isolation or server authorization.

const API_ORIGIN = 'http://leaf-proof.invalid'
const ORG_ID = 'cat-proof-org'
const PROJECT_ID = '6f1c2a3e-4b5d-4e6f-8a7b-9c0d1e2f3a4b'
const CLONE_ID = '0a1b2c3d-4e5f-4a6b-8c7d-8e9f0a1b2c3d'
const BINDING_ID = '1d2e3f4a-5b6c-4d7e-8f9a-0b1c2d3e4f5a'
const MEMBERSHIP_ID = '2e3f4a5b-6c7d-4e8f-9a0b-1c2d3e4f5a6b'
const PROJECT_NAME = 'Lifecycle Roof'
const CLONE_NAME = `${PROJECT_NAME} (copy)`
const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const RECEIPT = {
  clone: 'rcpt-lifecycle-clone-0001',
  export: 'rcpt-lifecycle-export-0001',
  delete: 'rcpt-lifecycle-delete-0001',
}
const EXPORT_ARTIFACT = {
  schema: 'leaf.project-export.v1',
  project: { project_id: PROJECT_ID, name: PROJECT_NAME },
  files: [{ name: 'roof.dxf', sha256: 'a'.repeat(64) }],
  members: [{ role: 'owner' }],
}

function makeLifecycleState() {
  return {
    deleted: false,
    projects: [{ project_id: PROJECT_ID, name: PROJECT_NAME, status: 'active' }],
    receipts: [{
      receipt_id: 'rcpt-lifecycle-create-0001', action: 'project.create_blank',
      input_digest: 'b'.repeat(64), created_at: '2026-09-30T12:00:00Z',
    }],
  }
}

function receipt(id, action) {
  return { receipt_id: id, action, input_digest: 'c'.repeat(64), created_at: '2026-09-30T12:05:00Z' }
}

// Answers the project routes; returns null for anything the cat fixture owns.
function lifecycleResponse({ method, path }, state) {
  const json = (body, status = 200) => ({ status, body })
  const gone = () => json({ detail: 'That project is no longer available to you.' }, 404)
  const project = { project_id: PROJECT_ID, org_id: ORG_ID, name: PROJECT_NAME, status: 'active' }
  if (path === '/api/projects' && method === 'GET') return json({ projects: state.projects })
  if (path === `/api/projects/${PROJECT_ID}` && method === 'GET') {
    if (state.deleted) return gone()
    return json({
      project,
      drawing_versions: [{
        version_id: 'lifecycle-version-1', drawing_id: 'cat-panels', seq: 1,
        org_id: ORG_ID, project_id: PROJECT_ID,
      }],
      jobs: [],
      built_tools: [],
    })
  }
  if (path === `/api/projects/${PROJECT_ID}/lifecycle` && method === 'GET') {
    if (state.deleted) return gone()
    return json({
      project,
      members: [{
        membership_id: MEMBERSHIP_ID, binding_id: BINDING_ID, role: 'owner',
        display_name: 'Proof Operator', created_at: '2026-09-30T12:00:00Z',
      }],
      files: [{ name: 'roof.dxf' }],
      receipts: state.receipts,
      viewer: {
        membership_id: MEMBERSHIP_ID, binding_id: BINDING_ID, role: 'owner',
        can_invite: true, can_manage: true, can_label_identities: false,
      },
    })
  }
  if (path === `/api/projects/${PROJECT_ID}/clone` && method === 'POST') {
    const minted = receipt(RECEIPT.clone, 'project.clone')
    state.receipts = [...state.receipts, minted]
    state.projects = [...state.projects, { project_id: CLONE_ID, name: CLONE_NAME, status: 'active' }]
    return json({
      project: { project_id: CLONE_ID, org_id: ORG_ID, name: CLONE_NAME, status: 'active' },
      source_project_id: PROJECT_ID,
      copied_file_count: 1,
      receipt: minted,
    })
  }
  if (path === `/api/projects/${PROJECT_ID}/export` && method === 'POST') {
    const minted = receipt(RECEIPT.export, 'project.export')
    state.receipts = [...state.receipts, minted]
    return json({
      export: EXPORT_ARTIFACT, export_sha256: 'd'.repeat(64),
      file_count: 1, member_count: 1, receipt: minted,
    })
  }
  if (path === `/api/projects/${PROJECT_ID}` && method === 'DELETE') {
    state.deleted = true
    state.projects = state.projects.filter((p) => p.project_id !== PROJECT_ID)
    return json({ project_id: PROJECT_ID, deleted: true, receipt: receipt(RECEIPT.delete, 'project.delete') })
  }
  return null
}

async function bootLifecyclePanel(page) {
  const proofState = makeCatProofState()
  const state = makeLifecycleState()
  const mutations = []
  const unhandled = []
  const offHost = []

  await page.addInitScript((orgId) => localStorage.setItem('leaf.org_id', orgId), ORG_ID)

  // Lower priority than the API route below (Playwright runs the newest
  // matching route first); the predicate also excludes the API origin.
  await page.route((url) => /^(https?|wss?):$/.test(url.protocol)
    && url.hostname !== '127.0.0.1' && url.origin !== API_ORIGIN, async (route) => {
    offHost.push(route.request().url())
    await route.abort()
  })

  await page.route(`${API_ORIGIN}/api/**`, async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const raw = request.postData()
    let body = {}
    if (raw) {
      try { body = JSON.parse(raw) } catch { body = {} }
    }
    if (url.pathname.startsWith(`/api/projects/${PROJECT_ID}`) && method !== 'GET' && method !== 'OPTIONS') {
      mutations.push({ method, path: url.pathname, raw, body, headers: await request.allHeaders() })
    }
    const result = lifecycleResponse({ method, path: url.pathname }, state)
      ?? catProofResponse({ method, path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, proofState)
    if (result.status === 404 && /unhandled proof route/.test(result.body?.error?.message || '')) {
      unhandled.push(`${method} ${url.pathname}`)
    }
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try?proof=1')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 30_000 })
  // Same settle the standards-surface walk uses before the header switcher.
  await page.waitForTimeout(2_600)
  await expect(page.getByTestId('continuity-rail')).toHaveAttribute('data-project-state', 'drawing-only')

  await page.locator('.proj-switch .proj-chip').click()
  await page.getByRole('menuitem', { name: new RegExp(PROJECT_NAME) }).click()
  await expect(page.getByTestId('continuity-rail')).toHaveAttribute('data-project-state', 'project')

  const panel = page.getByTestId('projects-surface')
  await expect(panel).toBeVisible({ timeout: 15_000 })
  await expect(panel.getByRole('button', { name: 'Clone project' })).toBeVisible({ timeout: 15_000 })
  await expect(panel.locator('.receipt-row[data-receipt-id="rcpt-lifecycle-create-0001"]')).toBeVisible()
  return { panel, state, mutations, unhandled, offHost }
}

function expectMutation(entry, method, path) {
  expect(entry.method).toBe(method)
  expect(entry.path).toBe(path)
  // A fresh idempotency key per mutation, so a retry never replays a receipt.
  // 127.0.0.1 is a secure context, so api.js mints it with crypto.randomUUID.
  expect(entry.headers['idempotency-key']).toMatch(UUID_SHAPE)
}

// Routes outside the lifecycle contract that the fixture does not serve are
// recorded for diagnosis only; a request off 127.0.0.1 fails the test.
function expectCleanRun({ unhandled, offHost }) {
  if (unhandled.length) test.info().annotations.push({ type: 'unhandled-routes', description: unhandled.join(', ') })
  expect(offHost).toEqual([])
}

test('clone sends the derived copy name and shows the server receipt', async ({ page }) => {
  const run = await bootLifecyclePanel(page)
  const { panel, mutations } = run

  await panel.getByRole('button', { name: 'Clone project' }).click()
  const dialog = page.getByRole('dialog', { name: `Clone ${PROJECT_NAME}` })
  await expect(dialog).toContainText(`This creates a new project you own, copied from "${PROJECT_NAME}".`)
  await dialog.getByRole('button', { name: 'Clone project' }).click()

  await expect(dialog.locator('.clone-dialog-success')).toHaveText(`Clone complete: "${CLONE_NAME}", owned by you.`)
  await expect(dialog.locator('.clone-dialog-receipt-id')).toHaveText(RECEIPT.clone)

  expect(mutations).toHaveLength(1)
  const [clone] = mutations
  expectMutation(clone, 'POST', `/api/projects/${PROJECT_ID}/clone`)
  expect(clone.headers['content-type']).toBe('application/json')
  expect(JSON.parse(clone.raw)).toEqual({ name: CLONE_NAME })

  // The refetch after the mutation carries the clone receipt into the timeline.
  await expect(panel.locator(`.receipt-row[data-receipt-id="${RECEIPT.clone}"]`)).toBeVisible()
  await dialog.getByRole('button', { name: 'Close' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(page.getByTestId('continuity-rail')).toHaveAttribute('data-project-state', 'project')
  expectCleanRun(run)
})

test('export posts with no body and downloads the sanitized artifact', async ({ page }) => {
  const run = await bootLifecyclePanel(page)
  const { panel, mutations } = run

  await panel.getByRole('button', { name: 'Export project' }).click()
  const dialog = page.getByRole('dialog', { name: 'Export project' })
  await expect(dialog).toContainText('no draft or working-file state included')
  await dialog.getByRole('button', { name: 'Export', exact: true }).click()

  await expect(dialog.locator('.export-dialog-receipt-id')).toHaveText(RECEIPT.export)
  expect(mutations).toHaveLength(1)
  const [exported] = mutations
  expectMutation(exported, 'POST', `/api/projects/${PROJECT_ID}/export`)
  expect(exported.raw).toBeNull()

  const link = dialog.getByRole('link', { name: 'Download' })
  await expect(link).toHaveAttribute('download', 'lifecycle-roof-export.json')
  const [download] = await Promise.all([page.waitForEvent('download'), link.click()])
  expect(download.suggestedFilename()).toBe('lifecycle-roof-export.json')
  const saved = await download.path()
  expect(JSON.parse(readFileSync(saved, 'utf8'))).toEqual(EXPORT_ARTIFACT)

  await expect(panel.locator(`.receipt-row[data-receipt-id="${RECEIPT.export}"]`)).toBeVisible()
  await dialog.getByRole('button', { name: 'Close' }).click()
  await expect(dialog).toHaveCount(0)
  expectCleanRun(run)
})

test('delete needs the typed name, sends a bodyless DELETE and returns to the drawing fallback', async ({ page }) => {
  const run = await bootLifecyclePanel(page)
  const { panel, mutations } = run

  const zone = panel.locator('.danger-action-delete')
  await zone.getByRole('button', { name: 'Delete', exact: true }).click()
  await expect(zone).toContainText("This can't be undone.")
  const confirm = zone.getByRole('button', { name: 'Delete', exact: true })
  const typed = zone.getByLabel('Type the project name to confirm delete')

  await typed.fill('Lifecycle')
  await expect(confirm).toBeDisabled()
  expect(mutations).toHaveLength(0)

  await typed.fill(PROJECT_NAME)
  await expect(confirm).toBeEnabled()
  await confirm.click()

  // The panel unmounts with the project; the receipt survives in the toast.
  await expect(page.locator('.toast').filter({ hasText: 'Project deleted.' }))
    .toContainText(`Project deleted. Receipt ${RECEIPT.delete}`)
  await expect(page.getByTestId('projects-surface')).toHaveCount(0)
  const rail = page.getByTestId('continuity-rail')
  await expect(rail).toHaveAttribute('data-project-state', 'drawing-only')
  await expect(rail).toContainText('no workspace project')
  await expect(page.locator('.proj-switch .proj-chip .tag')).toHaveText('Drawing')

  expect(mutations).toHaveLength(1)
  const [removed] = mutations
  expectMutation(removed, 'DELETE', `/api/projects/${PROJECT_ID}`)
  expect(removed.raw).toBeNull()
  expect(run.state.deleted).toBe(true)
  expectCleanRun(run)
})
