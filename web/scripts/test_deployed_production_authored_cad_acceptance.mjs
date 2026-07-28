import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { after, describe, it } from 'node:test'
import { fileURLToPath } from 'node:url'
import {
  createLocalJWKSet,
  exportJWK,
  generateKeyPair,
  SignJWT,
} from 'jose'

import {
  parseProductionArgs,
  validateProductionConfig,
  verifyProductionTenantTokens,
} from './deployed_production_authored_cad_acceptance.mjs'
import {
  AcceptanceError,
  evaluateDeploymentIdentity,
  validateConfig as validateStagingConfig,
} from './deployed_authored_cad_acceptance.mjs'

const REVISION = 'f'.repeat(40)
const RUN_ID = 'production-20260728'
const ISSUER = 'https://leafautomation.us.auth0.com/'
const AUDIENCE = 'https://api.leafdesign.ai'
const CLAIM_NS = 'https://leafdesign.ai/'
const SECRET_DIR = mkdtempSync(join(tmpdir(), 'leaf-production-acceptance-'))
const SECRET_FILE = join(SECRET_DIR, 'publication-secret')
const SECRET_VALUE = 'independent-secret-value\n'
writeFileSync(SECRET_FILE, SECRET_VALUE, { mode: 0o600 })
after(() => rmSync(SECRET_DIR, { recursive: true, force: true }))

function environment(overrides = {}) {
  return {
    LEAF_ACCEPTANCE_ENVIRONMENT: 'production',
    LEAF_ACCEPTANCE_RUN_ID: RUN_ID,
    LEAF_ACCEPTANCE_WEB_URL: 'https://leaf-platform-web.vercel.app',
    LEAF_ACCEPTANCE_API_URL: 'https://platform.leafdesign.ai',
    LEAF_ACCEPTANCE_ALLOWED_HOSTS: 'leaf-platform-web.vercel.app,platform.leafdesign.ai',
    LEAF_ACCEPTANCE_EXPECTED_REVISION: REVISION,
    LEAF_ACCEPTANCE_PRODUCTION_CONFIRMATION:
      `production-authored-acceptance:${REVISION}:${RUN_ID}`,
    LEAF_ACCEPTANCE_PUBLICATION_APPROVAL_SECRET_FILE: SECRET_FILE,
    LEAF_ACCEPTANCE_TENANT_A_ID: 'production_acceptance_a',
    LEAF_ACCEPTANCE_TENANT_A_JWT: 'aaa.bbb.ccc',
    LEAF_ACCEPTANCE_TENANT_A_DRAWING_ID: `production-acceptance-${RUN_ID}-a`,
    LEAF_ACCEPTANCE_TENANT_A_REQUEST:
      `Production acceptance run ${RUN_ID} tenant A: create a drawing tool that adds a centered test prism.`,
    LEAF_ACCEPTANCE_TENANT_B_ID: 'production_acceptance_b',
    LEAF_ACCEPTANCE_TENANT_B_JWT: 'ddd.eee.fff',
    LEAF_ACCEPTANCE_TENANT_B_DRAWING_ID: `production-acceptance-${RUN_ID}-b`,
    LEAF_ACCEPTANCE_TENANT_B_REQUEST:
      `Production acceptance run ${RUN_ID} tenant B: create a drawing tool that adds an offset test cylinder.`,
    ...overrides,
  }
}

async function signer() {
  const { publicKey, privateKey } = await generateKeyPair('RS256')
  const publicJwk = await exportJWK(publicKey)
  publicJwk.kid = 'acceptance-test-key'
  publicJwk.alg = 'RS256'
  publicJwk.use = 'sig'
  return {
    privateKey,
    keyResolver: createLocalJWKSet({ keys: [publicJwk] }),
  }
}

async function token(
  privateKey,
  tenantId,
  {
    now,
    subject = tenantId,
    jti = `${tenantId}-jti`,
    tenantClass = 'non_customer_acceptance',
    issuer = ISSUER,
    audience = AUDIENCE,
    issuedAt = now - 60,
    expiresAt = now + 30 * 60,
  } = {},
) {
  return new SignJWT({
    [`${CLAIM_NS}tenant_id`]: tenantId,
    [`${CLAIM_NS}tenant_class`]: tenantClass,
  })
    .setProtectedHeader({ alg: 'RS256', kid: 'acceptance-test-key' })
    .setIssuer(issuer)
    .setAudience(audience)
    .setSubject(subject)
    .setJti(jti)
    .setIssuedAt(issuedAt)
    .setExpirationTime(expiresAt)
    .sign(privateKey)
}

