import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

// Lane C (studio-lanes-20260930): browser proof for the Campaign panel
// (web/src/campaigns/CampaignPanel.jsx) mounted on the Browser board of an open
// project. Every scenario counts the exact campaign and project writes it sent,
// checks every campaign read against the paths the state can reach, and
// aborts (and fails on) any request that leaves 127.0.0.1 or the proof API.
//
// Every scenario starts from an empty campaign list and reaches its state
// through a user action. The dev server runs React StrictMode, whose
// setup-cleanup-setup probe can drop useCampaigns' first list read, so no
// assertion depends on that first read being rendered. The named empty status
// ("No campaigns yet.") is proven after an explicit Reload instead.
//
// Fields are found by role and accessible name, never getByLabel exact: the
// panel wraps each control in its <label>, so the label's text also carries the
// textarea's typed value and the select's option text and stops matching.

const API_HOST = 'leaf-proof.invalid'
const LOCAL_HOSTS = new Set(['127.0.0.1', API_HOST])
const CORS = {
  'access-control-allow-origin': '*',
  'access-control-allow-headers': '*',
  'access-control-allow-methods': '*',
}
const TOKEN = 'campaign-proof-token'
const TENANT = 'cat-litmus-tenant'
const ORG_ID = 'cat-proof-org'

// The campaign API refuses any id that is not UUID shaped before sending.
const PROJECT_ID = '5a1f0c3e-2b7d-4c8a-9e61-0d4f7b2a9c11'
const CAMPAIGN_ID = '7c2e9a4b-1d3f-4e5a-8b6c-9d0e1f2a3b4c'
const QUESTION_ID = '3e4f5a6b-7c8d-4e9f-a0b1-c2d3e4f5a6b7'
const PROJECT = { project_id: PROJECT_ID, name: 'Campaign proof roof', status: 'active' }
const WORKSPACE = { project: PROJECT, drawing_versions: [], jobs: [], built_tools: [], drawing_artifacts: [] }

const TITLE = 'Roof layout review'
const PROMPT = 'Review the roof layout and list every panel row that crosses a setback.'
const QUESTION_PROMPT = 'Which roof faces should the review cover?'
const ANSWER = 'South and west faces only.'

const BASE = `/api/campaigns/${CAMPAIGN_ID}`
const LIST_READ = ['/api/campaigns']
const SELECTED_READS = [
  '/api/campaigns', BASE, `${BASE}/questions`, `${BASE}/enrollments`, `${BASE}/capabilities`, `${BASE}/execution`,
]
const SUBMIT = 'POST /api/campaigns'
const ANSWER_WRITE = `POST ${BASE}/questions/${QUESTION_ID}/answer`

const QUOTA_BODY = {
  error: { error_code: 'quota_exceeded', message: 'Tenant in-flight run limit reached.', retryable: true },
  quota_kind: 'tenant_inflight',
  limit: 2,
  used: 2,
}
const QUOTA_MESSAGE = 'Run limit reached: 2 runs are already queued or running. This one will not be sent; try again when one finishes.'
const UNAVAILABLE_BODY = { error: { error_code: 'campaigns_unavailable', retryable: true } }
const UNAVAILABLE_MESSAGE = 'Campaigns are unavailable right now; retry in a moment.'

const json = (body, status = 200) => ({ status, body })

