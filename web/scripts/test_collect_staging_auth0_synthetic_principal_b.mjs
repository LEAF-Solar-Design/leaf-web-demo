import assert from 'node:assert/strict'
import { generateKeyPairSync } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  ACTIVATION_SCHEMA, AUDIENCE, AUTH0_ORIGIN, CLIENT_ID, PLAN_VERSION, SIGNED_SCHEMA,
  READINESS_SIGNED_SCHEMA, TARGET_ORIGIN, buildReadiness, canonical, collectPrincipalB, loadActivation, loadOutsideRepositorySigner,
  parseArgs, sha256, signEnvelope, validateActivationReceipt, validateSignedEnvelope,
} from './collect_staging_auth0_synthetic_principal_b.mjs'

const email = 'leaf-synthetic-b@example.test'
const tenant = 'synthetic-tenant-b'
const subject = 'auth0|synthetic-principal-b'
const sourceSha = 'a'.repeat(40)
const collectorSourceSha = 'b'.repeat(40)
const keyId = 'staging-collector-v1'
const activationAt = '2026-08-09T12:00:00Z'
const keyPair = generateKeyPairSync('ed25519')
const jwk = keyPair.publicKey.export({ format: 'jwk' })
const publicKeySha256 = sha256(Buffer.from(jwk.x, 'base64url'))

function principal(label) {
  return {
    email_sha256: sha256(label === 'B' ? email : 'leaf-synthetic-a@example.test'),
    user_sha256: sha256(label === 'B' ? subject : 'auth0|synthetic-principal-a'),
    tenant_sha256: sha256(label === 'B' ? tenant : 'synthetic-tenant-a'),
    blocked: false, email_verified: true, mfa_enrolled: label === 'A',
    created: label === 'B', mutated: label === 'B',
  }
}

function activationReceipt() {
  return {
    schema: ACTIVATION_SCHEMA, plan_version: PLAN_VERSION, mode: 'activate', source_sha: sourceSha,
    run: { id: '123', attempt: '1' }, verified_at: activationAt,
    authority: {
      tenant_domain_sha256: '1'.repeat(64), management_scopes_sha256: '2'.repeat(64),
      management_scope_count: 5, active_actions_sha256: '3'.repeat(64), active_action_count: 1,
      active_actions_source_sha: sourceSha,
    },
    principals: { A: principal('A'), B: principal('B') },
    phase_binding: {
      prior_receipt_sha256: '4'.repeat(64), prior_receipt_run_id: '111', prior_receipt_run_attempt: '1',
      prior_receipt_artifact_sha256: '7'.repeat(64),
      collector_readiness_sha256: '5'.repeat(64), collector_source_sha: collectorSourceSha,
      collector_readiness_challenge_sha256: '6'.repeat(64), collector_readiness_run_id: '222',
      collector_readiness_run_attempt: '1', collector_readiness_artifact_sha256: '8'.repeat(64),
      interactive_receipt_sha256: null, interactive_receipt_artifact_sha256: null,
      interactive_receipt_run_id: null, interactive_receipt_run_attempt: null,
      interactive_signing_key_id_sha256: sha256(keyId),
      interactive_signing_public_key_sha256: publicKeySha256, interactive_challenge_sha256: null,
    },
    safety: {
      principal_a_mutation: false, delete_authority: false, password_retained: false,
      raw_identifiers_retained: false, raw_api_body_retained: false,
      interactive_credentials_outside_workflow: true,
    },
  }
}

const config = {
  email, tenantId: tenant, collectorSourceSha, signingKeyId: keyId,
  signingPublicKeySha256: publicKeySha256,
}
const observedAt = new Date('2026-08-09T12:05:00Z')
assert.equal(validateActivationReceipt(activationReceipt(), config, observedAt).mode, 'activate')
for (const mutate of [
  (value) => { value.extra = 'raw-value' },
  (value) => { value.mode = 'finalize' },
  (value) => { value.principals.B.email_sha256 = '0'.repeat(64) },
  (value) => { value.principals.B.blocked = true },
  (value) => { value.phase_binding.collector_source_sha = 'c'.repeat(40) },
  (value) => { value.phase_binding.interactive_receipt_sha256 = '7'.repeat(64) },
]) {
  const value = structuredClone(activationReceipt())
  mutate(value)
  assert.throws(() => validateActivationReceipt(value, config, observedAt))
}

