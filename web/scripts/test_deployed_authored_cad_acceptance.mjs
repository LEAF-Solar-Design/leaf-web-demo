import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { afterEach, describe, it } from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  AcceptanceError,
  approveIsolatedStagedPublication,
  browserTenantForMode,
  buildReceipt,
  evaluateReadiness,
  evaluateDeploymentIdentity,
  main,
  isMutatingApiRequest,
  openDrawingView,
  openTryAuthorSurface,
  proveExecutedAuthorityIsolation,
  proveExecutedDrawingIsolation,
  provePinnedWriteRejections,
  provisionAcceptanceDrawing,
  provisionAcceptanceDrawings,
  requireCameraMotion,
  requireDistinctStagedResults,
  runApiPreflight,
  scrubbedDetail,
  safeFailureDetails,
  safeFailureRecord,
  takeEditingCheckout,
  validateConfig,
  validateStagedAuthorResponse,
  waitForTerminalJob,
  waitForJobOutcome,
} from './deployed_authored_cad_acceptance.mjs'

const REVISION = 'f'.repeat(40)
const DIGEST = `sha256:${'a'.repeat(64)}`
const TOKEN_A = 'aaa.bbb.ccc'
const TOKEN_B = 'ddd.eee.fff'
const ORIGINAL_ERROR = console.error
const sha256 = (value) => createHash('sha256').update(value).digest('hex')

function environment(overrides = {}) {
  const runId = 'run-20260726'
  return {
    LEAF_ACCEPTANCE_ENVIRONMENT: 'staging',
    LEAF_ACCEPTANCE_RUN_ID: runId,
    LEAF_ACCEPTANCE_WEB_URL: 'https://staging.leaf.test',
    LEAF_ACCEPTANCE_API_URL: 'https://staging-api.leaf.test',
    LEAF_ACCEPTANCE_ALLOWED_HOSTS: 'staging.leaf.test,staging-api.leaf.test',
    LEAF_ACCEPTANCE_EXPECTED_REVISION: REVISION,
    LEAF_ACCEPTANCE_PUBLICATION_APPROVAL_SECRET: 'independent-secret-value',
    LEAF_ACCEPTANCE_TENANT_A_ID: 'acceptance-a',
    LEAF_ACCEPTANCE_TENANT_A_JWT: TOKEN_A,
    LEAF_ACCEPTANCE_TENANT_A_DRAWING_ID: `acceptance-${runId}-a`,
    LEAF_ACCEPTANCE_TENANT_A_REQUEST: `Create a novel sitting cat for ${runId}`,
    LEAF_ACCEPTANCE_TENANT_B_ID: 'acceptance-b',
    LEAF_ACCEPTANCE_TENANT_B_JWT: TOKEN_B,
    LEAF_ACCEPTANCE_TENANT_B_DRAWING_ID: `acceptance-${runId}-b`,
    LEAF_ACCEPTANCE_TENANT_B_REQUEST: `Create a novel sitting fox for ${runId}`,
    ...overrides,
  }
}

function deploymentIdentity(overrides = {}) {
  return {
    schema: 'leaf.deployment-identity.v1',
    environment: 'staging',
    source_revision: REVISION,
    services: Object.fromEntries(
      ['app', 'broker', 'canonical-worker', 'harness', 'web']
        .map((name) => [name, { image_digest: DIGEST, source_revision: REVISION }]),
    ),
    ...overrides,
  }
}

function ready(overrides = {}) {
  return {
    ok: true,
    ready: true,
    status: 'ready',
    degraded_mode: false,
    source_revision: REVISION,
    dependencies: Object.fromEntries(
      ['broker', 'harness', 'database', 'worker', 'durable_stores', 'build']
        .map((name) => [name, { state: 'ready' }]),
    ),
    ...overrides,
  }
}

