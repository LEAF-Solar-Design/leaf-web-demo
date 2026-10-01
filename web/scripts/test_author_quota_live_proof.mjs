import assert from 'node:assert/strict'
import { afterEach, beforeEach, describe, it } from 'node:test'

import {
  MAX_REQUEST_CEILING,
  ProofError,
  TOKEN_ENV,
  classifyStageResponse,
  createFetchBackend,
  createRequestBudget,
  isProductionHost,
  main,
  parseArgs,
  resolveConfig,
  runExecution,
  summarizeQuotaRefusal,
} from './author_quota_live_proof.mjs'

const TOKEN = 'proof.token.value-0123456789'
const ORIGINAL_FETCH = globalThis.fetch

function environment(overrides = {}) {
  return {
    LEAF_QUOTA_PROOF_WEB_URL: 'https://staging.leaf.test',
    LEAF_QUOTA_PROOF_API_URL: 'https://staging-api.leaf.test',
    LEAF_QUOTA_PROOF_ALLOWED_HOSTS: 'staging.leaf.test,staging-api.leaf.test',
    LEAF_QUOTA_PROOF_RUN_ID: 'quota-run-1',
    [TOKEN_ENV]: TOKEN,
    ...overrides,
  }
}

// The exact wire shape server/routers/author.py returns for the daily cap
// (same envelope web/e2e/author-quota-gate.spec.mjs fulfills by hand).
function quotaEnvelope(tier, limit, used) {
  const message = `Daily authoring limit reached for your plan (${used}/${limit}). Resets 00:00 UTC. Upgrade for more.`
  return {
    ok: false,
    error: { error_code: 'quota_exceeded', message, retryable: true, tier, limit, used, quota_kind: 'daily_author' },
    error_code: 'quota_exceeded',
    retryable: true,
    message,
    tier,
    limit,
    used,
    quota_kind: 'daily_author',
    degraded_mode: false,
  }
}

// A fake staging API holding a per-tenant daily cap. Counts every request.
function fakeQuotaBackend({ cap = 1, failWith = null } = {}) {
  const calls = []
  let used = 0
  return {
    calls,
    async stage(request) {
      calls.push({ idempotencyKey: request.idempotencyKey, token: request.readToken() })
      if (failWith) return failWith
      if (used < cap) {
        used += 1
        return { status: 202, body: { change_set_id: `cs-${used}`, poll_url: `/api/author/stages/cs-${used}` } }
      }
      return { status: 429, body: quotaEnvelope('hosted_starter', cap, used) }
    },
  }
}

// A fake browser that sends its one authoring request through the shared budget
// to the same fake backend and renders what AuthorPanel's QuotaGate renders.
function fakeBrowser(backend, { render } = {}) {
  const calls = []
  return {
    calls,
    async captureQuotaGate(opts) {
      calls.push({ screenshotPath: opts.screenshotPath, webOrigin: opts.webOrigin })
      opts.takeRequest()
      const response = await backend.stage({
        apiOrigin: opts.apiOrigin,
        description: opts.description,
        idempotencyKey: 'browser',
        readToken: opts.readToken,
      })
      const body = response.body || {}
      const capture = {
        stageStatus: response.status,
        stageAttempts: 1,
        gateVisible: response.status === 429,
        gateText: response.status === 429
          ? `You have used your tool authoring for today (${body.used}/${body.limit}). Your plan includes a daily limit on authoring new tools. It resets at 00:00 UTC, so upgrade your plan for more.`
          : '',
        inlineErrorCount: 0,
        authoredCount: 0,
        blockedOrigins: [],
        screenshot: opts.screenshotPath,
      }
      return render ? render(capture) : capture
    },
  }
}

// Any network touch in these tests is a defect: fetch throws and is counted.
let fetchCalls = 0
beforeEach(() => {
  fetchCalls = 0
  globalThis.fetch = async () => {
    fetchCalls += 1
    throw new Error('network is forbidden in this test')
  }
})
afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH
})

function collect() {
  const out = []
  const err = []
  return { out, err, stdout: (line) => out.push(line), stderr: (line) => err.push(line) }
}

function configFor(overrides = {}, args = { maxRequests: '10' }) {
  const { config, refusals } = resolveConfig(environment(overrides), args)
  assert.deepEqual(refusals, [])
  return config
}

