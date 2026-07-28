import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { afterEach, describe, it } from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  AcceptanceError,
  approveIsolatedStagedPublication,
  buildReceipt,
  evaluateReadiness,
  evaluateDeploymentIdentity,
  main,
  isMutatingApiRequest,
  proveExecutedAuthorityIsolation,
  proveExecutedDrawingIsolation,
  provePinnedWriteRejections,
  requireCameraMotion,
  requireDistinctStagedResults,
  runApiPreflight,
  validateConfig,
  validateStagedAuthorResponse,
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

  it('accepts only 403 or 404 for forged cross-tenant drawing reads', async () => {
    const config = validateConfig(environment(), true)
    const ownA = { tenant: 'a', shape: 'cat' }
    const ownB = { tenant: 'b', shape: 'fox' }
    const fetchImpl = async (url, options) => {
      const drawing = new URL(url).pathname.split('/').at(-2)
      const tokenA = options.headers.Authorization === `Bearer ${TOKEN_A}`
      const ownDrawing = (drawing.endsWith('-a') && tokenA) || (drawing.endsWith('-b') && !tokenA)
      return ownDrawing ? response(200, { intake: tokenA ? ownA : ownB }) : response(404, {})
    }
    const browser = [
      { label: 'A', executed: true },
      { label: 'B', executed: true },
    ]
    assert.deepEqual(
      await proveExecutedDrawingIsolation(config, browser, fetchImpl),
      { status: 'denied', distinct_result_hashes: true },
    )

    const sanitizedButPermissive = async (url, options) => {
      const drawing = new URL(url).pathname.split('/').at(-2)
      const tokenA = options.headers.Authorization === `Bearer ${TOKEN_A}`
      const ownDrawing = (drawing.endsWith('-a') && tokenA) || (drawing.endsWith('-b') && !tokenA)
      return response(200, { intake: ownDrawing ? (tokenA ? ownA : ownB) : { redacted: true } })
    }
    await assert.rejects(
      () => proveExecutedDrawingIsolation(config, browser, sanitizedButPermissive),
      /cross-tenant drawing read returned HTTP 200/,
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

    const permissive = async (url) => {
      const path = new URL(url).pathname
      if (path.endsWith('/versions')) return response(200, { head: 2 })
      return response(202, { job_id: 'unexpected' })
    }
    await assert.rejects(
      () => provePinnedWriteRejections(config, browser, permissive),
      /unexpected HTTP 202/,
    )
  })

  it('rejects a no-op camera gesture', () => {
    assert.throws(() => requireCameraMotion('1,2,3|4,5,6', '1,2,3|4,5,6'), /did not move/)
    assert.doesNotThrow(() => requireCameraMotion('1,2,3|4,5,6', '2,2,3|4,5,6'))
  })

  it('requires distinct staged tool identities, exact capability, and request-bound change sets', () => {
    const config = validateConfig(environment(), true)
    const staged = (toolName, changeSetId) => ({
      receipt: { state: 'staged', change_set_id: changeSetId },
      tool: { name: toolName, capabilities: ['drawing.write'] },
    })
    assert.deepEqual(
      validateStagedAuthorResponse(staged('cat_tool', '11111111-1111-4111-8111-111111111111'), config.tenants[0]),
      { toolName: 'cat_tool', changeSetId: '11111111-1111-4111-8111-111111111111' },
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

  it('contains no route interception API in the deployed browser driver', () => {
    const source = readFileSync(fileURLToPath(
      new URL('./deployed_authored_cad_acceptance.mjs', import.meta.url),
    ), 'utf8')
    assert.ok(!source.includes('.route('))
    assert.ok(!source.includes('leaf-proof.invalid/api/**'))
    assert.ok(!source.includes("getByRole('button', { name: 'Approve'"))
    assert.ok(source.includes("getByText(/Viewing v1.*read-only preview/)"))
    assert.ok(source.includes("expired_approval: 'requires_external_evidence'"))
  })

  it('loads the workbench shell without contacting a third-party origin', () => {
    const index = readFileSync(fileURLToPath(
      new URL('../index.html', import.meta.url),
    ), 'utf8')
    const externalResources = Array.from(
      index.matchAll(/\b(?:href|src)="(https?:\/\/[^"]+)"/g),
      (match) => match[1],
    )
    assert.deepEqual(externalResources, [])
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
