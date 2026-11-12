import assert from 'node:assert/strict'
import { createPublicKey, verify } from 'node:crypto'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { createTestIdentity } from './testIdentity.mjs'

test('test identity writes only a public key and signs verifiable scoped tokens', async () => {
  const root = await mkdtemp(join(tmpdir(), 'leaf-test-identity-'))
  try {
    const identity = createTestIdentity({ root, runId: 'unit-test' })
    assert.equal(identity.issuer, 'https://test-identity.leaf.invalid/unit-test/')
    assert.equal(identity.audience, 'https://test-identity.leaf.invalid/api')
    assert.equal(identity.claimNamespace, 'https://test-identity.leaf.invalid/')
    assert.deepEqual(identity.env, {
      LEAF_AUTH_LIVE: '1', LEAF_RUNTIME_ENV: 'local', LEAF_AUTH0_JWKS_FILE: identity.jwksPath,
      LEAF_AUTH0_ISSUER: identity.issuer, LEAF_AUTH0_AUDIENCE: identity.audience,
      LEAF_TENANT_CLAIM_NS: identity.claimNamespace, LEAF_BROKER_SECRET: identity.env.LEAF_BROKER_SECRET,
      LEAF_AUTHORED_EXECUTION: '0',
    })
    assert.match(identity.env.LEAF_BROKER_SECRET, /^[a-f0-9]{64}$/)
    const jwks = JSON.parse(await readFile(identity.jwksPath, 'utf8'))
    assert.equal(jwks.keys.length, 1)
    for (const name of ['d', 'p', 'q', 'dp', 'dq', 'qi']) assert.equal(Object.hasOwn(jwks.keys[0], name), false)
    assert.deepEqual(Object.keys(jwks.keys[0]).sort(), ['alg', 'e', 'kid', 'kty', 'n', 'use'])
    const token = identity.mint({ subject: 'test-identity|unit', tenantId: 'org-1', orgId: 'org-1', tier: 'hosted_free', now: 1000 })
    const [header, payload, signature] = token.split('.')
    assert.equal(verify('RSA-SHA256', Buffer.from(`${header}.${payload}`), createPublicKey({ key: jwks.keys[0], format: 'jwk' }), Buffer.from(signature, 'base64url')), true)
    assert.deepEqual(JSON.parse(Buffer.from(header, 'base64url')), { alg: 'RS256', kid: jwks.keys[0].kid })
    assert.deepEqual(JSON.parse(Buffer.from(payload, 'base64url')), {
      iss: identity.issuer, aud: identity.audience, sub: 'test-identity|unit', iat: 1000, nbf: 1000, exp: 1900,
      [identity.claimNamespace + 'tenant_id']: 'org-1', [identity.claimNamespace + 'org_id']: 'org-1', [identity.claimNamespace + 'tier']: 'hosted_free',
    })
    const bootstrap = JSON.parse(Buffer.from(identity.mint({ subject: 'test-identity|bootstrap' }).split('.')[1], 'base64url'))
    for (const name of ['tenant_id', 'org_id', 'tier']) assert.equal(Object.hasOwn(bootstrap, identity.claimNamespace + name), false)
    const explicitNull = JSON.parse(Buffer.from(identity.mint({ subject: 'test-identity|null', orgId: null }).split('.')[1], 'base64url'))
    assert.equal(explicitNull[identity.claimNamespace + 'org_id'], null)
    const second = createTestIdentity({ root, runId: 'unit-test' })
    assert.equal(second.issuer, identity.issuer)
    assert.equal(second.audience, identity.audience)
    const otherKey = JSON.parse(await readFile(second.jwksPath, 'utf8')).keys[0]
    assert.notEqual(otherKey.n, jwks.keys[0].n)
    assert.equal(verify('RSA-SHA256', Buffer.from(`${header}.${payload}`), createPublicKey({ key: otherKey, format: 'jwk' }), Buffer.from(signature, 'base64url')), false)
  } finally { await rm(root, { recursive: true, force: true }) }
})

test('test identity refuses invalid run IDs, subjects and lifetimes', async () => {
  const root = await mkdtemp(join(tmpdir(), 'leaf-test-identity-'))
  try {
    for (const runId of ['', 'UPPER', '../escape', 'a'.repeat(65), null, 1]) assert.throws(() => createTestIdentity({ root, runId }), TypeError)
    const identity = createTestIdentity({ root })
    assert.match(identity.runId, /^[a-z0-9-]{1,64}$/)
    for (const subject of ['', undefined, null, 1, {}, 'leaf-proof-project|reserved']) assert.throws(() => identity.mint({ subject }), TypeError)
    for (const lifetimeSeconds of [59, 3601, 60.5, '900', null, NaN]) assert.throws(() => identity.mint({ subject: 'test-identity|unit', lifetimeSeconds }), TypeError)
    for (const lifetimeSeconds of [60, 3600]) assert.equal(identity.mint({ subject: 'test-identity|unit', lifetimeSeconds }).split('.').length, 3)
  } finally { await rm(root, { recursive: true, force: true }) }
})