describe('dry run (the default)', () => {
  it('makes zero requests and launches nothing with a full staging config', async () => {
    const backend = fakeQuotaBackend()
    const browser = fakeBrowser(backend)
    const io = collect()
    const code = await main(['--max-requests', '5'], environment(), { ...io, backend, browser })
    assert.equal(code, 0)
    assert.equal(backend.calls.length, 0)
    assert.equal(browser.calls.length, 0)
    assert.equal(fetchCalls, 0)
    const plan = JSON.parse(io.out[0])
    assert.equal(plan.mode, 'dry-run')
    assert.equal(plan.requests_made, 0)
    assert.equal(plan.execution_ready, true)
    assert.deepEqual(plan.refusals, [])
    assert.equal(plan.token_env, TOKEN_ENV)
    assert.equal(plan.token_present, true)
    assert.ok(!io.out.join('\n').includes(TOKEN), 'the token value never reaches output')
  })

  it('makes zero requests with no configuration and lists every refusal', async () => {
    const backend = fakeQuotaBackend()
    const browser = fakeBrowser(backend)
    const io = collect()
    const code = await main([], {}, { ...io, backend, browser })
    assert.equal(code, 0)
    assert.equal(backend.calls.length, 0)
    assert.equal(browser.calls.length, 0)
    assert.equal(fetchCalls, 0)
    const plan = JSON.parse(io.out[0])
    assert.equal(plan.execution_ready, false)
    assert.ok(plan.refusals.some((r) => r.includes('LEAF_QUOTA_PROOF_WEB_URL')))
    assert.ok(plan.refusals.some((r) => r.includes('--max-requests')))
    assert.ok(plan.refusals.some((r) => r.includes(TOKEN_ENV)))
  })

  it('stays a dry run against a production host: reports, never requests', async () => {
    const backend = fakeQuotaBackend()
    const io = collect()
    const env = environment({
      LEAF_QUOTA_PROOF_API_URL: 'https://platform.leafdesign.ai',
      LEAF_QUOTA_PROOF_ALLOWED_HOSTS: 'platform.leafdesign.ai,staging.leaf.test',
    })
    assert.equal(await main([], env, { ...io, backend, browser: fakeBrowser(backend) }), 0)
    assert.equal(backend.calls.length, 0)
    const plan = JSON.parse(io.out[0])
    assert.equal(plan.execution_ready, false)
    assert.ok(plan.refusals.some((r) => r.includes('production')))
  })
})

describe('execution opt-in', () => {
  const refusedCases = [
    ['no request cap', [], environment()],
    ['a cap below the minimum', ['--max-requests', '1'], environment()],
    ['a cap above the ceiling', ['--max-requests', String(MAX_REQUEST_CEILING + 1)], environment()],
    ['a production api host even when allowlisted', ['--max-requests', '5'], environment({
      LEAF_QUOTA_PROOF_API_URL: 'https://api.leafdesign.ai',
      LEAF_QUOTA_PROOF_ALLOWED_HOSTS: 'staging.leaf.test,api.leafdesign.ai',
    })],
    ['a host off the allowlist', ['--max-requests', '5'], environment({
      LEAF_QUOTA_PROOF_ALLOWED_HOSTS: 'staging.leaf.test',
    })],
    ['a non-staging host', ['--max-requests', '5'], environment({
      LEAF_QUOTA_PROOF_API_URL: 'https://api.leaf.test',
      LEAF_QUOTA_PROOF_ALLOWED_HOSTS: 'staging.leaf.test,api.leaf.test',
    })],
    ['plain http', ['--max-requests', '5'], environment({ LEAF_QUOTA_PROOF_API_URL: 'http://staging-api.leaf.test' })],
    ['a missing token', ['--max-requests', '5'], environment({ [TOKEN_ENV]: '' })],
  ]
  for (const [name, args, env] of refusedCases) {
    it(`refuses ${name} before any request`, async () => {
      const backend = fakeQuotaBackend()
      const browser = fakeBrowser(backend)
      const io = collect()
      const code = await main(['--execute', ...args], env, { ...io, backend, browser })
      assert.equal(code, 2)
      assert.equal(backend.calls.length, 0)
      assert.equal(browser.calls.length, 0)
      assert.equal(fetchCalls, 0)
      assert.equal(JSON.parse(io.err[0]).code, 'configuration')
    })
  }

  it('rejects unknown arguments', () => {
    assert.throws(() => parseArgs(['--prod']), (e) => e instanceof ProofError && e.code === 'usage')
    assert.throws(() => parseArgs(['--execute=yes']), (e) => e.code === 'usage')
  })

  it('names production hosts by list and by shape', () => {
    assert.equal(isProductionHost('platform.leafdesign.ai'), true)
    assert.equal(isProductionHost('anything.leafdesign.ai'), true)
    assert.equal(isProductionHost('staging.leafdesign.ai'), false)
  })
})

