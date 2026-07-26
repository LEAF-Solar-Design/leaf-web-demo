import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { afterEach, describe, it } from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  AcceptanceError,
  approveStagedPublication,
  buildReceipt,
  evaluateReadiness,
  main,
  proveExecutedDrawingIsolation,
  runApiPreflight,
  validateConfig,
  validateDeploymentManifest,
} from './deployed_authored_cad_acceptance.mjs'

const REVISION = 'f'.repeat(40)
const DIGEST = `sha256:${'a'.repeat(64)}`
const TOKEN_A = 'aaa.bbb.ccc'
const TOKEN_B = 'ddd.eee.fff'
const ORIGINAL_ERROR = console.error

function environment(overrides = {}) {
  const runId = 'run-20260726'
  return {
    LEAF_ACCEPTANCE_ENVIRONMENT: 'staging',
    LEAF_ACCEPTANCE_RUN_ID: runId,
    LEAF_ACCEPTANCE_WEB_URL: 'https://staging.leaf.test',
    LEAF_ACCEPTANCE_API_URL: 'https://staging-api.leaf.test',
    LEAF_ACCEPTANCE_ALLOWED_HOSTS: 'staging.leaf.test,staging-api.leaf.test',
    LEAF_ACCEPTANCE_EXPECTED_REVISION: REVISION,
    LEAF_ACCEPTANCE_IMAGE_MANIFEST: 'manifest.json',
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

function manifest(overrides = {}) {
  return {
    schema: 'leaf.deployment-image-manifest.v1',
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
  it('accepts only an explicit non-production HTTPS staging allowlist', () => {
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

  it('requires five immutable images from the exact source revision', () => {
    const config = validateConfig(environment())
    assert.equal(validateDeploymentManifest(manifest(), config).environment, 'staging')

    const mixed = manifest()
    mixed.services.broker = { image_digest: DIGEST, source_revision: 'e'.repeat(40) }
    assert.throws(() => validateDeploymentManifest(mixed, config), /broker was not built/)

    const missing = manifest()
    delete missing.services.harness
    assert.throws(() => validateDeploymentManifest(missing, config), /services must be exactly/)

    const mutable = manifest()
    mutable.services.web.image_digest = 'latest'
    assert.throws(() => validateDeploymentManifest(mutable, config), /immutable image digest/)
  })
})

describe('deployed authored CAD acceptance checks', () => {
  it('uses the independent approval route without a tenant JWT', async () => {
    const config = validateConfig(environment(), true)
    const calls = []
    const fetchImpl = async (url, options) => {
      calls.push({ url: String(url), options })
      return response(200, { confirmation_id: 'confirmation-one' })
    }
    const result = await approveStagedPublication(
      config,
      config.tenants[0],
      '11111111-1111-4111-8111-111111111111',
      fetchImpl,
    )
    assert.equal(result.status, 'approved')
    assert.equal(calls.length, 1)
    assert.equal(
      calls[0].url,
      'https://staging-api.leaf.test/internal/customization/confirm',
    )
    assert.equal(calls[0].options.headers.Authorization, undefined)
    assert.equal(calls[0].options.headers['X-Tenant-Id'], 'acceptance-a')
    assert.equal(
      calls[0].options.headers['X-Approval-Secret'],
      'independent-secret-value',
    )
    assert.deepEqual(
      JSON.parse(calls[0].options.body),
      { change_set_id: '11111111-1111-4111-8111-111111111111' },
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
    assert.equal(calls.length, 5)
    assert.ok(calls.every((call) => call.credentials === 'omit'))
    assert.ok(calls.every((call) => call.redirect === 'error'))
    assert.ok(calls.every((call) => [
      `Bearer ${TOKEN_A}`,
      `Bearer ${TOKEN_B}`,
    ].includes(call.authorization)))

    const permissive = async (url, options) => {
      const path = new URL(url).pathname
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

  it('rejects executed cross-tenant drawing bytes even behind a forged header', async () => {
    const config = validateConfig(environment(), true)
    const ownA = { tenant: 'a', shape: 'cat' }
    const ownB = { tenant: 'b', shape: 'fox' }
    const fetchImpl = async (url, options) => {
      const drawing = new URL(url).pathname.split('/').at(-2)
      const tokenA = options.headers.Authorization === `Bearer ${TOKEN_A}`
      if (drawing.endsWith('-a')) {
        return response(200, { intake: tokenA ? ownA : { tenant: 'b', shape: 'base' } })
      }
      return response(200, { intake: tokenA ? { tenant: 'a', shape: 'base' } : ownB })
    }
    const browser = [
      { label: 'A', executed: true },
      { label: 'B', executed: true },
    ]
    assert.deepEqual(
      await proveExecutedDrawingIsolation(config, browser, fetchImpl),
      { status: 'denied', distinct_result_hashes: true },
    )

    const leaking = async (url, options) => {
      const drawing = new URL(url).pathname.split('/').at(-2)
      if (drawing.endsWith('-b')) return response(200, { intake: ownB })
      return response(200, {
        intake: options.headers.Authorization === `Bearer ${TOKEN_A}`
          ? ownA
          : ownA,
      })
    }
    await assert.rejects(
      () => proveExecutedDrawingIsolation(config, browser, leaking),
      /read the other tenant's drawing bytes/,
    )
  })

  it('builds a receipt with hashes and image digests but no credential material', () => {
    const config = validateConfig(environment(), true)
    const receipt = buildReceipt(
      config,
      manifest(),
      { tenant_header_override: 'denied' },
      [
        { label: 'A', executed: true, _workbench_id: 'secret-workbench-a' },
        { label: 'B', executed: true, _workbench_id: 'secret-workbench-b' },
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
    assert.equal(receipt.images.web, DIGEST)
  })

  it('contains no route interception API in the deployed browser driver', () => {
    const source = readFileSync(fileURLToPath(
      new URL('./deployed_authored_cad_acceptance.mjs', import.meta.url),
    ), 'utf8')
    assert.ok(!source.includes('.route('))
    assert.ok(!source.includes('leaf-proof.invalid/api/**'))
    assert.ok(!source.includes("getByRole('button', { name: 'Approve'"))
  })

  it('refuses production before reading a manifest or launching a browser', async () => {
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