// The fake campaign service. Returns null for a path it does not own so the
// shared cat proof fixture answers the rest of the app's boot traffic.
function campaignReply(backend, method, path, body) {
  if (method === 'GET' && path === '/api/projects') return json({ projects: [PROJECT] })
  if (method === 'GET' && path === `/api/projects/${PROJECT_ID}`) return json(WORKSPACE)
  if (path.startsWith('/api/projects')) return null
  if (method === 'GET' && path === '/api/campaigns') {
    return backend.listUnavailable ? json(UNAVAILABLE_BODY, 503) : json({ campaigns: backend.campaigns.map((row) => ({ ...row })) })
  }
  if (method === 'POST' && path === '/api/campaigns') {
    const outcome = backend.submitReplies.shift() || 'recorded'
    if (outcome === 'quota') return json(QUOTA_BODY, 429)
    if (outcome === 'unavailable') return json(UNAVAILABLE_BODY, 503)
    const campaign = {
      campaign_id: CAMPAIGN_ID, project_id: PROJECT_ID, title: body?.title, prompt: body?.prompt,
      status: 'accepted', dispatch: { available: false }, created_at: '2026-09-30T12:00:00Z',
    }
    backend.campaigns = [campaign]
    backend.questions = backend.withQuestion
      ? [{ question_id: QUESTION_ID, prompt: QUESTION_PROMPT, status: 'open', answer: null }]
      : []
    return json({ campaign, replayed: false }, 201)
  }
  const campaign = backend.campaigns[0]
  if (campaign && method === 'GET' && path === BASE) return json({ campaign, completion: null })
  if (campaign && method === 'GET' && path === `${BASE}/questions`) {
    return json({ questions: backend.questions.map((question) => ({ ...question })) })
  }
  if (campaign && method === 'GET' && path === `${BASE}/enrollments`) {
    return json({ enrollment: { enrollments: [], allowed_machines: [] } })
  }
  if (campaign && method === 'GET' && path === `${BASE}/capabilities`) return json({ capabilities: [] })
  if (campaign && method === 'GET' && path === `${BASE}/execution`) {
    return json({ execution: { tasks: [], receipts: [], events: [] }, completion: null })
  }
  if (campaign && method === 'POST' && path === `${BASE}/questions/${QUESTION_ID}/answer`) {
    const question = backend.questions.find((row) => row.question_id === QUESTION_ID)
    if (!question || question.status !== 'open') return json({ error: { error_code: 'answer_conflict' } }, 409)
    Object.assign(question, { status: 'answered', answer: body?.answer })
    return json({ question_id: QUESTION_ID, answer: body?.answer, question: { ...question } })
  }
  return json({ error: { error_code: 'not_found', message: `unhandled campaign proof route ${method} ${path}` } }, 404)
}

async function installBackend(page) {
  const proofState = makeCatProofState()
  const backend = {
    calls: [],
    foreign: [],
    campaigns: [],
    questions: [],
    submitReplies: [],
    listUnavailable: false,
    withQuestion: false,
    writes() {
      return this.calls.filter((call) => call.method !== 'GET').map((call) => `${call.method} ${call.path}`)
    },
    count(method, path) {
      return this.calls.filter((call) => call.method === method && call.path === path).length
    },
    sent(write) {
      return this.calls.filter((call) => `${call.method} ${call.path}` === write)
    },
  }
  await page.addInitScript(({ token, org }) => {
    localStorage.setItem('leaf.jwt', token)
    localStorage.setItem('leaf.org_id', org)
  }, { token: TOKEN, org: ORG_ID })
  // Nothing may leave the dev server or the proof API.
  await page.route((url) => /^https?:$/.test(url.protocol) && !LOCAL_HOSTS.has(url.hostname), (route) => {
    backend.foreign.push(route.request().url())
    return route.abort()
  })
  await page.route((url) => url.hostname === API_HOST, async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    if (method === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
    let body = null
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = request.postData() }
    }
    const query = Object.fromEntries(url.searchParams)
    const watched = url.pathname.startsWith('/api/campaigns') || url.pathname.startsWith('/api/projects')
    if (watched) backend.calls.push({ method, path: url.pathname, query, body, headers: await request.allHeaders() })
    const reply = (watched && campaignReply(backend, method, url.pathname, body))
      || catProofResponse({ method, path: url.pathname, body: body || {}, query }, proofState)
    await route.fulfill({
      status: reply.status,
      contentType: 'application/json',
      body: reply.body == null ? '' : JSON.stringify(reply.body),
      headers: CORS,
    })
  })
  return backend
}

