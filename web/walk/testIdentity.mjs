import { generateKeyPairSync, randomBytes, sign } from 'node:crypto'
import { writeFileSync } from 'node:fs'
import { join } from 'node:path'

export function createTestIdentity({ root, runId = randomBytes(16).toString('hex') }) {
  if (typeof runId !== 'string' || !/^[a-z0-9-]{1,64}$/.test(runId)) throw new TypeError('Invalid test identity runId')
  const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 })
  const { kty, n, e } = publicKey.export({ format: 'jwk' })
  const kid = randomBytes(16).toString('hex')
  const jwksPath = join(root, 'test-identity-jwks.json')
  writeFileSync(jwksPath, JSON.stringify({ keys: [{ kty, n, e, alg: 'RS256', use: 'sig', kid }] }) + '\n')
  const issuer = `https://test-identity.leaf.invalid/${runId}/`
  const audience = 'https://test-identity.leaf.invalid/api'
  const claimNamespace = 'https://test-identity.leaf.invalid/'
  const env = {
    LEAF_AUTH_LIVE: '1', LEAF_RUNTIME_ENV: 'local', LEAF_AUTH0_JWKS_FILE: jwksPath,
    LEAF_AUTH0_ISSUER: issuer, LEAF_AUTH0_AUDIENCE: audience, LEAF_TENANT_CLAIM_NS: claimNamespace,
    LEAF_BROKER_SECRET: randomBytes(32).toString('hex'), LEAF_AUTHORED_EXECUTION: '0',
  }
  function mint({ subject, tenantId, orgId, tier, lifetimeSeconds = 900, now = Math.floor(Date.now() / 1000) }) {
    if (typeof subject !== 'string' || !subject || subject.startsWith('leaf-proof-project|')) throw new TypeError('Invalid test identity subject')
    if (!Number.isInteger(lifetimeSeconds) || lifetimeSeconds < 60 || lifetimeSeconds > 3600) throw new TypeError('Invalid test identity lifetimeSeconds')
    const claims = { iss: issuer, aud: audience, sub: subject, iat: now, nbf: now, exp: now + lifetimeSeconds }
    for (const [name, value] of [['tenant_id', tenantId], ['org_id', orgId], ['tier', tier]]) {
      if (value !== undefined) claims[claimNamespace + name] = value
    }
    const encode = (value) => Buffer.from(JSON.stringify(value)).toString('base64url')
    const input = `${encode({ alg: 'RS256', kid })}.${encode(claims)}`
    return `${input}.${sign('RSA-SHA256', Buffer.from(input), privateKey).toString('base64url')}`
  }
  return { issuer, audience, claimNamespace, jwksPath, env, mint, runId }
}