const keyDirectory = await mkdtemp(join(tmpdir(), 'leaf-staging-b-key-'))
const artifactDirectory = await mkdtemp(join(tmpdir(), 'leaf-staging-b-activation-'))
try {
  const keyPath = join(keyDirectory, 'collector.pem')
  await writeFile(keyPath, keyPair.privateKey.export({ format: 'pem', type: 'pkcs8' }), { mode: 0o600 })
  const signer = await loadOutsideRepositorySigner({ signingKeyPath: keyPath })
  assert.equal(signer.publicKeySha256, publicKeySha256)
  const preparationReceipt = activationReceipt()
  preparationReceipt.mode = 'prepare'
  preparationReceipt.principals.B.blocked = true
  for (const key of Object.keys(preparationReceipt.phase_binding)) preparationReceipt.phase_binding[key] = null
  const preparation = { receipt: preparationReceipt, digest: 'd'.repeat(64) }
  const readiness = buildReadiness(preparation, { ...config, priorArtifactSha256: 'e'.repeat(64) }, signer, observedAt)
  assert.equal(readiness.schema, READINESS_SIGNED_SCHEMA)
  assert.equal(readiness.payload.prior_receipt_sha256, preparation.digest)
  assert.equal(readiness.payload.prior_receipt_artifact_sha256, 'e'.repeat(64))

  const activationPath = join(artifactDirectory, 'activation.json')
  const activationBytes = `${canonical(activationReceipt())}\n`
  await writeFile(activationPath, activationBytes)
  const activation = await loadActivation(activationPath, config, observedAt)
  assert.equal(activation.digest, sha256(Buffer.from(activationBytes)))

  const parsedEnvironment = {
    LEAF_STAGING_SYNTHETIC_TENANT_B_EMAIL: process.env.LEAF_STAGING_SYNTHETIC_TENANT_B_EMAIL,
    LEAF_STAGING_SYNTHETIC_TENANT_B_ID: process.env.LEAF_STAGING_SYNTHETIC_TENANT_B_ID,
  }
  process.env.LEAF_STAGING_SYNTHETIC_TENANT_B_EMAIL = email
  process.env.LEAF_STAGING_SYNTHETIC_TENANT_B_ID = tenant
  process.env.GITHUB_SHA = collectorSourceSha
  process.env.LEAF_STAGING_SYNTHETIC_INTERACTIVE_ED25519_PRIVATE_KEY_PATH = keyPath
  process.env.LEAF_STAGING_SYNTHETIC_INTERACTIVE_ED25519_KEY_ID = keyId
  const parsed = parseArgs([
    '--mode', 'interactive', '--receipt', activationPath, '--output', join(artifactDirectory, 'proof.json'),
  ])
  assert.equal(parsed.email, email)
  assert.equal(parsed.tenantId, tenant)
  assert.throws(() => parseArgs(['--password', 'must-not-exist']))
  if (parsedEnvironment.LEAF_STAGING_SYNTHETIC_TENANT_B_EMAIL === undefined) delete process.env.LEAF_STAGING_SYNTHETIC_TENANT_B_EMAIL
  else process.env.LEAF_STAGING_SYNTHETIC_TENANT_B_EMAIL = parsedEnvironment.LEAF_STAGING_SYNTHETIC_TENANT_B_EMAIL
  if (parsedEnvironment.LEAF_STAGING_SYNTHETIC_TENANT_B_ID === undefined) delete process.env.LEAF_STAGING_SYNTHETIC_TENANT_B_ID
  else process.env.LEAF_STAGING_SYNTHETIC_TENANT_B_ID = parsedEnvironment.LEAF_STAGING_SYNTHETIC_TENANT_B_ID

  function token() {
    const header = Buffer.from(JSON.stringify({ alg: 'RS256' })).toString('base64url')
    const payload = Buffer.from(JSON.stringify({
      iss: `${AUTH0_ORIGIN}/`, aud: [AUDIENCE], azp: CLIENT_ID, sub: subject,
      'https://leafdesign.ai/tenant_id': tenant,
    })).toString('base64url')
    return `${header}.${payload}.signature-value`
  }

  function fakeBrowser() {
    const handlers = new Map()
    let currentUrl = `${TARGET_ORIGIN}/try`
    let evaluateCount = 0
    const emit = (url) => handlers.get('request')?.({ url: () => url })
    const page = {
      on(name, callback) { handlers.set(name, callback) },
      async goto(url) { currentUrl = url; emit(url) },
      async evaluate() {
        evaluateCount += 1
        if (evaluateCount === 1) return 401
        if (evaluateCount === 2) return { status: 200, token: token() }
        throw new Error(`unexpected evaluate ${evaluateCount}`)
      },
      getByRole() {
        return { first() { return this }, async click() {
          const query = new URLSearchParams({
            response_type: 'code', redirect_uri: TARGET_ORIGIN, audience: AUDIENCE,
            client_id: CLIENT_ID, state: 'state', code_challenge: 'challenge', code_challenge_method: 'S256',
          })
          emit(`${AUTH0_ORIGIN}/authorize?${query}`)
          emit(`${AUTH0_ORIGIN}/u/mfa-otp-enrollment`)
          currentUrl = `${TARGET_ORIGIN}/?code=code&state=state`
          emit(currentUrl)
          currentUrl = `${TARGET_ORIGIN}/try`
        } }
      },
      async waitForURL(predicate) { assert.equal(predicate(new URL(currentUrl)), true) },
      async waitForFunction() {},
    }
    return {
      emit,
      playwright: { chromium: { async launch(options) {
        assert.deepEqual(options, { headless: false })
        return { async newContext() { return { async newPage() { return page } } }, async close() {} }
      } } },
    }
  }

  const browser = fakeBrowser()
  const times = [
    '2026-08-09T12:00:10Z', '2026-08-09T12:00:20Z',
    '2026-08-09T12:00:30Z', '2026-08-09T12:00:31Z',
  ]
  const payload = await collectPrincipalB(config, activation, {
    playwright: browser.playwright,
    fetch: async (_url, request) => {
      const body = JSON.parse(request.body)
      assert.deepEqual(body, { client_id: CLIENT_ID, email, connection: 'Username-Password-Authentication' })
      return { status: 200 }
    },
    confirm: async () => { browser.emit(`${AUTH0_ORIGIN}/lo/reset?ticket=not-retained`) },
    notify: () => {},
    now: () => new Date(times.shift()),
  })
  assert.equal(payload.principal_b_user_sha256, sha256(subject))
  assert.equal(payload.challenge_sha256, sha256(`leaf-staging-auth0-interactive:${activation.digest}`))
  const envelope = signEnvelope(payload, signer)
  assert.equal(validateSignedEnvelope(envelope, activation, config, signer, new Date('2026-08-09T12:01:00Z')).schema, SIGNED_SCHEMA)
  const retained = canonical(envelope)
  assert.equal(retained.includes(email), false)
  assert.equal(retained.includes(tenant), false)
  assert.equal(retained.includes(subject), false)
  assert.equal(retained.includes('ticket=not-retained'), false)

  const tampered = structuredClone(envelope)
  tampered.payload.pkce_login_at = '2026-08-09T12:00:29Z'
  assert.throws(() => validateSignedEnvelope(tampered, activation, config, signer, observedAt), /signature/)

  const replayActivation = { ...activation, digest: '9'.repeat(64) }
  assert.throws(() => validateSignedEnvelope(envelope, replayActivation, config, signer, observedAt), /activation or challenge/)

  const attacker = generateKeyPairSync('ed25519')
  const forged = signEnvelope(payload, { privateKey: attacker.privateKey })
  assert.throws(() => validateSignedEnvelope(forged, activation, config, signer, observedAt), /signature/)
} finally {
  await rm(keyDirectory, { recursive: true, force: true })
  await rm(artifactDirectory, { recursive: true, force: true })
}

const source = readFileSync(new URL('./collect_staging_auth0_synthetic_principal_b.mjs', import.meta.url), 'utf8')
assert.equal(source.includes('.route('), false)
assert.equal(source.includes('--password'), false)
assert.equal(source.includes('--mfa'), false)
assert.equal(source.includes('passwords_recorded'), false)
assert.equal(source.includes('mfa_values_recorded'), false)

console.log('STAGING_AUTH0_PRINCIPAL_B_COLLECTOR_TESTS_OK')
