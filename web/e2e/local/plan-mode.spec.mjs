import { expect, test } from '@playwright/test'
import { requireLocalReady } from './requireReady.mjs'

// SDS-PMW: a short pre-merge walk of Studio plan mode on the hermetic local stack.
// LEAF_AGENT_MOCK=1 mounts the scripted harness turn runner, whose plan flow asks one
// question, proposes one plan and resumes on approval. The managed stack keeps the
// plan rollout flag off, so this walk (a) reads the real inline-authoring flags and
// flips only plan_mode in the browser, which shows the Plan pill, and (b) carries the
// scripted runner's "planwalk" marker in the prompt. Every request after that is real:
// sessions, answers, the approval record and the resume turn. The walk runs in the
// Studio shell (/app), the one surface that mounts the Plan pill; its default drawing
// is the curated demo that every fresh stack serves.

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const REQUEST = 'planwalk: a tool that tallies the entities on each layer'

test('plan mode asks, proposes a plan and builds on approval', async ({ page, request }) => {
  await requireLocalReady(request, test, API_BASE)

  await page.route('**/api/author/inline-authoring', async (route) => {
    const response = await route.fetch()
    const flags = await response.json()
    await route.fulfill({ response, json: { ...flags, plan_mode: true } })
  })

  const observed = []
  page.on('response', (response) => {
    if (!response.url().startsWith(API_BASE)) return
    const url = new URL(response.url())
    observed.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
  })

  await page.goto('/app?surface=cad')
  await expect(page.locator('.app[data-surface="cad"]')).toHaveCount(1, { timeout: 30_000 })
  // WorkspaceEnvironment.jsx seats a starting chooser over the shell; enter the workspace.
  const environment = page.locator('section.workspace-environment')
  if (await environment.count() && await environment.getAttribute('data-entered') !== 'true') {
    const resume = environment.getByRole('button', { name: 'Continue workspace', exact: true })
    if (await resume.isVisible()) await resume.click()
    else await environment.getByRole('button', { name: 'Describe what to create', exact: true }).click()
    await expect(environment).toHaveAttribute('data-entered', 'true')
  }

  const pill = page.locator('button[aria-pressed][title^="Plan first"]')
  await expect(pill).toBeVisible({ timeout: 30_000 })
  await expect(pill).toHaveAttribute('aria-pressed', 'false')
  await pill.click()
  await expect(pill).toHaveAttribute('aria-pressed', 'true')

  const bar = page.getByLabel('Command bar', { exact: true })
  await bar.fill(REQUEST)
  await bar.press('Enter')

  const questions = page.locator('section[aria-label="Planning questions"]').first()
  await expect(questions).toBeVisible({ timeout: 15_000 })
  await expect(questions).toContainText('Output')
  await expect(questions).toContainText('How should the layer tally be reported?')
  await questions.getByRole('button', { name: /^Table/ }).click()
  await expect(questions.getByRole('button', { name: /^Table/ })).toHaveAttribute('aria-pressed', 'true')
  await expect(questions.getByRole('button', { name: /^Summary/ })).toHaveAttribute('aria-pressed', 'false')
  const submit = questions.getByRole('button', { name: 'Submit', exact: true })
  await expect(submit).toBeEnabled({ timeout: 15_000 })
  await submit.click()

  const plan = page.locator('section[aria-label="Build plan"]').first()
  await expect(plan).toBeVisible({ timeout: 15_000 })
  await expect(plan).toContainText('Plan · Revision 1')
  await expect(plan.getByRole('heading', { name: 'Layer tally' })).toBeVisible()
  await expect(plan).toContainText('New tool layer-tally')
  const inputs = plan.getByRole('table', { name: 'Inputs' })
  await expect(inputs).toContainText('layer_filter')
  await expect(inputs).toContainText('string')
  await expect(plan).toContainText('Table')
  await expect(page.getByText('Here is the plan. Approve it to build the tool.').first()).toBeVisible()
  const approve = plan.getByRole('button', { name: 'Approve and build', exact: true })
  await expect(approve).toBeEnabled({ timeout: 15_000 })
  await expect(plan.getByRole('button', { name: 'Keep planning', exact: true })).toBeVisible()

  await approve.click()
  await expect(plan.getByRole('status')).toHaveText('Approved', { timeout: 15_000 })
  await expect(page.getByText('Plan approved. Building layer-tally now.').first()).toBeVisible({ timeout: 15_000 })

  expect(observed.filter((entry) => /^POST \/api\/agent\/approvals\/plan-[^/]+ 200$/.test(entry))).toHaveLength(1)
  expect(observed.filter((entry) => /^POST \/api\/sessions\/[^/]+\/messages 202$/.test(entry))).toHaveLength(3)
  expect(observed.filter((entry) => entry.startsWith('POST /api/run '))).toHaveLength(0)
})
