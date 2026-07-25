import { catProofResponse, makeCatProofState } from '../catProofFixture.mjs'

export const NEGATIVE_TOOL = {
  name: 'count-by-layer',
  version: '1.0.0',
  description: 'Counts entities per layer without changing the drawing.',
  kind: 'script',
  engine_op: 'count_by_layer',
  params: { type: 'object', properties: {}, required: [] },
  returns: { type: 'object' },
  capabilities: ['drawing.read'],
  provenance: { author: 'agent', created: '2026-07-24T00:00:00Z' },
}

const cors = {
  'access-control-allow-origin': '*',
  'access-control-allow-headers': '*',
}

const json = (body, status = 200) => ({ body, status })

export async function installNegativeApi(page, { approval = null, run = null } = {}) {
  const state = makeCatProofState()
  const evidence = {
    calls: [],
    runSubmissions: 0,
    jobDetailReads: 0,
    versionMutations: 0,
    returnedJobIds: [],
  }

  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const path = url.pathname
    const body = request.postData() ? request.postDataJSON() : {}
    evidence.calls.push(`${method} ${path}`)

    if (method === 'POST' && path === '/api/run') evidence.runSubmissions += 1
    if (method === 'GET' && /^\/api\/jobs\/[^/]+$/.test(path)) evidence.jobDetailReads += 1
    if (method !== 'GET' && /^\/api\/drawings\/[^/]+\/(undo|redo|versions)$/.test(path)) {
      evidence.versionMutations += 1
    }

    let result
    if (path === '/api/tools') {
      result = json({ tools: [NEGATIVE_TOOL] })
    } else if (path === '/api/capabilities') {
      result = json({
        families: [{
          family_id: 'measurement',
          label: 'Measurement',
          description: 'Read-only drawing checks.',
          capabilities: [{ ...NEGATIVE_TOOL, params_schema: NEGATIVE_TOOL.params }],
        }],
      })
    } else if (path === '/api/entitlements') {
      // The catalog is initially usable. Run-denial tests exercise the
      // authoritative server revalidation at submission time.
      result = json({
        tier: 'proof',
        entitlements: { run_read: true, run_write: true, build: true, converse: true },
      })
    } else if (path === '/api/usage' && run?.usage) {
      result = json(run.usage)
    } else if (method === 'POST' && path === '/api/run' && run) {
      result = json(run.body, run.status)
    } else if (method === 'POST' && path === '/api/agent/approvals/cat-confirmation' && approval) {
      if (approval === 'stale') {
        result = json({ error: { error_code: 'bad_params', message: 'approval is no longer resolvable' } }, 409)
      } else {
        result = json({ resolved: true, approved: approval !== 'denied' })
      }
    } else if (method === 'POST' && path === '/api/sessions/cat-session/messages' && body.confirm && approval) {
      if (approval === 'stale') {
        result = json({ error: { error_code: 'bad_params', message: 'approval is no longer resolvable' } }, 409)
      } else if (approval === 'expired') {
        result = json({ error: { error_code: 'bad_params', message: 'confirmation expired' } }, 410)
      } else {
        state.events.push(
          { v: 1, session_id: 'cat-session', turn_id: 'turn-2', seq: 5, type: 'turn_started', data: {} },
          {
            v: 1,
            session_id: 'cat-session',
            turn_id: 'turn-2',
            seq: 6,
            type: 'confirmation_resolved',
            data: { confirmation_id: 'cat-confirmation', approved: false, by: 'operator' },
          },
          {
            v: 1,
            session_id: 'cat-session',
            turn_id: 'turn-2',
            seq: 7,
            type: 'text_delta',
            data: { text: 'Denied. The drawing remains unchanged.' },
          },
          {
            v: 1,
            session_id: 'cat-session',
            turn_id: 'turn-2',
            seq: 8,
            type: 'turn_complete',
            data: { stop_reason: 'end_turn' },
          },
        )
        result = json({ turn_id: 'turn-2', status: 'started' }, 202)
      }
    } else {
      result = catProofResponse({ method, path, body, query: Object.fromEntries(url.searchParams) }, state)
    }

    if (result.body?.job_id) evidence.returnedJobIds.push(result.body.job_id)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: cors,
    })
  })

  return { evidence, state }
}

export async function openApp(page) {
  await page.goto('/app')
  await page.getByRole('combobox', { name: 'Command bar' }).waitFor()
}

export async function proposeCat(page) {
  const request = 'Rearrange the existing panels in this drawing into the shape of a sitting cat.'
  await page.getByRole('combobox', { name: 'Command bar' }).fill(request)
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  const card = page.locator('.converse-confirm').filter({ hasText: 'arrange-panels-as-cat' })
  await card.waitFor()
  return card
}

export async function submitReadRun(page) {
  await page.getByRole('combobox', { name: 'Command bar' }).fill('/count-by-layer')
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  await page.getByRole('button', { name: 'Run count-by-layer' }).click()
}

export function expectNoCreatedWork(expect, evidence, state) {
  expect(evidence.returnedJobIds, 'no denial response may mint a job id').toEqual([])
  expect(evidence.jobDetailReads, 'the client must not attach to a denied job').toBe(0)
  expect(evidence.versionMutations, 'the client must not mutate drawing versions').toBe(0)
  expect(state.head, 'the fixture drawing head must remain unchanged').toBe(1)
}