async function openCampaignPanel(page, backend) {
  await page.goto('/app?surface=browser')
  const board = page.locator('[data-ground="browser"]')
  await expect(board).toBeVisible({ timeout: 30_000 })
  const start = board.getByRole('region', { name: 'Workspace projects', exact: true })
  await start.getByRole('button', { name: PROJECT.name, exact: true }).click({ timeout: 30_000 })
  await expect(board).toHaveAttribute('data-project-state', 'project')
  await expect.poll(() => backend.count('GET', `/api/projects/${PROJECT_ID}`)).toBeGreaterThan(0)
  const panel = board.getByRole('region', { name: 'Campaign', exact: true })
  await expect(panel).toBeVisible()
  await expect(panel.getByRole('heading', { name: 'Submit a campaign', exact: true })).toBeVisible()
  await expect.poll(() => backend.count('GET', '/api/campaigns')).toBeGreaterThan(0)
  return panel
}

async function fillDraft(panel) {
  await panel.getByRole('textbox', { name: 'Title', exact: true }).fill(TITLE)
  await panel.getByRole('textbox', { name: 'Prompt', exact: true }).fill(PROMPT)
}

// Exact writes, reads only from the allowed set, every campaign call scoped to
// the open project with the session bearer, and no request off the host.
function expectTraffic(backend, { writes, reads }) {
  expect(backend.writes()).toEqual(writes)
  const campaignCalls = backend.calls.filter((call) => call.path.startsWith('/api/campaigns'))
  for (const call of campaignCalls) {
    if (call.method === 'GET') {
      expect(reads, `unexpected campaign read ${call.path}`).toContain(call.path)
      expect(call.query.project_id).toBe(PROJECT_ID)
    }
    expect(call.headers.authorization).toBe(`Bearer ${TOKEN}`)
    expect(call.headers['x-tenant-id']).toBe(TENANT)
  }
  expect(backend.foreign).toEqual([])
}

function expectSubmitted(call) {
  expect(call.body).toEqual({ project_id: PROJECT_ID, title: TITLE, prompt: PROMPT })
  expect(call.headers['idempotency-key']).toMatch(/^\S{1,128}$/)
}

