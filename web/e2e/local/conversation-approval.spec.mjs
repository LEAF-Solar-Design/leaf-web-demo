import { expect, test } from '@playwright/test'
import { join, relative } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')
const REQUEST = 'Please approve this proposed drawing change.'

test('conversation proposal and approval resume share the unified scene', async ({ page, request }, testInfo) => {
  const readyResponse = await request.get(`${API_BASE}/api/ready`, { timeout: 3_000 })
  test.skip(!readyResponse.ok(), `real local stack is not ready at ${API_BASE}`)
  test.skip(!(await readyResponse.json())?.ready, `real local stack is not ready at ${API_BASE}`)

  const observed = []
  page.on('response', (response) => {
    if (!response.url().startsWith(API_BASE)) return
    const url = new URL(response.url())
    observed.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })

  await page.getByRole('textbox', { name: 'Command bar' }).fill(REQUEST)
  await page.getByRole('button', { name: 'Run', exact: true }).click()

  const panel = page.locator('.converse-card')
  await expect(panel).toBeVisible({ timeout: 15_000 })
  await expect(panel.getByText(REQUEST, { exact: true })).toBeVisible()
  const proposal = panel.locator('.converse-confirm').filter({ hasText: 'count_by_layer' }).first()
  await expect(proposal).toContainText('drawing.write', { timeout: 15_000 })
  await expect(proposal).toContainText('needs your approval before it changes drawing state')
  await expect(proposal.getByRole('button', { name: 'Approve' })).toBeEnabled()

  expect(observed.some((entry) => entry === 'POST /api/nl-prompt 200')).toBe(true)
  expect(observed.some((entry) => entry === 'POST /api/sessions 200')).toBe(true)
  expect(observed.some((entry) => /^POST \/api\/sessions\/[^/]+\/messages 202$/.test(entry))).toBe(true)
  expect(observed.filter((entry) => entry.startsWith('POST /api/run '))).toHaveLength(0)

  await proposal.getByRole('button', { name: 'Approve' }).click()
  await expect(proposal).toContainText('Approved', { timeout: 15_000 })
  await expect(panel).toContainText('Done')
  await expect(panel).toContainText('count_by_layer')

  const approvalPosts = observed.filter((entry) => /^POST \/api\/agent\/approvals\/[^/]+ 200$/.test(entry))
  const messagePosts = observed.filter((entry) => /^POST \/api\/sessions\/[^/]+\/messages 202$/.test(entry))
  const transcriptGets = observed.filter((entry) => /^GET \/api\/sessions\/[^/]+\/transcript 200$/.test(entry))
  expect(approvalPosts).toHaveLength(1)
  expect(messagePosts).toHaveLength(2)
  expect(transcriptGets.length).toBeGreaterThan(0)
  expect(observed.filter((entry) => entry.startsWith('POST /api/run '))).toHaveLength(0)

  writeProofReceipt(join(PROOF_DIR, 'conversation-approval-receipt.json'), {
    capability_ids: ['CV-01', 'CV-02'],
    evidence_tier: 'local-e2e',
    route: '/try',
    runtime: 'real local Vite, FastAPI, session store, SSE and transcript transport, with scripted harness turn runner',
    api_endpoints: observed,
    artifacts: [
      relative(join(process.cwd(), '..'), testInfo.outputPath('video.webm')).replaceAll('\\', '/'),
    ],
    assertions: [
      'the unified command bar routed an unmatched request into a real durable conversation session',
      'the same scene rendered the user request and server-authored drawing.write proposal',
      'approval was recorded once through the authoritative approval endpoint',
      'the approval resumed the same session through a second message turn',
      'the durable transcript reconciled the approved decision and completion text',
      'the scripted conversation path never submitted a product run',
    ],
    result: {
      verdict: 'pass',
      request: REQUEST,
      approval_count: approvalPosts.length,
      message_turn_count: messagePosts.length,
      transcript_poll_count: transcriptGets.length,
      product_run_count: 0,
    },
    limitations: [
      'LEAF_AGENT_MOCK=1 substitutes the scripted harness turn runner for Claude.',
      'The scripted approval resume proves session and approval transport, not product tool execution.',
      'APS_LIVE=0 substitutes the local drawing engine for Autodesk APS.',
    ],
  })
})