describe('fake cap-one execution', () => {
  it('stops at the first quota refusal and captures the QuotaGate', async () => {
    const backend = fakeQuotaBackend({ cap: 1 })
    const browser = fakeBrowser(backend)
    const config = configFor()
    const receipt = await runExecution(config, { backend, browser })

    // One spend, one refusal, then no further API request despite budget left.
    assert.deepEqual(receipt.attempts, [
      { attempt: 1, status: 202, outcome: 'accepted' },
      { attempt: 2, status: 429, outcome: 'quota_refusal' },
    ])
    assert.equal(receipt.stopped_at_attempt, 2)
    assert.deepEqual(receipt.request_sources, ['api', 'api', 'browser'])
    assert.equal(receipt.requests_made, 3)
    assert.equal(backend.calls.length, 3)
    assert.equal(browser.calls.length, 1)
    assert.equal(fetchCalls, 0)

    assert.equal(receipt.quota_refusal.error_code, 'quota_exceeded')
    assert.equal(receipt.quota_refusal.quota_kind, 'daily_author')
    assert.equal(receipt.quota_refusal.limit, 1)
    assert.equal(receipt.quota_refusal.used, 1)

    assert.equal(receipt.quota_gate.stage_status, 429)
    assert.equal(receipt.quota_gate.visible, true)
    assert.ok(receipt.quota_gate.text.includes('(1/1)'))
    assert.ok(receipt.quota_gate.text.includes('daily limit on authoring new tools'))
    assert.ok(receipt.quota_gate.text.includes('resets at 00:00 UTC'))
    assert.match(receipt.quota_gate.screenshot, /quota-gate\.png$/)

    // The token came from the env var by name and never lands in the receipt.
    assert.ok(backend.calls.every((c) => c.token === TOKEN))
    assert.ok(!JSON.stringify(receipt).includes(TOKEN))
    assert.ok(!JSON.stringify(config).includes(TOKEN))
  })

  it('fails closed when the gate renders as a red failure', async () => {
    const backend = fakeQuotaBackend({ cap: 1 })
    const browser = fakeBrowser(backend, {
      render: (c) => ({ ...c, gateVisible: false, gateText: '', inlineErrorCount: 1 }),
    })
    await assert.rejects(runExecution(configFor(), { backend, browser }), (e) => e.code === 'ui_mismatch')
  })

  it('stops without the browser when the cap sits above the request budget', async () => {
    const backend = fakeQuotaBackend({ cap: 5 })
    const browser = fakeBrowser(backend)
    await assert.rejects(
      runExecution(configFor({}, { maxRequests: '3' }), { backend, browser }),
      (e) => e.code === 'cap_not_reached',
    )
    assert.equal(backend.calls.length, 2, 'one unit stays reserved for the browser')
    assert.equal(browser.calls.length, 0)
  })

  it('stops at once on a non-quota failure', async () => {
    for (const failWith of [
      { status: 500, body: { error_code: 'internal' } },
      { status: 429, body: { error_code: 'rate_limited' } },
    ]) {
      const backend = fakeQuotaBackend({ failWith })
      const browser = fakeBrowser(backend)
      await assert.rejects(runExecution(configFor(), { backend, browser }), (e) => e.code === 'unexpected_response')
      assert.equal(backend.calls.length, 1)
      assert.equal(browser.calls.length, 0)
    }
  })

  it('scrubs the token from a failure detail', async () => {
    const backend = { async stage() { throw new ProofError('network', `boom ${TOKEN}`) } }
    const io = collect()
    const code = await main(['--execute', '--max-requests', '3'], environment(), {
      ...io, backend, browser: fakeBrowser(backend),
    })
    assert.equal(code, 1)
    assert.ok(!io.err.join('\n').includes(TOKEN))
    assert.ok(io.err[0].includes('[redacted]'))
  })
})

describe('request budget and envelope', () => {
  it('refuses a request past the cap', () => {
    const budget = createRequestBudget(2)
    budget.take('api')
    budget.take('browser')
    assert.throws(() => budget.take('browser'), (e) => e.code === 'request_cap')
    assert.equal(budget.used, 2)
  })

  it('classifies only the daily_author quota 429 as the refusal', () => {
    assert.equal(classifyStageResponse({ status: 202 }), 'accepted')
    assert.equal(classifyStageResponse({ status: 429, body: quotaEnvelope('t', 1, 1) }), 'quota_refusal')
    assert.equal(classifyStageResponse({ status: 429, body: {} }), 'unexpected')
    assert.equal(classifyStageResponse({ status: 403, body: quotaEnvelope('t', 1, 1) }), 'unexpected')
  })

  it('refuses a refusal envelope without integer counts', () => {
    assert.throws(() => summarizeQuotaRefusal({ error_code: 'quota_exceeded' }), (e) => e.code === 'quota_envelope')
  })

  it('the fetch backend sends one POST with the bearer and no retries', async () => {
    const seen = []
    const backend = createFetchBackend({
      fetchImpl: async (url, init) => {
        seen.push({ url, init })
        return { status: 429, text: async () => JSON.stringify(quotaEnvelope('t', 1, 1)) }
      },
    })
    const response = await backend.stage({
      apiOrigin: 'https://staging-api.leaf.test',
      tenantId: null,
      description: 'd',
      idempotencyKey: 'k',
      readToken: () => TOKEN,
    })
    assert.equal(seen.length, 1)
    assert.equal(seen[0].url, 'https://staging-api.leaf.test/api/author/stage')
    assert.equal(seen[0].init.method, 'POST')
    assert.equal(seen[0].init.redirect, 'manual')
    assert.equal(seen[0].init.headers.Authorization, `Bearer ${TOKEN}`)
    assert.equal(response.status, 429)
    assert.equal(response.body.quota_kind, 'daily_author')
    assert.equal(fetchCalls, 0)
  })
})