describe('production authored CAD acceptance target policy', () => {
  it('accepts only explicit execute against the exact production origin', () => {
    const config = validateProductionConfig(environment(), 'execute')
    assert.equal(config.environment, 'production')
    assert.equal(config.webUrl, 'https://leaf-platform-web.vercel.app')
    assert.equal(config.apiUrl, 'https://platform.leafdesign.ai')
    assert.equal(config.publicationApprovalSecret, SECRET_VALUE)

    for (const overrides of [
      { LEAF_ACCEPTANCE_ENVIRONMENT: 'staging' },
      { LEAF_ACCEPTANCE_WEB_URL: 'http://leaf-platform-web.vercel.app' },
      { LEAF_ACCEPTANCE_WEB_URL: 'https://leaf-platform-web.vercel.app/' },
      { LEAF_ACCEPTANCE_WEB_URL: 'https://leaf-platform-web.vercel.app.' },
      { LEAF_ACCEPTANCE_WEB_URL: 'https://leaf-platform-web.vercel.app:8443' },
      { LEAF_ACCEPTANCE_WEB_URL: 'https://platform.leafdesign.ai' },
      { LEAF_ACCEPTANCE_API_URL: 'https://api.leafdesign.ai' },
      { LEAF_ACCEPTANCE_API_URL: 'https://platform-staging.leafdesign.ai' },
      { LEAF_ACCEPTANCE_ALLOWED_HOSTS: 'platform.leafdesign.ai,leaf-platform-web.vercel.app' },
      { LEAF_ACCEPTANCE_ALLOWED_HOSTS: 'leaf-platform-web.vercel.app' },
      { LEAF_ACCEPTANCE_ALLOWED_HOSTS: 'localhost' },
      { LEAF_ACCEPTANCE_ALLOWED_HOSTS: 'leaf-proof.invalid' },
      { LEAF_ACCEPTANCE_PRODUCTION_CONFIRMATION: 'accept-production' },
    ]) {
      assert.throws(
        () => validateProductionConfig(environment(overrides), 'execute'),
        AcceptanceError,
      )
    }
    assert.throws(
      () => validateProductionConfig(environment(), ''),
      /explicit preflight or execute/,
    )
    assert.throws(
      () => validateProductionConfig(environment({
        LEAF_ACCEPTANCE_PUBLICATION_APPROVAL_SECRET_FILE: join(SECRET_DIR, 'missing'),
      }), 'execute'),
      /must be readable/,
    )
  })

  it('supports explicit read-only preflight without the publication credential', () => {
    const config = validateProductionConfig(environment({
      LEAF_ACCEPTANCE_PUBLICATION_APPROVAL_SECRET_FILE: '',
    }), 'preflight')
    assert.equal(config.mode, 'preflight')
    assert.equal(config.execute, false)
    assert.equal(config.publicationApprovalSecret, '')
    assert.deepEqual(parseProductionArgs(['--preflight', '--receipt', 'proof.json']), {
      mode: 'preflight',
      receipt: 'proof.json',
      state: null,
      verifyState: null,
    })
    assert.deepEqual(
      parseProductionArgs(['--execute', '--receipt', 'proof.json', '--state', 'private.json']),
      { mode: 'execute', receipt: 'proof.json', state: 'private.json', verifyState: null },
    )
    assert.deepEqual(
      parseProductionArgs(['--preflight', '--receipt', 'proof.json', '--verify-state', 'private.json']),
      { mode: 'preflight', receipt: 'proof.json', state: null, verifyState: 'private.json' },
    )
    assert.throws(
      () => parseProductionArgs(['--preflight', '--execute', '--receipt', 'proof.json']),
      /choose one production mode/,
    )
    const source = readFileSync(fileURLToPath(
      new URL('./deployed_authored_cad_acceptance.mjs', import.meta.url),
    ), 'utf8')
    const preflight = source.slice(
      source.indexOf('export async function runApiPreflight'),
      source.indexOf('async function requestStagedPublicationApproval'),
    )
    assert.ok(preflight.includes("'/api/deployment-identity'"))
    assert.ok(preflight.includes("'/api/ready'"))
    assert.ok(preflight.includes("'/api/tenant/claude-grant'"))
    for (const forbidden of [
      '/api/session', '/api/author', '/api/run', '/api/drawings/',
      '/internal/customization/confirm', 'runBrowserAcceptance',
    ]) {
      assert.ok(!preflight.includes(forbidden), `preflight contains ${forbidden}`)
    }
    const productionSource = readFileSync(fileURLToPath(
      new URL('./deployed_production_authored_cad_acceptance.mjs', import.meta.url),
    ), 'utf8')
    assert.ok(productionSource.includes('production execute requires --state'))
    const privateState = productionSource.slice(
      productionSource.indexOf('function buildPrivateState'),
      productionSource.indexOf('function writeReceipt'),
    )
    for (const forbidden of ['jwt', 'tenant.id', 'publicationApprovalSecret', '_run_request', '_run_headers']) {
      assert.ok(!privateState.includes(forbidden), `private state contains ${forbidden}`)
    }
  })

  it('requires exact run-scoped drawings and source-fixed distinct requests', () => {
    assert.throws(
      () => validateProductionConfig(environment({
        LEAF_ACCEPTANCE_TENANT_A_DRAWING_ID: 'customer-drawing',
      }), 'execute'),
      /exact production acceptance drawing/,
    )
    assert.throws(
      () => validateProductionConfig(environment({
        LEAF_ACCEPTANCE_TENANT_A_REQUEST: `Production acceptance run ${RUN_ID} customer prompt`,
      }), 'execute'),
      /source-fixed/,
    )
    assert.throws(
      () => validateProductionConfig(environment({
        LEAF_ACCEPTANCE_TENANT_B_ID: 'production_acceptance_a',
      }), 'execute'),
      /must be distinct/,
    )
  })

  it('does not weaken the staging production denylist', () => {
    assert.throws(
      () => validateStagingConfig(environment()),
      /must be exactly staging/,
    )
    assert.throws(
      () => validateStagingConfig({
        ...environment(),
        LEAF_ACCEPTANCE_ENVIRONMENT: 'staging',
      }),
      /production hostname/,
    )
  })

  it('accepts production deployment identity only when requested explicitly', () => {
    const identity = {
      schema: 'leaf.deployment-identity.v1',
      environment: 'production',
      source_revision: REVISION,
      services: Object.fromEntries(
        ['app', 'broker', 'canonical-worker', 'harness', 'web'].map((name) => [
          name,
          { image_digest: `sha256:${'a'.repeat(64)}`, source_revision: REVISION },
        ]),
      ),
    }
    assert.equal(
      evaluateDeploymentIdentity(identity, REVISION, 'production').environment,
      'production',
    )
    assert.throws(
      () => evaluateDeploymentIdentity(identity, REVISION),
      /not for staging/,
    )
  })
})