function response(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

afterEach(() => {
  console.error = ORIGINAL_ERROR
})

describe('deployed authored CAD acceptance configuration', () => {
  it('rejects every production hostname spelling before any local or network action', () => {
    const config = validateConfig(environment())
    assert.equal(config.environment, 'staging')
    assert.equal(config.tenants.length, 2)

    for (const overrides of [
      { LEAF_ACCEPTANCE_ENVIRONMENT: 'production' },
      { LEAF_ACCEPTANCE_WEB_URL: 'http://staging.leaf.test' },
      { LEAF_ACCEPTANCE_API_URL: 'https://staging-api.leaf.test/api' },
      {
        LEAF_ACCEPTANCE_WEB_URL: 'https://platform.leafdesign.ai',
        LEAF_ACCEPTANCE_ALLOWED_HOSTS: 'platform.leafdesign.ai,staging-api.leaf.test',
      },
      {
        LEAF_ACCEPTANCE_WEB_URL: 'https://platform.leafdesign.ai.',
        LEAF_ACCEPTANCE_ALLOWED_HOSTS: 'platform.leafdesign.ai.,staging-api.leaf.test',
      },
      {
        LEAF_ACCEPTANCE_WEB_URL: 'https://platform.leafdesign.ai:443',
        LEAF_ACCEPTANCE_ALLOWED_HOSTS: 'platform.leafdesign.ai,staging-api.leaf.test',
      },
      {
        LEAF_ACCEPTANCE_WEB_URL: 'https://platform.leafdesign.ai:8443',
        LEAF_ACCEPTANCE_ALLOWED_HOSTS: 'platform.leafdesign.ai,staging-api.leaf.test',
      },
      { LEAF_ACCEPTANCE_ALLOWED_HOSTS: 'other.test,staging-api.leaf.test' },
    ]) {
      assert.throws(() => validateConfig(environment(overrides)), AcceptanceError)
    }
  })

  it('requires distinct tenant identities and acceptance-only drawing ids', () => {
    assert.throws(
      () => validateConfig(environment({ LEAF_ACCEPTANCE_TENANT_B_JWT: TOKEN_A })),
      /must be distinct/,
    )
    assert.throws(
      () => validateConfig(environment({ LEAF_ACCEPTANCE_TENANT_A_DRAWING_ID: 'customer-drawing' })),
      /must equal acceptance-/,
    )
    assert.throws(
      () => validateConfig(environment({ LEAF_ACCEPTANCE_TENANT_A_REQUEST: 'generic request' })),
      /must contain the run id/,
    )
  })

  it('requires a separate publication approval credential only for execute mode', () => {
    assert.doesNotThrow(() => validateConfig(environment({
      LEAF_ACCEPTANCE_PUBLICATION_APPROVAL_SECRET: '',
    })))
    assert.throws(
      () => validateConfig(environment({
        LEAF_ACCEPTANCE_PUBLICATION_APPROVAL_SECRET: '',
      }), true),
      /PUBLICATION_APPROVAL_SECRET is required/,
    )
    assert.throws(
      () => validateConfig(environment({
        LEAF_ACCEPTANCE_PUBLICATION_APPROVAL_SECRET: 'too-short',
      }), true),
      /at least 16 characters/,
    )
  })

  it('requires live deployment identity with five immutable digests and a full source SHA', () => {
    assert.equal(evaluateDeploymentIdentity(deploymentIdentity(), REVISION).environment, 'staging')

    const mixed = deploymentIdentity()
    mixed.services.broker = { image_digest: DIGEST, source_revision: 'e'.repeat(40) }
    assert.throws(() => evaluateDeploymentIdentity(mixed, REVISION), /broker live identity was not built/)

    const missing = deploymentIdentity()
    delete missing.services.harness
    assert.throws(() => evaluateDeploymentIdentity(missing, REVISION), /services must be exactly/)

    const mutable = deploymentIdentity()
    mutable.services.web.image_digest = 'latest'
    assert.throws(() => evaluateDeploymentIdentity(mutable, REVISION), /immutable image digest/)
    assert.throws(
      () => validateConfig(environment({ LEAF_ACCEPTANCE_EXPECTED_REVISION: 'f'.repeat(39) })),
      /not a source revision/,
    )
  })
})

describe('deployed authored CAD acceptance checks', () => {
  it('uses the curated read-only drawing in preflight and uploaded drawings in execute', () => {
    const tenant = {
      label: 'A',
      drawingId: 'acceptance-run-a',
      jwt: TOKEN_A,
    }

    assert.deepEqual(browserTenantForMode(tenant, false), {
      ...tenant,
      drawingId: 'rooftop_demo',
    })
    assert.equal(browserTenantForMode(tenant, true), tenant)
  })

  it('opens authoring through the /try Author tab, not the /app section', async () => {
    const calls = []
    const authorRequest = {
      waitFor: async (options) => calls.push(['field.waitFor', options]),
    }
    const authorTab = {
      waitFor: async (options) => calls.push(['tab.waitFor', options]),
      isEnabled: async () => true,
      click: async () => calls.push(['tab.click']),
    }
    const page = {
      getByRole: (role, options) => {
        calls.push(['getByRole', role, options])
        return authorTab
      },
      getByLabel: (label) => {
        calls.push(['getByLabel', label])
        return authorRequest
      },
      locator: (selector) => {
        throw new Error(`unexpected locator: ${selector}`)
      },
    }

    assert.equal(await openTryAuthorSurface(page), authorRequest)
    assert.deepEqual(calls, [
      ['getByRole', 'tab', { name: 'Author', exact: true }],
      ['tab.waitFor', { state: 'visible', timeout: 30_000 }],
      ['tab.click'],
      ['getByLabel', 'What should the tool do?'],
      ['field.waitFor', { state: 'visible', timeout: 30_000 }],
    ])
  })

  it('reports a disabled or missing /try author surface by acceptance stage', async () => {
    const disabledPage = {
      getByRole: () => ({
        waitFor: async () => {},
        isEnabled: async () => false,
      }),
    }
    await assert.rejects(
      () => openTryAuthorSurface(disabledPage),
      (error) => error instanceof AcceptanceError
        && error.check === 'author_surface'
        && /disabled/.test(error.message),
    )

    const missingPage = {
      getByRole: () => ({
        waitFor: async () => {
          const error = new Error('missing')
          error.name = 'TimeoutError'
          throw error
        },
      }),
    }
    await assert.rejects(
      () => openTryAuthorSurface(missingPage),
      (error) => error instanceof AcceptanceError
        && error.check === 'author_surface'
        && /TimeoutError/.test(error.message),
    )
  })

  it('returns from Jobs to the drawing View before checking camera controls', async () => {
    const calls = []
    const viewTab = {
      waitFor: async (options) => calls.push(['tab.waitFor', options]),
      isEnabled: async () => true,
      click: async () => calls.push(['tab.click']),
    }
    const page = {
      getByRole: (role, options) => {
        calls.push(['getByRole', role, options])
        return viewTab
      },
    }

    await openDrawingView(page)
    assert.deepEqual(calls, [
      ['getByRole', 'tab', { name: 'View', exact: true }],
      ['tab.waitFor', { state: 'visible', timeout: 30_000 }],
      ['tab.click'],
    ])
  })

  it('takes and proves the drawing checkout before authored execution', async () => {
    const calls = []
    const take = {
      waitFor: async (options) => calls.push(['take.waitFor', options]),
      click: async () => calls.push(['take.click']),
    }
    const held = {
      waitFor: async (options) => calls.push(['held.waitFor', options]),
    }
    const response = {
      url: () => 'https://staging-api.leaf.test/api/drawings/drawing-a/checkout',
      request: () => ({ method: () => 'POST' }),
      status: () => 200,
    }
    const page = {
      getByRole: (role, options) => {
        calls.push(['getByRole', role, options])
        return take
      },
      waitForResponse: async (predicate, options) => {
        calls.push(['waitForResponse', options])
        assert.equal(predicate(response), true)
        return response
      },
      getByText: (text, options) => {
        calls.push(['getByText', text, options])
        return held
      },
    }

    await takeEditingCheckout(page, 'https://staging-api.leaf.test', 'drawing-a')
    assert.deepEqual(calls, [
      ['getByRole', 'button', { name: 'Take edit lock', exact: true }],
      ['take.waitFor', { state: 'visible', timeout: 30_000 }],
      ['waitForResponse', { timeout: 30_000 }],
      ['take.click'],
      ['getByText', 'You hold the edit lock', { exact: true }],
      ['held.waitFor', { state: 'visible', timeout: 30_000 }],
    ])
  })

  it('reports a rejected drawing checkout by acceptance stage', async () => {
    const page = {
      getByRole: () => ({ waitFor: async () => {}, click: async () => {} }),
      waitForResponse: async () => ({
        url: () => 'https://staging-api.leaf.test/api/drawings/drawing-a/checkout',
        request: () => ({ method: () => 'POST' }),
        status: () => 409,
      }),
    }
    await assert.rejects(
      () => takeEditingCheckout(page, 'https://staging-api.leaf.test', 'drawing-a'),
      (error) => error instanceof AcceptanceError
        && error.check === 'checkout'
        && /HTTP 409/.test(error.message),
    )
  })

  it('denies the wrong tenant before the owner uses the independent approval route', async () => {
    const config = validateConfig(environment(), true)
    const calls = []
    const fetchImpl = async (url, options) => {
      calls.push({ url: String(url), options })
      return options.headers['X-Tenant-Id'] === 'acceptance-a'
        ? response(200, { confirmation_id: 'confirmation-one' })
        : response(404, {})
    }
    const changeSetId = '11111111-1111-4111-8111-111111111111'
    const result = await approveIsolatedStagedPublication(
      config, config.tenants[0], config.tenants[1], changeSetId, fetchImpl,
    )
    assert.equal(result.status, 'approved')
    assert.equal(calls.length, 2)
    assert.equal(
      calls[0].url,
      'https://staging-api.leaf.test/internal/customization/confirm',
    )
    assert.deepEqual(calls.map((call) => call.options.headers['X-Tenant-Id']), [
      'acceptance-b', 'acceptance-a',
    ])
    for (const call of calls) {
      assert.equal(call.options.headers.Authorization, undefined)
      assert.equal(call.options.headers['X-Approval-Secret'], 'independent-secret-value')
      assert.deepEqual(JSON.parse(call.options.body), { change_set_id: changeSetId })
    }

    const alwaysMissing = async () => response(404, {})
    await assert.rejects(
      () => approveIsolatedStagedPublication(
        config, config.tenants[0], config.tenants[1], changeSetId, alwaysMissing,
      ),
      /approval authority returned HTTP 404/,
    )

    const alwaysPermissive = async () => response(200, { confirmation_id: 'leaked' })
    await assert.rejects(
      () => approveIsolatedStagedPublication(
        config, config.tenants[0], config.tenants[1], changeSetId, alwaysPermissive,
      ),
      /wrong-tenant approval authority returned HTTP 200/,
    )
  })

  it('fails closed on degraded dependencies and mixed live revision', () => {
    evaluateReadiness(ready(), REVISION)
    const degraded = ready()
    degraded.dependencies.worker.state = 'degraded'
    assert.throws(() => evaluateReadiness(degraded, REVISION), /worker is not ready/)
    assert.throws(
      () => evaluateReadiness(ready({ source_revision: 'e'.repeat(40) }), REVISION),
      /does not match/,
    )
  })

  it('provisions a tenant-owned live DWG and waits for extraction readiness', async () => {
    const config = validateConfig(environment(), true)
    const tenant = config.tenants[0]
    const drawingId = '11111111-1111-4111-8111-111111111111'
    const calls = []
    const fetchImpl = async (url, options) => {
      calls.push({ url: String(url), options })
      if (new URL(url).pathname === '/api/drawings/upload') {
        assert.ok(options.body instanceof FormData)
        assert.equal(options.headers.Authorization, `Bearer ${TOKEN_A}`)
        return response(202, { drawing_id: drawingId, tenant_id: tenant.id, status: 'extracting' })
      }
      return response(200, { drawing_id: drawingId, tenant_id: tenant.id, status: 'ready' })
    }
    assert.equal(
      await provisionAcceptanceDrawing(
        config, tenant, new Uint8Array([1, 2, 3]),
        { fetchImpl, waitImpl: async () => {}, maxPolls: 2 },
      ),
      drawingId,
    )
    assert.deepEqual(calls.map((call) => new URL(call.url).pathname), [
      '/api/drawings/upload',
      `/api/drawings/${drawingId}/upload-status`,
    ])
  })

  it('retries a transient transport failure on the DWG upload, then succeeds', async () => {
    const config = validateConfig(environment(), true)
    const tenant = config.tenants[0]
    const drawingId = '11111111-1111-4111-8111-111111111111'
    let uploadAttempts = 0
    const fetchImpl = async (url) => {
      if (new URL(url).pathname === '/api/drawings/upload') {
        uploadAttempts += 1
        if (uploadAttempts < 3) {
          const err = new TypeError('fetch failed')
          throw err
        }
        return response(202, { drawing_id: drawingId, tenant_id: tenant.id, status: 'extracting' })
      }
      return response(200, { drawing_id: drawingId, tenant_id: tenant.id, status: 'ready' })
    }
    assert.equal(
      await provisionAcceptanceDrawing(
        config, tenant, new Uint8Array([1, 2, 3]),
        { fetchImpl, waitImpl: async () => {}, maxPolls: 2 },
      ),
      drawingId,
    )
    assert.equal(uploadAttempts, 3)
  })

  it('gives up the DWG upload after the bounded transport-retry budget', async () => {
    const config = validateConfig(environment(), true)
    const tenant = config.tenants[0]
    let uploadAttempts = 0
    const fetchImpl = async (url) => {
      if (new URL(url).pathname === '/api/drawings/upload') {
        uploadAttempts += 1
        throw new TypeError('fetch failed')
      }
      return response(200, {})
    }
    await assert.rejects(
      () => provisionAcceptanceDrawing(
        config, tenant, new Uint8Array([1, 2, 3]),
        { fetchImpl, waitImpl: async () => {} },
      ),
      (error) => error instanceof AcceptanceError
        && error.check === 'live_dwg_A'
        && /after 3 attempts/.test(error.message),
    )
    assert.equal(uploadAttempts, 3)
  })

  it('does not retry a real non-2xx upload receipt', async () => {
    const config = validateConfig(environment(), true)
    const tenant = config.tenants[0]
    let uploadAttempts = 0
    const fetchImpl = async (url) => {
      if (new URL(url).pathname === '/api/drawings/upload') {
        uploadAttempts += 1
        return response(403, { error: { message: 'forbidden' } })
      }
      return response(200, {})
    }
    await assert.rejects(
      () => provisionAcceptanceDrawing(
        config, tenant, new Uint8Array([1, 2, 3]),
        { fetchImpl, waitImpl: async () => {} },
      ),
      /invalid HTTP 403 receipt/,
    )
    assert.equal(uploadAttempts, 1)
  })

  it('provisions tenant DWGs sequentially for the single-slot APS pool', async () => {
    const config = validateConfig(environment(), true)
    const calls = []
    let finishFirst
    let finishSecond
    const first = new Promise((resolve) => { finishFirst = resolve })
    const second = new Promise((resolve) => { finishSecond = resolve })
    const provisionImpl = async (_config, tenant) => {
      calls.push(tenant.label)
      return tenant.label === 'A' ? first : second
    }

    const pending = provisionAcceptanceDrawings(
      config,
      new Uint8Array([1, 2, 3]),
      { provisionImpl },
    )
    await Promise.resolve()
    assert.deepEqual(calls, ['A'])

    finishFirst('drawing-a')
    await new Promise((resolve) => setImmediate(resolve))
    assert.deepEqual(calls, ['A', 'B'])

    finishSecond('drawing-b')
    assert.deepEqual(await pending, ['drawing-a', 'drawing-b'])
  })

  it('reports a terminal broker failure before waiting for browser version state', async () => {
    const config = validateConfig(environment(), true)
    const tenant = config.tenants[0]
    const fetchImpl = async () => response(200, {
      status: 'failed',
      error: { error_code: 'BAD_PARAMS', message: 'unknown drawing' },
    })
    const jobId = '11111111-1111-4111-8111-111111111111'
    await assert.rejects(
      () => waitForTerminalJob(config, tenant, jobId, { fetchImpl, waitImpl: async () => {} }),
      (error) => error instanceof AcceptanceError
        && error.check === 'authored_job'
        && error.message === 'the authored write job failed'
        && error.details.job_id === jobId
        && error.details.terminal_status === 'failed'
        && error.details.error_code === 'BAD_PARAMS',
    )
  })

  it('emits only bounded token-free job correlation details', () => {
    const safe = safeFailureDetails(new AcceptanceError('authored_job', 'failed', {
      job_id: '11111111-1111-4111-8111-111111111111',
      terminal_status: 'failed',
      error_code: 'BAD_PARAMS',
      backend_body: { authorization: `Bearer ${TOKEN_A}` },
    }))
    assert.deepEqual(safe, {
      job_id: '11111111-1111-4111-8111-111111111111',
      terminal_status: 'failed',
      error_code: 'BAD_PARAMS',
    })
    assert.equal(safeFailureDetails(new AcceptanceError('authored_job', 'failed', {
      job_id: TOKEN_A,
      terminal_status: 'unknown',
      error_code: TOKEN_A,
    })), undefined)
    assert.deepEqual(safeFailureDetails(new AcceptanceError('authored_job', 'failed', {
      job_id: '22222222-2222-4222-8222-222222222222',
      terminal_status: 'http_error',
      http_status: 503,
    })), {
      job_id: '22222222-2222-4222-8222-222222222222',
      terminal_status: 'http_error',
      http_status: 503,
    })
  })

  it('scrubs the complete one-line failure record and drops credential-shaped job ids', () => {
    const jwtish = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhY2NlcHRhbmNlIn0.c2lnbmF0dXJlLXNpZ25hdHVyZQ'
    const record = safeFailureRecord(new AcceptanceError(
      'authored_job',
      `backend returned ${jwtish}`,
      {
        job_id: `sk_live_${'x'.repeat(48)}`,
        terminal_status: 'failed',
        error_code: jwtish,
        backend_body: { authorization: `Bearer ${jwtish}` },
      },
    ), 'browser_execute_A')
    const serialized = JSON.stringify(record)
    assert.ok(!serialized.includes(jwtish))
    assert.ok(!serialized.includes('sk_live_'))
    assert.equal(record.message, 'authored job did not complete successfully')
    assert.ok(!Object.hasOwn(record, 'detail'))
    assert.deepEqual(record.details, { terminal_status: 'failed' })
  })

  it('drops every backend secret from a terminal authored-job record', async () => {
    const config = validateConfig(environment(), true)
    const tenant = config.tenants[0]
    const jwt = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhY2NlcHRhbmNlIn0.c2lnbmF0dXJlLXNpZ25hdHVyZQ'
    const apiKey = `sk_live_${'x'.repeat(48)}`
    const githubToken = `github_pat_${'y'.repeat(48)}`
    const bearer = `Bearer ${'z'.repeat(64)}`
    const opaque = `opaque-${'q'.repeat(64)}`
    const fetchImpl = async () => response(200, {
      status: 'failed',
      error: {
        error_code: apiKey,
        message: `${jwt} ${githubToken} ${bearer} ${opaque}`,
      },
    })
    let failure
    try {
      await waitForTerminalJob(
        config,
        tenant,
        '33333333-3333-4333-8333-333333333333',
        { fetchImpl, waitImpl: async () => {} },
      )
    } catch (error) {
      failure = error
    }
    const serialized = JSON.stringify(safeFailureRecord(failure, 'browser_execute_A'))
    for (const secret of [jwt, apiKey, githubToken, bearer, opaque]) {
      assert.ok(!serialized.includes(secret))
    }
    assert.deepEqual(JSON.parse(serialized), {
      ok: false,
      check: 'authored_job',
      error: 'AcceptanceError',
      message: 'authored job did not complete successfully',
      step: 'browser_execute_A',
      details: {
        job_id: '33333333-3333-4333-8333-333333333333',
        terminal_status: 'failed',
      },
    })
  })

  it('rejects secret-shaped job ids before request paths or records can expose them', async () => {
    const config = validateConfig(environment(), true)
    const tenant = config.tenants[0]
    const secrets = [
      'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhY2NlcHRhbmNlIn0.c2lnbmF0dXJlLXNpZ25hdHVyZQ',
      `sk_live_${'x'.repeat(48)}`,
      `github_pat_${'y'.repeat(48)}`,
      `Bearer ${'z'.repeat(64)}`,
      `opaque-${'q'.repeat(64)}`,
    ]
    let fetchCalls = 0
    const fetchImpl = async () => {
      fetchCalls += 1
      return { status: 200, json: async () => { throw new Error('malformed') } }
    }
    for (const jobId of secrets) {
      let failure
      try {
        await waitForTerminalJob(config, tenant, jobId, { fetchImpl, waitImpl: async () => {} })
      } catch (error) {
        failure = error
      }
      const serialized = JSON.stringify(safeFailureRecord(failure, 'authored_job_A'))
      assert.ok(!serialized.includes(jobId))
      assert.equal(JSON.parse(serialized).check, 'authored_job')
    }
    assert.equal(fetchCalls, 0)
  })

  it('maps a malformed status response to a constant authored-job failure', async () => {
    const config = validateConfig(environment(), true)
    const tenant = config.tenants[0]
    const fetchImpl = async () => ({
      status: 200,
      json: async () => { throw new Error(`opaque-${'q'.repeat(64)}`) },
    })
    let failure
    try {
      await waitForTerminalJob(
        config,
        tenant,
        '44444444-4444-4444-8444-444444444444',
        { fetchImpl, waitImpl: async () => {} },
      )
    } catch (error) {
      failure = error
    }
    const record = safeFailureRecord(failure, 'authored_job_A')
    assert.equal(record.check, 'authored_job')
    assert.equal(record.message, 'authored job did not complete successfully')
    assert.ok(!Object.hasOwn(record, 'detail'))
  })

  it('rejects secret-shaped replay job ids without fetching or exposing them', async () => {
    const config = validateConfig(environment(), true)
    const tenant = config.tenants[0]
    const secrets = [
      `sk_live_${'x'.repeat(48)}`,
      `github_pat_${'y'.repeat(48)}`,
      `opaque-${'q'.repeat(64)}`,
    ]
    let fetchCalls = 0
    const fetchImpl = async () => {
      fetchCalls += 1
      throw new Error('transport failed')
    }
    for (const jobId of secrets) {
      let failure
      try {
        await waitForJobOutcome(config, tenant, jobId, { fetchImpl, waitImpl: async () => {} })
      } catch (error) {
        failure = error
      }
      const serialized = JSON.stringify(safeFailureRecord(failure, 'stale_head_replay_A'))
      assert.ok(!serialized.includes(jobId))
      assert.equal(JSON.parse(serialized).check, 'authored_job')
    }
    assert.equal(fetchCalls, 0)
  })

  it('maps replay transport and malformed JSON failures to safe authored-job records', async () => {
    const config = validateConfig(environment(), true)
    const tenant = config.tenants[0]
    const jobId = '55555555-5555-4555-8555-555555555555'
    const secret = `opaque-${'q'.repeat(64)}`
    const fetchers = [
      async () => { throw new Error(secret) },
      async () => ({ status: 200, json: async () => { throw new Error(secret) } }),
    ]
    for (const fetchImpl of fetchers) {
      let failure
      try {
        await waitForJobOutcome(config, tenant, jobId, { fetchImpl, waitImpl: async () => {} })
      } catch (error) {
        failure = error
      }
      const record = safeFailureRecord(failure, 'stale_head_replay_A')
      const serialized = JSON.stringify(record)
      assert.ok(!serialized.includes(secret))
      assert.equal(record.check, 'authored_job')
      assert.equal(record.message, 'authored job did not complete successfully')
      assert.ok(!Object.hasOwn(record, 'detail'))
      assert.deepEqual(record.details, {
        job_id: jobId,
        terminal_status: 'http_error',
      })
    }
  })

  it('proves forged tenant headers cannot override either JWT identity', async () => {
    const config = validateConfig(environment())
    const calls = []
    const fetchImpl = async (url, options) => {
      calls.push({
        url: String(url),
        authorization: options.headers.Authorization,
        credentials: options.credentials,
        redirect: options.redirect,
      })
      const path = new URL(url).pathname
      if (path === '/api/deployment-identity') return response(200, deploymentIdentity())
      if (path === '/api/ready') return response(200, ready())
      if (path === '/api/tenant/claude-grant') {
        return response(200, { linked: true, kind: 'oauth' })
      }
      if (path === '/api/session') {
        const tenantId = options.headers.Authorization === `Bearer ${TOKEN_A}`
          ? 'acceptance-a'
          : 'acceptance-b'
        return response(200, { intake: {}, tenant_id: tenantId })
      }
      return response(500, {})
    }
    const result = await runApiPreflight(config, fetchImpl)
    assert.equal(result.tenant_header_override, 'denied')
    assert.equal(calls.length, 6)
    assert.ok(calls.every((call) => call.credentials === 'omit'))
    assert.ok(calls.every((call) => call.redirect === 'error'))
    assert.ok(calls.every((call) => [
      `Bearer ${TOKEN_A}`,
      `Bearer ${TOKEN_B}`,
    ].includes(call.authorization)))

    const permissive = async (url, options) => {
      const path = new URL(url).pathname
      if (path === '/api/deployment-identity') return response(200, deploymentIdentity())
      if (path === '/api/ready') return response(200, ready())
      if (path === '/api/tenant/claude-grant') {
        return response(200, { linked: true, kind: 'oauth' })
      }
      return response(200, {
        intake: {},
        tenant_id: options.headers['X-Tenant-Id'],
      })
    }
    await assert.rejects(
      () => runApiPreflight(config, permissive),
      /overrode the JWT tenant identity/,
    )
  })

  it('accepts only denial or a non-disclosing fallback for forged cross-tenant drawing reads', async () => {
    const config = validateConfig(environment(), true)
    const ownA = { tenant: 'a', shape: 'cat' }
    const ownB = { tenant: 'b', shape: 'fox' }
    const browser = [
      { label: 'A', executed: true },
      { label: 'B', executed: true },
    ]
    // The caller's bootstrapped public demo seat — what the tenant-scoped store
    // serves for ANY non-owned id (write_loop.ensure_demo_drawing seeds every
    // first-seen id per-tenant from the cached demo).
    const SEAT = { curated: 'demo_seat', public: true }
    // What the leg returns when no leak is client-observable: adversarial
    // (transformed) confidentiality is explicitly deferred to the receipt's
    // external audit evidence rather than claimed.
    const CONTAINED = {
      status: 'no_client_observable_disclosure',
      distinct_result_hashes: true,
      adversarial_confidentiality: 'requires_external_audit',
    }
    // Classify the requested drawing id: the caller's own acceptance drawing,
    // the OTHER tenant's real acceptance drawing, or any other non-owned id
    // (which is what the leg's clean random-id control requests).
    const classify = (drawing, tokenA) => {
      const ownDrawing = (drawing.endsWith('-a') && tokenA) || (drawing.endsWith('-b') && !tokenA)
      if (ownDrawing) return 'own'
      if (drawing.endsWith('-a') || drawing.endsWith('-b')) return 'other'
      return 'miss'
    }

    // Denial: every non-owned read is 404.
    const denied = async (url, options) => {
      const drawing = new URL(url).pathname.split('/').at(-2)
      const tokenA = options.headers.Authorization === `Bearer ${TOKEN_A}`
      return classify(drawing, tokenA) === 'own'
        ? response(200, { intake: tokenA ? ownA : ownB, tenant_id: tokenA ? 'acceptance-a' : 'acceptance-b' })
        : response(404, {})
    }
    assert.deepEqual(
      await proveExecutedDrawingIsolation(config, browser, denied),
      { ...CONTAINED, status: 'denied' },
    )

    // The /try surface's real posture: EVERY non-owned read (the other's real
    // id and the random control alike) returns the caller's own bootstrapped
    // seat. Identity echoes the caller, cross == control, and neither equals a
    // private drawing — containment.
    const callerOwnFallback = async (url, options) => {
      const drawing = new URL(url).pathname.split('/').at(-2)
      const tokenA = options.headers.Authorization === `Bearer ${TOKEN_A}`
      const callerId = tokenA ? 'acceptance-a' : 'acceptance-b'
      if (classify(drawing, tokenA) === 'own') return response(200, { intake: tokenA ? ownA : ownB, tenant_id: callerId })
      return response(200, { intake: SEAT, tenant_id: callerId })
    }
    assert.deepEqual(
      await proveExecutedDrawingIsolation(config, browser, callerOwnFallback),
      CONTAINED,
    )

    // Accidental regression — the signature this leg exists to catch: non-owned
    // reads serve the other tenant's UNTRANSFORMED private intake (sol-critic
    // round 3's unpartitioned global miss-fallback). cross == control, but the
    // bytes equal the other tenant's real drawing — rejected by the own-hash
    // guard.
    const exactLeak = async (url, options) => {
      const drawing = new URL(url).pathname.split('/').at(-2)
      const tokenA = options.headers.Authorization === `Bearer ${TOKEN_A}`
      const callerId = tokenA ? 'acceptance-a' : 'acceptance-b'
      if (classify(drawing, tokenA) === 'own') return response(200, { intake: tokenA ? ownA : ownB, tenant_id: callerId })
      return response(200, { intake: tokenA ? ownB : ownA, tenant_id: callerId })
    }
    await assert.rejects(
      () => proveExecutedDrawingIsolation(config, browser, exactLeak),
      /disclosed the other tenant's drawing/,
    )

    // Id-keyed leak (sol-critic round 1's augmented copy): only the other
    // tenant's REAL id leaks (here transformed, so the own-hash guard alone
    // would miss it); a random miss returns the safe seat. cross != control —
    // rejected by the control-equality guard.
    const idKeyedLeak = async (url, options) => {
      const drawing = new URL(url).pathname.split('/').at(-2)
      const tokenA = options.headers.Authorization === `Bearer ${TOKEN_A}`
      const callerId = tokenA ? 'acceptance-a' : 'acceptance-b'
      const kind = classify(drawing, tokenA)
      if (kind === 'own') return response(200, { intake: tokenA ? ownA : ownB, tenant_id: callerId })
      if (kind === 'other') return response(200, { intake: { ...(tokenA ? ownB : ownA), leaked_from_other_tenant: true }, tenant_id: callerId })
      return response(200, { intake: SEAT, tenant_id: callerId })
    }
    await assert.rejects(
      () => proveExecutedDrawingIsolation(config, browser, idKeyedLeak),
      /disclosed the other tenant's drawing/,
    )

    // Header-driven leak (sol-critic round 2): the leak is keyed on the forged
    // X-Tenant-Id header, not the id. The clean control carries no forged
    // header, so cross != control — rejected.
    const headerDrivenLeak = async (url, options) => {
      const drawing = new URL(url).pathname.split('/').at(-2)
      const tokenA = options.headers.Authorization === `Bearer ${TOKEN_A}`
      const callerId = tokenA ? 'acceptance-a' : 'acceptance-b'
      if (classify(drawing, tokenA) === 'own') return response(200, { intake: tokenA ? ownA : ownB, tenant_id: callerId })
      const forgedTid = options.headers['X-Tenant-Id']
      if (forgedTid) return response(200, { intake: forgedTid === 'acceptance-b' ? ownB : ownA, tenant_id: callerId })
      return response(200, { intake: SEAT, tenant_id: callerId })
    }
    await assert.rejects(
      () => proveExecutedDrawingIsolation(config, browser, headerDrivenLeak),
      /disclosed the other tenant's drawing/,
    )

    // A 200 that echoes the OTHER tenant's identity (forged X-Tenant-Id took)
    // is a bypass, rejected even when the body is the safe seat.
    const forgedIdentityHonored = async (url, options) => {
      const drawing = new URL(url).pathname.split('/').at(-2)
      const tokenA = options.headers.Authorization === `Bearer ${TOKEN_A}`
      const callerId = tokenA ? 'acceptance-a' : 'acceptance-b'
      const kind = classify(drawing, tokenA)
      if (kind === 'own') return response(200, { intake: tokenA ? ownA : ownB, tenant_id: callerId })
      if (kind === 'other') return response(200, { intake: SEAT, tenant_id: options.headers['X-Tenant-Id'] })
      return response(200, { intake: SEAT, tenant_id: callerId })
    }
    await assert.rejects(
      () => proveExecutedDrawingIsolation(config, browser, forgedIdentityHonored),
      /disclosed the other tenant's drawing/,
    )

    // Suspicious id-dependence: the other's real id gets a 200 while a random
    // miss 404s. The clean control must succeed for containment to stand.
    const inconsistentFallback = async (url, options) => {
      const drawing = new URL(url).pathname.split('/').at(-2)
      const tokenA = options.headers.Authorization === `Bearer ${TOKEN_A}`
      const callerId = tokenA ? 'acceptance-a' : 'acceptance-b'
      const kind = classify(drawing, tokenA)
      if (kind === 'own') return response(200, { intake: tokenA ? ownA : ownB, tenant_id: callerId })
      if (kind === 'other') return response(200, { intake: SEAT, tenant_id: callerId })
      return response(404, {})
    }
    await assert.rejects(
      () => proveExecutedDrawingIsolation(config, browser, inconsistentFallback),
      /disclosed the other tenant's drawing/,
    )

    // SCOPE BOUNDARY (sol-critic round 4, accepted): a server that leaks a
    // TRANSFORMED copy of the other tenant's drawing identically for every
    // non-owned probe is indistinguishable, from the client, from a safe
    // bootstrapped fallback — the client has no independent channel to the
    // other tenant's private bytes. The leg therefore does NOT claim to catch
    // it: it returns the containment result whose adversarial_confidentiality
    // field defers that proof to the receipt's external audit evidence. This
    // test codifies the boundary so a future edit cannot silently widen the
    // claim without revisiting it.
    const uniformTransformedLeak = async (url, options) => {
      const drawing = new URL(url).pathname.split('/').at(-2)
      const tokenA = options.headers.Authorization === `Bearer ${TOKEN_A}`
      const callerId = tokenA ? 'acceptance-a' : 'acceptance-b'
      if (classify(drawing, tokenA) === 'own') return response(200, { intake: tokenA ? ownA : ownB, tenant_id: callerId })
      return response(200, { intake: { ...(tokenA ? ownB : ownA), transformed: true }, tenant_id: callerId })
    }
    assert.deepEqual(
      await proveExecutedDrawingIsolation(config, browser, uniformTransformedLeak),
      CONTAINED,
    )
  })

  it('proves repository, catalog, job, and audit isolation', async () => {
    const config = validateConfig(environment(), true)
    const browser = [
      {
        label: 'A', executed: true, _staged_tool_name: 'cat_tool',
        _staged_change_set_id: '11111111-1111-4111-8111-111111111111',
        _job_id: 'job-a',
        _publication_confirmation_id: 'confirmation-a',
      },
      {
        label: 'B', executed: true, _staged_tool_name: 'fox_tool',
        _staged_change_set_id: '22222222-2222-4222-8222-222222222222',
        _job_id: 'job-b',
        _publication_confirmation_id: 'confirmation-b',
      },
    ]
    const fetchImpl = async (url, options) => {
      const parsed = new URL(url)
      const tokenA = options.headers.Authorization === `Bearer ${TOKEN_A}`
      if (parsed.pathname === '/api/tools') {
        return response(200, { tools: [{ name: tokenA ? 'cat_tool' : 'fox_tool' }] })
      }
      if (parsed.pathname === '/api/agent/audit') {
        return response(200, { records: [{ event: tokenA ? 'audit-a' : 'audit-b' }], count: 1 })
      }
      if (parsed.pathname.startsWith('/api/jobs/')) return response(404, {})
      if (parsed.pathname === '/api/author/publication-requests') return response(404, {})
      return response(500, {})
    }
    assert.deepEqual(
      await proveExecutedAuthorityIsolation(config, browser, fetchImpl),
      {
        status: 'denied',
        authorities: ['repository', 'catalog', 'job', 'audit'],
        tenants: ['A', 'B'],
      },
    )

    const leakingCatalog = async (url, options) => {
      const parsed = new URL(url)
      if (parsed.pathname === '/api/tools') {
        return response(200, { tools: [{ name: 'cat_tool' }, { name: 'fox_tool' }] })
      }
      return fetchImpl(url, options)
    }
    await assert.rejects(
      () => proveExecutedAuthorityIsolation(config, browser, leakingCatalog),
      /another tenant tool leaked/,
    )

  })

  it('rejects stale catalog and exact write replay without changing head', async () => {
    const config = validateConfig(environment(), true)
    const browser = config.tenants.map((tenant) => ({
      label: tenant.label,
      executed: true,
      _run_request: {
        tool: `${tenant.label.toLowerCase()}_tool`,
        dwg: tenant.drawingId,
        dwg_version: 1,
        expected_drawing_head: 1,
        catalog_digest: `sha256:${'a'.repeat(64)}`,
      },
    }))
    let acceptedRuns = 0
    const fetchImpl = async (url, options) => {
      const parsed = new URL(url)
      if (parsed.pathname.endsWith('/versions')) return response(200, { head: 2 })
      if (parsed.pathname === '/api/run') {
        acceptedRuns += 1
        const body = JSON.parse(options.body)
        const message = body.catalog_digest === `sha256:${'0'.repeat(64)}`
          ? 'catalog tool changed or confirmation digest is missing; refresh tools and confirm again'
          : 'drawing head changed after approval; refresh drawing state and confirm again'
        return response(409, { error: { error_code: 'BAD_PARAMS', message } })
      }
      return response(500, {})
    }
    assert.deepEqual(
      await provePinnedWriteRejections(config, browser, fetchImpl),
      {
        status: 'denied_without_mutation',
        stale_head: true,
        stale_catalog: true,
        replayed_exact_request: true,
        expired_approval: 'requires_external_evidence',
      },
    )
    assert.equal(acceptedRuns, 4)

    // A 202 whose job COMPLETES means the stale write LANDED — the exact danger
    // this leg guards. It must be rejected.
    const landingReplay = async (url) => {
      const path = new URL(url).pathname
      if (path.endsWith('/versions')) return response(200, { head: 2 })
      if (path === '/api/run') {
        return response(202, { job_id: '66666666-6666-4666-8666-666666666666' })
      }
      if (path.startsWith('/api/jobs/')) return response(200, { status: 'complete', result: { ok: true } })
      return response(500, {})
    }
    await assert.rejects(
      () => provePinnedWriteRejections(config, browser, landingReplay),
      /did not fail on the stale drawing-head pin/,
    )
  })

  it('accepts the live async stale-head rejection: 202 then a failed job', async () => {
    // The plain M2M catalog run does not carry expected_drawing_head, so the
    // synchronous head pin (jobs.py:501) is skipped and the WORKER rejects the
    // stale head — 202 at submit, then the job fails with "stale drawing head".
    // This is the real ee150b8 behavior; the sync-409 shape is the
    // conversational path.
    const config = validateConfig(environment(), true)
    const browser = config.tenants.map((tenant) => ({
      label: tenant.label,
      executed: true,
      _run_request: {
        tool: `${tenant.label.toLowerCase()}_tool`,
        dwg: tenant.drawingId,
        dwg_version: 1,
        catalog_digest: `sha256:${'a'.repeat(64)}`,
      },
    }))
    let jobPolls = 0
    const fetchImpl = async (url, options) => {
      const parsed = new URL(url)
      if (parsed.pathname.endsWith('/versions')) return response(200, { head: 2 })
      if (parsed.pathname.startsWith('/api/jobs/')) {
        jobPolls += 1
        return response(200, {
          status: 'failed',
          error: { error_code: 'BAD_PARAMS', message: 'drawing/mutation unavailable: stale drawing head: expected 1, current 2' },
        })
      }
      if (parsed.pathname === '/api/run') {
        const body = JSON.parse(options.body)
        if (body.catalog_digest === `sha256:${'0'.repeat(64)}`) {
          return response(409, { error: { error_code: 'BAD_PARAMS', message: 'catalog tool changed or confirmation digest is missing; refresh tools and confirm again' } })
        }
        return response(202, {
          job_id: body.tool.startsWith('a_')
            ? '77777777-7777-4777-8777-777777777777'
            : '88888888-8888-4888-8888-888888888888',
        })
      }
      return response(500, {})
    }
    assert.deepEqual(
      await provePinnedWriteRejections(config, browser, fetchImpl),
      {
        status: 'denied_without_mutation',
        stale_head: true,
        stale_catalog: true,
        replayed_exact_request: true,
        expired_approval: 'requires_external_evidence',
      },
    )
    assert.equal(jobPolls, 2) // one replay job per tenant, each failed on first poll

    // A 202 whose replay job COMPLETES (stale write landed) is rejected even on
    // the async path.
    const asyncLanding = async (url, options) => {
      const parsed = new URL(url)
      if (parsed.pathname.endsWith('/versions')) return response(200, { head: 2 })
      if (parsed.pathname.startsWith('/api/jobs/')) return response(200, { status: 'complete', result: { ok: true } })
      if (parsed.pathname === '/api/run') {
        const body = JSON.parse(options.body)
        if (body.catalog_digest === `sha256:${'0'.repeat(64)}`) {
          return response(409, { error: { error_code: 'BAD_PARAMS', message: 'catalog tool changed' } })
        }
        return response(202, { job_id: '99999999-9999-4999-8999-999999999999' })
      }
      return response(500, {})
    }
    await assert.rejects(
      () => provePinnedWriteRejections(config, browser, asyncLanding),
      /did not fail on the stale drawing-head pin/,
    )
  })

  it('detects a landed version even when head is unchanged', async () => {
    // sol-critic PR #412 round 1: a stale write could land as a new immutable
    // version that bumps `latest` and grows `versions` WITHOUT moving `head`.
    // Both replays report the expected rejection wording, yet a version lands.
    // A head-only after-check would miss it; the full version signature must not.
    const config = validateConfig(environment(), true)
    const browser = config.tenants.map((tenant) => ({
      label: tenant.label,
      executed: true,
      _run_request: {
        tool: `${tenant.label.toLowerCase()}_tool`,
        dwg: tenant.drawingId,
        dwg_version: 1,
        catalog_digest: `sha256:${'a'.repeat(64)}`,
      },
    }))
    const versionsReads = new Map()
    const fetchImpl = async (url, options) => {
      const parsed = new URL(url)
      if (parsed.pathname.endsWith('/versions')) {
        const key = parsed.pathname
        const n = (versionsReads.get(key) || 0) + 1
        versionsReads.set(key, n)
        // First read per drawing is `before` (latest 2), second is `after`
        // (latest 3 — a version landed despite the "rejection").
        return n === 1
          ? response(200, { head: 2, latest: 2, versions: [{ v: 1 }, { v: 2 }] })
          : response(200, { head: 2, latest: 3, versions: [{ v: 1 }, { v: 2 }, { v: 3 }] })
      }
      if (parsed.pathname === '/api/run') {
        const body = JSON.parse(options.body)
        const message = body.catalog_digest === `sha256:${'0'.repeat(64)}`
          ? 'catalog tool changed or confirmation digest is missing; refresh tools and confirm again'
          : 'drawing head changed after approval; refresh drawing state and confirm again'
        return response(409, { error: { error_code: 'BAD_PARAMS', message } })
      }
      return response(500, {})
    }
    await assert.rejects(
      () => provePinnedWriteRejections(config, browser, fetchImpl),
      /changed the drawing version state/,
    )
  })

  it('rejects a no-op camera gesture', () => {
    assert.throws(() => requireCameraMotion('1,2,3|4,5,6', '1,2,3|4,5,6'), /did not move/)
    assert.doesNotThrow(() => requireCameraMotion('1,2,3|4,5,6', '2,2,3|4,5,6'))
  })

  it('requires distinct staged tool identities, a known write capability, and request-bound change sets', () => {
    const config = validateConfig(environment(), true)
    const staged = (toolName, changeSetId) => ({
      receipt: { state: 'staged', change_set_id: changeSetId },
      tool: { name: toolName, capabilities: ['drawing.write'] },
    })
    assert.deepEqual(
      validateStagedAuthorResponse(staged('cat_tool', '11111111-1111-4111-8111-111111111111'), config.tenants[0]),
      { toolName: 'cat_tool', changeSetId: '11111111-1111-4111-8111-111111111111' },
    )
    assert.deepEqual(
      validateStagedAuthorResponse({
        ...staged('cat_tool', '11111111-1111-4111-8111-111111111111'),
        tool: { name: 'cat_tool', capabilities: ['drawing.read', 'drawing.write'] },
      }, config.tenants[0]),
      { toolName: 'cat_tool', changeSetId: '11111111-1111-4111-8111-111111111111' },
    )
    assert.throws(
      () => validateStagedAuthorResponse({
        ...staged('cat_tool', '11111111-1111-4111-8111-111111111111'),
        tool: { name: 'cat_tool', capabilities: ['drawing.read'] },
      }, config.tenants[0]),
      /did not stage one novel drawing.write tool/,
    )
    assert.throws(
      () => validateStagedAuthorResponse({
        ...staged('cat_tool', '11111111-1111-4111-8111-111111111111'),
        tool: { name: 'cat_tool', capabilities: ['drawing.write', 'network.read'] },
      }, config.tenants[0]),
      /did not stage one novel drawing.write tool/,
    )
    const results = [
      {
        _staged_tool_name: 'cat_tool',
        _staged_change_set_id: '11111111-1111-4111-8111-111111111111',
        staged_request_hash: sha256(config.tenants[0].request),
      },
      {
        _staged_tool_name: 'fox_tool',
        _staged_change_set_id: '22222222-2222-4222-8222-222222222222',
        staged_request_hash: sha256(config.tenants[1].request),
      },
    ]
    assert.doesNotThrow(() => requireDistinctStagedResults(results, config.tenants))
    assert.throws(
      () => requireDistinctStagedResults([
        results[0],
        { ...results[1], _staged_tool_name: results[0]._staged_tool_name },
      ], config.tenants),
      /bound to their requests/,
    )
    assert.throws(
      () => requireDistinctStagedResults([
        results[0],
        { ...results[1], staged_request_hash: sha256('wrong request') },
      ], config.tenants),
      /bound to their requests/,
    )
  })

  it('builds a receipt with hashes and image digests but no credential material', () => {
    const config = validateConfig(environment(), true)
    const receipt = buildReceipt(
      config,
      deploymentIdentity(),
      { tenant_header_override: 'denied' },
      [
        {
          label: 'A', executed: true, _workbench_id: 'secret-workbench-a',
          _run_request: { secret: TOKEN_A },
          _run_headers: { 'x-checkout-capability': 'secret-capability-a' },
          _job_id: 'secret-job-a',
          _publication_confirmation_id: 'secret-confirmation-a',
        },
        {
          label: 'B', executed: true, _workbench_id: 'secret-workbench-b',
          _run_request: { secret: TOKEN_B },
          _run_headers: { 'x-checkout-capability': 'secret-capability-b' },
          _job_id: 'secret-job-b',
          _publication_confirmation_id: 'secret-confirmation-b',
        },
      ],
      '2026-07-26T00:00:00Z',
      '2026-07-26T00:01:00Z',
    )
    const encoded = JSON.stringify(receipt)
    assert.equal(receipt.mode, 'execute')
    assert.equal(receipt.secrets_recorded, false)
    assert.ok(!encoded.includes(TOKEN_A))
    assert.ok(!encoded.includes(TOKEN_B))
    assert.ok(!encoded.includes('acceptance-a'))
    assert.ok(!encoded.includes('acceptance-b'))
    assert.ok(!encoded.includes('independent-secret-value'))
    assert.ok(!encoded.includes('secret-workbench'))
    assert.ok(!encoded.includes('secret-job'))
    assert.ok(!encoded.includes('secret-confirmation'))
    assert.ok(!encoded.includes('secret-capability'))
    assert.equal(receipt.images.web, DIGEST)
    assert.equal(receipt.external_evidence.status, 'required')
    assert.ok(receipt.external_evidence.requirements.some((item) => item.includes('restart')))
  })

  it('records the actual shared preflight workbench separately from planned drawings', () => {
    const config = validateConfig(environment(), false)
    const workbenchHash = sha256('rooftop_demo')
    const receipt = buildReceipt(
      config,
      deploymentIdentity(),
      { tenant_header_override: 'denied' },
      config.tenants.map((tenant) => ({
        label: tenant.label,
        executed: false,
        workbench_hash: workbenchHash,
        _workbench_id: 'rooftop_demo',
      })),
      '2026-07-26T00:00:00Z',
      '2026-07-26T00:01:00Z',
    )

    assert.equal(receipt.mode, 'preflight')
    assert.equal(receipt.tenants[0].drawing_hash, workbenchHash)
    assert.equal(receipt.tenants[1].drawing_hash, workbenchHash)
    assert.equal(
      receipt.tenants[0].planned_drawing_hash,
      sha256(config.tenants[0].plannedDrawingId),
    )
    assert.notEqual(
      receipt.tenants[0].planned_drawing_hash,
      receipt.tenants[1].planned_drawing_hash,
    )
  })

  it('contains no route interception API in the deployed browser driver', () => {
    const source = readFileSync(fileURLToPath(
      new URL('./deployed_authored_cad_acceptance.mjs', import.meta.url),
    ), 'utf8')
    assert.ok(!source.includes('.route('))
    assert.ok(!source.includes('leaf-proof.invalid/api/**'))
    assert.ok(!source.includes("getByRole('button', { name: 'Approve'"))
    // /try's preview copy is "Viewing v1 read-only" (ToolCast.jsx:1330); the
    // "…read-only preview" wording belongs to /app's VersionHistory, which
    // this driver never opens.
    assert.ok(source.includes("getByText(/Viewing v1 read-only/)"))
    assert.ok(source.includes("expired_approval: 'requires_external_evidence'"))
    assert.ok(!source.includes("waitUntil: 'networkidle'"))
    assert.equal(source.match(/waitUntil: 'domcontentloaded'/g)?.length, 2)
    assert.equal(source.match(/hasText: 'Backend ready'/g)?.length, 2)
    const completedVersion = source.indexOf("filter({ hasText: 'Version 2' })")
    const openView = source.indexOf('await openDrawingView(page)', completedVersion)
    const cameraControls = source.indexOf("getByTestId('camera-controls')", openView)
    const focus3d = source.indexOf("getByTestId('focus-3d').click()", cameraControls)
    const sculptureMount = source.indexOf(
      '.viewer-canvas[data-view-mode="panel-sculpture"][data-camera-position][data-camera-target]',
      focus3d,
    )
    const scopedCanvas = source.indexOf("cameraMount.locator('canvas')", sculptureMount)
    assert.ok(
      completedVersion >= 0
        && openView > completedVersion
        && cameraControls > openView
        && focus3d > cameraControls
        && sculptureMount > focus3d
        && scopedCanvas > sculptureMount,
    )
  })

  it('leaves focus view and reopens the Execution tab before Undo/Redo', () => {
    // Focus 3D applies `.tc-focus-hidden` (display:none) to both rails, and
    // Undo/Redo render only inside the Execution tab — so after the orbit
    // proof the driver must click focus-3d a second time and reselect the
    // Execution tab before touching either chip. Run 30806698693 starved on
    // exactly this: the orbit passed, then Undo waited 30s against a hidden
    // rail.
    const source = readFileSync(fileURLToPath(
      new URL('./deployed_authored_cad_acceptance.mjs', import.meta.url),
    ), 'utf8')
    const poseCheck = source.indexOf('requireCameraMotion(beforeCameraPose, afterCameraPose)')
    const focusExit = source.indexOf("getByTestId('focus-3d').click()", poseCheck)
    const executionTab = source.indexOf(
      "getByRole('tab', { name: 'Execution', exact: true })",
      focusExit,
    )
    const undoClick = source.indexOf(
      "getByRole('button', { name: 'Undo', exact: true })",
      executionTab,
    )
    // Undo is asynchronous: the driver must prove the undone head landed
    // (Version 1) before clicking Redo — a one-shot isEnabled() read raced
    // drawing.versionBusy and failed a live execute.
    const undoneHead = source.indexOf("hasText: 'Version 1'", undoClick)
    const redoClick = source.indexOf(
      "getByRole('button', { name: 'Redo', exact: true })",
      undoneHead,
    )
    assert.ok(
      poseCheck >= 0
        && focusExit > poseCheck
        && executionTab > focusExit
        && undoClick > executionTab
        && undoneHead > undoClick
        && redoClick > undoneHead,
    )
  })

  it('names the starving step and a scrubbed detail in the failure line', async () => {
    const errors = []
    console.error = (value) => errors.push(value)
    const result = await main(
      ['--receipt', 'must-not-exist.json'],
      environment({ LEAF_ACCEPTANCE_ENVIRONMENT: 'production' }),
    )
    assert.equal(result, 1)
    const failure = JSON.parse(errors[0])
    assert.equal(typeof failure.step, 'string')
    assert.equal(typeof failure.detail, 'string')
    assert.ok(failure.detail.includes('staging'))
  })

  it('scrubs token-shaped material from the failure detail', () => {
    const jwtish = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhY2NlcHRhbmNlIn0.c2lnbmF0dXJlLXNpZ25hdHVyZQ'
    const detail = scrubbedDetail(new Error(`waiting for locator with ${jwtish} embedded`))
    assert.ok(!detail.includes(jwtish))
    assert.ok(detail.includes('[token]'))
    assert.ok(scrubbedDetail(new Error('x'.repeat(2000))).length <= 700)
  })

  it('counts mutating API requests on both allowed browser origins', () => {
    const allowed = new Set([
      'https://staging.leaf.test',
      'https://staging-api.leaf.test',
    ])
    assert.equal(
      isMutatingApiRequest('https://staging.leaf.test/api/author/register', 'POST', allowed),
      true,
    )
    assert.equal(
      isMutatingApiRequest('https://staging-api.leaf.test/api/run', 'POST', allowed),
      true,
    )
    assert.equal(
      isMutatingApiRequest('https://staging.leaf.test/try', 'POST', allowed),
      false,
    )
    assert.equal(
      isMutatingApiRequest('https://staging-api.leaf.test/api/run', 'GET', allowed),
      false,
    )
    assert.equal(
      isMutatingApiRequest('https://other.leaf.test/api/run', 'POST', allowed),
      false,
    )
  })

  it('refuses production before requesting live identity or launching a browser', async () => {
    const errors = []
    console.error = (value) => errors.push(value)
    const result = await main(
      ['--receipt', 'must-not-exist.json'],
      environment({ LEAF_ACCEPTANCE_ENVIRONMENT: 'production' }),
    )
    assert.equal(result, 1)
    assert.equal(JSON.parse(errors[0]).check, 'production_target')
    assert.ok(!errors[0].includes(TOKEN_A))
  })
})