async function expectCampaignShown(panel) {
  const status = panel.locator('.campaign-status')
  await expect(status).toHaveAttribute('data-state', 'accepted')
  await expect(status.getByRole('heading', { name: TITLE, exact: true })).toBeVisible()
  await expect(status.getByText('Accepted, not running', { exact: true })).toBeVisible()
  await expect(status.getByText('The build fleet is not connected yet.', { exact: true })).toBeVisible()
  const nav = panel.getByRole('navigation', { name: 'Project releases and campaigns', exact: true })
  await expect(nav.getByRole('button', { name: TITLE, exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect(panel.getByRole('region', { name: 'Project delivery', exact: true })).toContainText('Output unavailable.')
}

test.describe('campaign panel states', () => {
  test.describe.configure({ timeout: 120_000 })

  test('empty state renders only the submit form and sends nothing', async ({ page }) => {
    const backend = await installBackend(page)
    const panel = await openCampaignPanel(page, backend)

    await expect(panel.getByRole('heading', { name: 'Project results', exact: true })).toBeVisible()
    await expect(panel.getByRole('navigation', { name: 'Project releases and campaigns', exact: true })).toHaveCount(0)
    await expect(panel.locator('.campaign-status')).toHaveCount(0)
    await expect(panel.getByRole('region', { name: 'Project delivery', exact: true })).toHaveCount(0)
    await expect(panel.getByRole('alert')).toHaveCount(0)
    await expect(panel.getByRole('textbox', { name: 'Title', exact: true })).toHaveValue('')
    await expect(panel.getByRole('textbox', { name: 'Prompt', exact: true })).toHaveValue('')
    await expect(panel.getByText('32768 characters remaining', { exact: true })).toBeVisible()
    await expect(panel.getByRole('button', { name: 'Submit campaign', exact: true })).toBeEnabled()

    // The header's Finish this project switches the form locally and sends nothing.
    await panel.getByRole('button', { name: 'Finish this project', exact: true }).click()
    await expect(panel.getByRole('textbox', { name: 'Title', exact: true })).toBeFocused()
    await expect(panel.getByRole('combobox', { name: 'Delivery profile', exact: true })).toBeVisible()
    await expect(panel.getByText('2000 characters remaining', { exact: true })).toBeVisible()
    await expect(panel.getByRole('button', { name: 'Request release', exact: true })).toBeEnabled()

    expectTraffic(backend, { writes: [], reads: LIST_READ })
  })

  test('draft creation records one campaign and selects it', async ({ page }) => {
    const backend = await installBackend(page)
    const panel = await openCampaignPanel(page, backend)

    await fillDraft(panel)
    await panel.getByRole('button', { name: 'Submit campaign', exact: true }).click()
    await expectCampaignShown(panel)
    await expect(panel.getByRole('alert')).toHaveCount(0)
    // The new-request form moves behind its disclosure once a campaign exists.
    await expect(panel.locator('details', { has: page.locator('summary', { hasText: 'Start a new request' }) })).toHaveCount(1)

    expectTraffic(backend, { writes: [SUBMIT], reads: SELECTED_READS })
    expectSubmitted(backend.sent(SUBMIT)[0])
  })

  test('question answering records one answer and moves it to the history', async ({ page }) => {
    const backend = await installBackend(page)
    backend.withQuestion = true
    const panel = await openCampaignPanel(page, backend)

    await fillDraft(panel)
    await panel.getByRole('button', { name: 'Submit campaign', exact: true }).click()
    await expectCampaignShown(panel)
    const open = panel.locator('.campaign-questions li[data-state="open"]')
    await expect(open).toHaveCount(1)
    await expect(open).toContainText(QUESTION_PROMPT)
    await expect(panel.getByRole('heading', { name: 'Questions', exact: true })).toBeVisible()
    await expect(open.getByRole('button', { name: 'Record answer', exact: true })).toBeEnabled()
    expect(backend.writes()).toEqual([SUBMIT])

    await open.getByRole('textbox', { name: 'Answer', exact: true }).fill(ANSWER)
    await open.getByRole('button', { name: 'Record answer', exact: true }).click()
    await expect(panel.locator('.campaign-questions li[data-state="open"]')).toHaveCount(0)
    await expect(panel.getByRole('button', { name: 'Record answer', exact: true })).toHaveCount(0)
    const history = panel.locator('details', { has: page.locator('summary', { hasText: 'Answered question history' }) })
    await history.locator('summary').click()
    const answered = history.locator('li[data-state="answered"]')
    await expect(answered).toHaveCount(1)
    await expect(answered).toContainText(QUESTION_PROMPT)
    await expect(answered.locator('.campaign-answer')).toHaveText(ANSWER)
    await expect(panel.getByRole('alert')).toHaveCount(0)

    expectTraffic(backend, { writes: [SUBMIT, ANSWER_WRITE], reads: SELECTED_READS })
    expect(backend.sent(ANSWER_WRITE)[0].body).toEqual({ project_id: PROJECT_ID, answer: ANSWER })
  })

  test('quota refusal keeps the draft, never resends on its own, and Try again resends the same submission', async ({ page }) => {
    const backend = await installBackend(page)
    backend.submitReplies = ['quota', 'recorded']
    const panel = await openCampaignPanel(page, backend)

    await fillDraft(panel)
    await panel.getByRole('button', { name: 'Submit campaign', exact: true }).click()
    const refusal = panel.locator('.campaign-error[data-state="quota"]')
    await expect(refusal).toBeVisible()
    await expect(refusal).toHaveAttribute('role', 'alert')
    await expect(refusal.locator('p')).toHaveText(QUOTA_MESSAGE)
    await expect(refusal).toBeFocused()
    await expect(refusal.getByRole('button', { name: 'Try again', exact: true })).toBeVisible()
    await expect(refusal.getByRole('button', { name: 'Reload', exact: true })).toBeVisible()
    await expect(panel.getByRole('textbox', { name: 'Title', exact: true })).toHaveValue(TITLE)
    await expect(panel.getByRole('textbox', { name: 'Prompt', exact: true })).toHaveValue(PROMPT)
    await expect(panel.locator('.campaign-status')).toHaveCount(0)
    // A refusal is never retried automatically.
    await page.waitForTimeout(750)
    expect(backend.writes()).toEqual([SUBMIT])

    // Reload reads the list again and reaches the named empty status; still one write.
    await refusal.getByRole('button', { name: 'Reload', exact: true }).click()
    await expect(refusal.getByRole('status')).toHaveText('Campaigns reloaded.')
    await expect(panel.getByText('No campaigns yet.', { exact: true })).toBeVisible()
    expect(backend.writes()).toEqual([SUBMIT])

    // Only the explicit Try again sends it again, under the same submission key.
    await refusal.getByRole('button', { name: 'Try again', exact: true }).click()
    await expectCampaignShown(panel)
    await expect(panel.locator('.campaign-error')).toHaveCount(0)
    await expect(panel.getByText('No campaigns yet.', { exact: true })).toHaveCount(0)

    expectTraffic(backend, { writes: [SUBMIT, SUBMIT], reads: SELECTED_READS })
    const [first, second] = backend.sent(SUBMIT)
    expectSubmitted(first)
    expectSubmitted(second)
    expect(second.headers['idempotency-key']).toBe(first.headers['idempotency-key'])
  })

  test('service unavailability is named, sends nothing more, and recovers through Try again', async ({ page }) => {
    const backend = await installBackend(page)
    backend.submitReplies = ['unavailable']
    const panel = await openCampaignPanel(page, backend)

    await fillDraft(panel)
    await panel.getByRole('button', { name: 'Submit campaign', exact: true }).click()
    const refusal = panel.locator('form .campaign-error')
    await expect(refusal).toBeVisible()
    await expect(refusal).toHaveAttribute('role', 'alert')
    expect(await refusal.getAttribute('data-state')).toBeNull()
    await expect(refusal.locator('p')).toHaveText(UNAVAILABLE_MESSAGE)
    await expect(refusal.getByRole('button', { name: 'Try again', exact: true })).toHaveCount(0)
    await expect(refusal.getByRole('button', { name: 'Reload', exact: true })).toBeVisible()
    await expect(panel.getByRole('textbox', { name: 'Title', exact: true })).toHaveValue(TITLE)
    await expect(panel.getByRole('textbox', { name: 'Prompt', exact: true })).toHaveValue(PROMPT)
    expect(backend.writes()).toEqual([SUBMIT])

    // The list read fails too: the panel names the outage with its own Try again.
    backend.listUnavailable = true
    const listReads = backend.count('GET', '/api/campaigns')
    await refusal.getByRole('button', { name: 'Reload', exact: true }).click()
    const stale = panel.locator('.project-lifecycle-stale .campaign-error')
    await expect(stale).toBeVisible()
    await expect(stale).toHaveAttribute('role', 'alert')
    await expect(stale.locator('p')).toHaveText(UNAVAILABLE_MESSAGE)
    expect(backend.count('GET', '/api/campaigns')).toBe(listReads + 1)

    // Service back: Try again reloads the list into the empty status, still one write.
    backend.listUnavailable = false
    await stale.getByRole('button', { name: 'Try again', exact: true }).click()
    await expect(panel.locator('.project-lifecycle-stale')).toHaveCount(0)
    await expect(panel.getByText('No campaigns yet.', { exact: true })).toBeVisible()
    await expect(panel.locator('.campaign-status')).toHaveCount(0)
    expect(backend.count('GET', '/api/campaigns')).toBe(listReads + 2)

    expectTraffic(backend, { writes: [SUBMIT], reads: LIST_READ })
    expectSubmitted(backend.sent(SUBMIT)[0])
  })
})