describe('production authored CAD acceptance tenant classification', () => {
  it('verifies two signed, short-lived, classified and distinct tokens', async () => {
    const now = 1_800_000_000
    const { privateKey, keyResolver } = await signer()
    const jwtA = await token(privateKey, 'production_acceptance_a', { now })
    const jwtB = await token(privateKey, 'production_acceptance_b', { now })
    const config = validateProductionConfig(environment({
      LEAF_ACCEPTANCE_TENANT_A_JWT: jwtA,
      LEAF_ACCEPTANCE_TENANT_B_JWT: jwtB,
    }), 'execute')
    assert.deepEqual(
      await verifyProductionTenantTokens(config, { keyResolver, now }),
      { classification: 'non_customer_acceptance', tenants: ['A', 'B'] },
    )
  })

  it('rejects wrong signature, issuer, audience, class, tenant and lifetime', async () => {
    const now = 1_800_000_000
    const { privateKey, keyResolver } = await signer()
    const other = await signer()
    const validB = await token(privateKey, 'production_acceptance_b', { now })
    const cases = [
      token(other.privateKey, 'production_acceptance_a', { now }),
      token(privateKey, 'production_acceptance_a', { now, issuer: 'https://wrong.example/' }),
      token(privateKey, 'production_acceptance_a', { now, audience: 'https://wrong.example' }),
      token(privateKey, 'production_acceptance_a', { now, tenantClass: 'customer' }),
      token(privateKey, 'different_tenant', { now }),
      token(privateKey, 'production_acceptance_a', { now, expiresAt: now + 91 * 60 }),
      token(privateKey, 'production_acceptance_a', {
        now,
        issuedAt: now - 3 * 60 * 60,
        expiresAt: now + 30 * 60,
      }),
    ]
    for (const candidate of cases) {
      const config = validateProductionConfig(environment({
        LEAF_ACCEPTANCE_TENANT_A_JWT: await candidate,
        LEAF_ACCEPTANCE_TENANT_B_JWT: validB,
      }), 'execute')
      await assert.rejects(
        () => verifyProductionTenantTokens(config, { keyResolver, now }),
        AcceptanceError,
      )
    }
  })

  it('rejects token reuse through shared subject or jti claims', async () => {
    const now = 1_800_000_000
    const { privateKey, keyResolver } = await signer()
    for (const duplicate of [
      { subject: 'shared-subject' },
      { jti: 'shared-jti' },
    ]) {
      const jwtA = await token(privateKey, 'production_acceptance_a', { now, ...duplicate })
      const jwtB = await token(privateKey, 'production_acceptance_b', { now, ...duplicate })
      const config = validateProductionConfig(environment({
        LEAF_ACCEPTANCE_TENANT_A_JWT: jwtA,
        LEAF_ACCEPTANCE_TENANT_B_JWT: jwtB,
      }), 'execute')
      await assert.rejects(
        () => verifyProductionTenantTokens(config, { keyResolver, now }),
        /distinct subject and jti/,
      )
    }
  })

  it('contains no staging target relaxation or browser interception', () => {
    const production = readFileSync(fileURLToPath(
      new URL('./deployed_production_authored_cad_acceptance.mjs', import.meta.url),
    ), 'utf8')
    const staging = readFileSync(fileURLToPath(
      new URL('./deployed_authored_cad_acceptance.mjs', import.meta.url),
    ), 'utf8')
    assert.ok(!production.includes('.route('))
    assert.ok(!production.includes('platform-staging.leafdesign.ai'))
    assert.ok(staging.includes("LEAF_ACCEPTANCE_ENVIRONMENT') !== 'staging'"))
    assert.ok(staging.includes('the acceptance allowlist cannot contain a production hostname'))
  })
})
