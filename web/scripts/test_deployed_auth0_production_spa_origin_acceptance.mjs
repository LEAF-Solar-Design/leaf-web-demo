import assert from 'node:assert/strict'
import { generateKeyPairSync, verify } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  AUDIENCE,
  CLIENT_ID,
  ISSUER,
  SCHEMA,
  AUTH0_CDN_ORIGIN,
  SIGNED_SCHEMA,
  TARGET_ORIGIN,
  canonical,
  collectInteractiveProof,
  deriveCollectorCode,
  loadOutsideRepositorySigner,
  parseArgs,
  sha256,
  signInteractiveProof,
  validateInteractiveProof,
} from './deployed_auth0_production_spa_origin_acceptance.mjs'

const rawSubject = 'auth0|interactive-principal'
const rawTenant = 'interactive-tenant'
const assetSha = '9'.repeat(64)
const config = {
  challenge: 'A'.repeat(43),
  preparationPayloadSha256: '1'.repeat(64),
  sourceSha: 'a'.repeat(40),
  targetOrigin: TARGET_ORIGIN,
  signingKeyId: 'production-visible-browser-v1',
  deployedWebSource: {
    schema: 'leaf.production-auth0-spa-origin-web-source.v1', environment: 'production', target_origin: TARGET_ORIGIN,
    service_name: 'leaf-platform-web',
    task_definition_arn: 'arn:aws:ecs:us-east-1:807034087062:task-definition/leaf-platform-web:42',
    task_arn: `arn:aws:ecs:us-east-1:807034087062:task/leaf-automation-production/${'a'.repeat(32)}`,
    task_private_ipv4_sha256: '7'.repeat(64),
    target_group_arn: 'arn:aws:elasticloadbalancing:us-east-1:807034087062:targetgroup/leaf-prod-platform-web/abcdef1234567890',
    target_port: 8080,
    target_health: 'healthy',
    image_digest: `sha256:${'8'.repeat(64)}`, source_revision: 'a'.repeat(40),
    asset_path: '/assets/index-proof.js', asset_sha256: assetSha,
  },
  expectedPrincipalA: { subject_sha256: sha256(rawSubject), tenant_id_sha256: sha256(rawTenant), reviewed: true },
  collectorCode: {
    repository: 'LEAF-Solar-Design/leaf-web-demo', source_sha: 'b'.repeat(40),
    script_path: 'web/scripts/deployed_auth0_production_spa_origin_acceptance.mjs', script_sha256: 'c'.repeat(64),
  },
}

const signing = generateKeyPairSync('ed25519')
const signingJwk = signing.publicKey.export({ format: 'jwk' })
config.signingPublicKeySha256 = sha256(Buffer.from(signingJwk.x, 'base64url'))

function cycle(sequence) {
  return {
    sequence,
    authorize_request: {
      origin_exact: true,
      response_type_code: true,
      redirect_uri_exact: true,
      audience_exact: true,
      client_id_exact: true,
      state_present: true,
      code_challenge_present: true,
      code_challenge_method_s256: true,
    },
    callback: { origin_exact: true, code_present: true, state_present: true },
  }
}

function proof() {
  return {
    schema: SCHEMA,
    plan_version: '1.32',
    environment: 'production',
    target_origin: TARGET_ORIGIN,
    expected_source_revision: config.sourceSha,
    observed_source_revision: config.sourceSha,
    issuer: ISSUER,
    audience: AUDIENCE,
    client_id_sha256: sha256(CLIENT_ID),
    deployed_web_source: { ...config.deployedWebSource },
    expected_principal_a: { ...config.expectedPrincipalA },
    preparation: { challenge_sha256: sha256(config.challenge), payload_sha256: config.preparationPayloadSha256 },
    authorization_cycles: [cycle(1), cycle(2)],
    logout: { auth0_round_trip: true, return_origin_exact: true, local_token_absent: true },
    sessions: {
      before_login_refusal_status: 401,
      first: { api_status: 200, subject_sha256: sha256(rawSubject), tenant_id_sha256: sha256(rawTenant), access_token_sha256: '4'.repeat(64), jwt_alg_rs256: true },
      after_logout_refusal_status: 401,
      second: { api_status: 200, subject_sha256: sha256(rawSubject), tenant_id_sha256: sha256(rawTenant), access_token_sha256: '5'.repeat(64), jwt_alg_rs256: true },
    },
    route_interceptions: 0,
    observed_origins: [TARGET_ORIGIN, new URL(ISSUER).origin].sort(),
    operator_interactive: true,
    started_at: '2026-08-09T00:00:00.000Z',
    completed_at: '2026-08-09T00:05:00.000Z',
    raw_tokens_recorded: false,
    raw_auth_codes_recorded: false,
    raw_subjects_recorded: false,
    raw_tenant_ids_recorded: false,
    passwords_recorded: false,
    mfa_values_recorded: false,
    secrets_recorded: false,
    signing: { algorithm: 'Ed25519', key_id: config.signingKeyId, public_key_sha256: config.signingPublicKeySha256 },
    collector_code: { ...config.collectorCode },
    nonce_sha256: '6'.repeat(64),
  }
}

assert.equal(validateInteractiveProof(proof(), config).schema, SCHEMA)

const parsed = parseArgs([
  '--challenge', config.challenge,
  '--preparation-payload-sha256', config.preparationPayloadSha256,
  '--source-sha', config.sourceSha,
  '--web-service', config.deployedWebSource.service_name,
  '--web-task-definition', config.deployedWebSource.task_definition_arn,
  '--web-task-arn', config.deployedWebSource.task_arn,
  '--web-task-private-ip-sha256', config.deployedWebSource.task_private_ipv4_sha256,
  '--web-target-group-arn', config.deployedWebSource.target_group_arn,
  '--web-image-digest', config.deployedWebSource.image_digest,
  '--asset-path', config.deployedWebSource.asset_path,
  '--asset-sha256', config.deployedWebSource.asset_sha256,
  '--expected-subject-sha256', config.expectedPrincipalA.subject_sha256,
  '--expected-tenant-id-sha256', config.expectedPrincipalA.tenant_id_sha256,
  '--signing-key', join(tmpdir(), 'production-d1a-signing.pem'),
  '--signing-key-id', config.signingKeyId,
  '--output', 'interactive-proof.json',
])
assert.deepEqual(parsed.deployedWebSource, config.deployedWebSource)
assert.deepEqual(parsed.expectedPrincipalA, config.expectedPrincipalA)
assert.throws(() => parseArgs(['--expected-subject', rawSubject]))

for (const mutate of [
  (value) => { value.route_interceptions = 1 },
  (value) => { value.observed_origins.push('https://evil.example') },
  (value) => { value.authorization_cycles[0].authorize_request.code_challenge_method_s256 = false },
  (value) => { value.authorization_cycles.pop() },
  (value) => { value.logout.local_token_absent = false },
  (value) => { value.sessions.after_logout_refusal_status = 200 },
  (value) => { value.sessions.second.subject_sha256 = '6'.repeat(64) },
  (value) => { value.sessions.second.tenant_id_sha256 = '7'.repeat(64) },
  (value) => { value.sessions.second.access_token_sha256 = value.sessions.first.access_token_sha256 },
  (value) => { value.sessions.second.jwt_alg_rs256 = false },
  (value) => { value.preparation.challenge_sha256 = '8'.repeat(64) },
  (value) => { value.deployed_web_source.asset_sha256 = '0'.repeat(64) },
  (value) => { value.deployed_web_source.target_health = 'unhealthy' },
  (value) => { value.expected_principal_a.subject_sha256 = '0'.repeat(64) },
  (value) => { value.sessions.first.subject_sha256 = '0'.repeat(64) },
  (value) => { value.raw_tokens_recorded = true },
  (value) => { value.signing.key_id = 'attacker-key' },
  (value) => { value.collector_code.source_sha = 'd'.repeat(40) },
  (value) => { value.collector_code.script_sha256 = 'e'.repeat(64) },
  (value) => { value.nonce_sha256 = 'short' },
  (value) => { value.leaked_jwt = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhdXRoMHx4In0.signature' },
]) {
  const value = proof()
  mutate(value)
  assert.throws(() => validateInteractiveProof(value, config))
}

// Universal Login serves its SDK from cdn.auth0.com, so a real run observes three
// origins. Enumerated, not widened: dropping the target, dropping the issuer, adding a
// fourth origin, or repeating one must all still fail closed. Must match the verifier's
// ORIGIN_SHAPES, because a disagreement is only discoverable by spending an operator login.
const threeOriginProof = proof()
threeOriginProof.observed_origins = [TARGET_ORIGIN, new URL(ISSUER).origin, AUTH0_CDN_ORIGIN].sort()
assert.doesNotThrow(() => validateInteractiveProof(threeOriginProof, config))

for (const badOrigins of [
  [TARGET_ORIGIN, AUTH0_CDN_ORIGIN].sort(),
  [new URL(ISSUER).origin, AUTH0_CDN_ORIGIN].sort(),
  [TARGET_ORIGIN, new URL(ISSUER).origin, AUTH0_CDN_ORIGIN, 'https://evil.example'].sort(),
  [AUTH0_CDN_ORIGIN],
  [],
  [TARGET_ORIGIN, TARGET_ORIGIN, new URL(ISSUER).origin].sort(),
  'https://cdn.auth0.com',
]) {
  const value = proof()
  value.observed_origins = badOrigins
  assert.throws(() => validateInteractiveProof(value, config))
}

const collectorSource = readFileSync(new URL('./deployed_auth0_production_spa_origin_acceptance.mjs', import.meta.url), 'utf8')
assert.equal(collectorSource.includes('.route('), false)
assert.equal(collectorSource.includes('--password'), false)
assert.equal(collectorSource.includes('process.env'), false)

const derived = await deriveCollectorCode({
  execFileSync: (_command, args) => args.includes('rev-parse') ? `${config.collectorCode.source_sha}\n` : '',
})
assert.equal(derived.source_sha, config.collectorCode.source_sha)
assert.equal(derived.script_path, config.collectorCode.script_path)
await assert.rejects(() => deriveCollectorCode({ execFileSync: (_command, args) => args.includes('rev-parse') ? `${config.collectorCode.source_sha}\n` : ' M changed' }))

const signed = signInteractiveProof(proof(), config, {
  privateKey: signing.privateKey,
  publicKeySha256: config.signingPublicKeySha256,
})
assert.equal(signed.schema, SIGNED_SCHEMA)
assert.equal(verify(null, Buffer.from(canonical(signed.payload)), signing.publicKey, Buffer.from(signed.signature_b64, 'base64')), true)
const tampered = structuredClone(signed)
tampered.payload.logout.local_token_absent = false
assert.equal(verify(null, Buffer.from(canonical(tampered.payload)), signing.publicKey, Buffer.from(tampered.signature_b64, 'base64')), false)
assert.throws(() => signInteractiveProof(proof(), config, {
  privateKey: signing.privateKey,
  publicKeySha256: '0'.repeat(64),
}), /does not match preparation trust/)

const signingDirectory = await mkdtemp(join(tmpdir(), 'leaf-production-d1a-key-'))
try {
  const signingPath = join(signingDirectory, 'collector.pem')
  await writeFile(signingPath, signing.privateKey.export({ format: 'pem', type: 'pkcs8' }), { mode: 0o600 })
  const loaded = await loadOutsideRepositorySigner({ signingKeyPath: signingPath })
  assert.equal(loaded.publicKeySha256, config.signingPublicKeySha256)
} finally {
  await rm(signingDirectory, { recursive: true, force: true })
}

function token(signature) {
  const header = Buffer.from(JSON.stringify({ alg: 'RS256' })).toString('base64url')
  const payload = Buffer.from(JSON.stringify({
    iss: ISSUER,
    aud: [AUDIENCE],
    azp: CLIENT_ID,
    sub: rawSubject,
    'https://leafdesign.ai/tenant_id': rawTenant,
  })).toString('base64url')
  return `${header}.${payload}.${signature}`
}

// Test doubles are selected by the ARGUMENT SHAPE of a call -- the source text of
// the callback it was handed -- never by call index. The fake's evaluate() keeps a
// POSITIONAL counter, so anything matched by position silently renumbers when a
// call is inserted; that trap ate time on the PR #673 port and must not be re-armed.
const PROTECTED_SESSION_EVALUATE = /leaf\.jwt[\s\S]*api\/session/
const TOKEN_PRESENT_WAIT = /!!window\.localStorage/

function injections(specs) {
  return specs.map((spec) => ({ remaining: spec.times ?? 1, ...spec }))
}

function takeInjection(entries, source) {
  const hit = entries.find((entry) => entry.remaining > 0 && entry.source.test(source))
  if (!hit) return null
  hit.remaining -= 1
  return hit
}

// The exact string Playwright raises when a post-login navigation tears the
// evaluation context down under the collector (observed on two real operator
// logins, 2026-08-18).
function destroyedContext() {
  return new Error('page.evaluate: Execution context was destroyed, most likely because of a navigation.')
}

function fakePlaywright(options = {}) {
  const evaluateInjections = options.evaluate || []
  const waitInjections = options.waitForFunction || []
  let loadStateWaits = 0
  const handlers = new Map()
  let currentUrl = `${TARGET_ORIGIN}/try`
  let evaluateCount = 0
  let authorizationCount = 0
  // Sign in leaves the browser sitting at the issuer with the callback still
  // pending, exactly as a real Universal Login does while the operator types.
  let preSignInUrl = null
  let pendingCallback = null
  const tokens = [token('signature-one'), token('signature-two')]
  const emitRequest = (url) => handlers.get('request')?.({ url: () => url })
  const page = {
    on(name, callback) { handlers.set(name, callback) },
    async goto(url) { currentUrl = url; emitRequest(url) },
    async evaluate(_callback, argument) {
      // Injected faults and results are matched on the callback SOURCE and consumed
      // BEFORE the positional counter advances, so a retried call keeps its own
      // number and a retry never renumbers the positional cases below.
      const injected = takeInjection(evaluateInjections, String(_callback))
      if (injected?.error) throw injected.error
      if (injected && 'result' in injected) return injected.result
      // The RP-initiated logout evaluate is identified by its ARGUMENT SHAPE, not
      // by call position, so adding it never renumbers the positional cases below.
      if (argument !== null && typeof argument === 'object' && 'issuerOrigin' in argument) {
        assert.equal(argument.issuerOrigin, new URL(ISSUER).origin)
        assert.equal(argument.clientId, CLIENT_ID)
        assert.equal(argument.returnTo, TARGET_ORIGIN, 'logout must return to the exact target origin')
        emitRequest(`${new URL(ISSUER).origin}/v2/logout`)
        currentUrl = `${TARGET_ORIGIN}/`
        return undefined
      }
      evaluateCount += 1
      if (evaluateCount === 1) return { status: 200, body: { source_sha: config.sourceSha } }
      if (evaluateCount === 2) {
        assert.equal(argument, config.deployedWebSource.asset_path)
        return { status: 200, sha256: assetSha }
      }
      if (evaluateCount === 3 || evaluateCount === 6) return 401
      if (evaluateCount === 4) return { status: 200, token: tokens[0] }
      if (evaluateCount === 5) return true
      if (evaluateCount === 7) return { status: 200, token: tokens[1] }
      throw new Error(`unexpected evaluate call ${evaluateCount}`)
    },
    getByRole(_role, options) {
      return {
        first() { return this },
        async click() {
          if (options.name === 'Sign in') {
            authorizationCount += 1
            const query = new URLSearchParams({
              response_type: 'code', redirect_uri: TARGET_ORIGIN, audience: AUDIENCE,
              client_id: CLIENT_ID, state: `state-${authorizationCount}`,
              code_challenge: `challenge-${authorizationCount}`, code_challenge_method: 'S256',
            })
            const authorizeUrl = `${new URL(ISSUER).origin}/authorize?${query}`
            emitRequest(authorizeUrl)
            preSignInUrl = currentUrl
            currentUrl = authorizeUrl
            pendingCallback = `${TARGET_ORIGIN}/?code=code-${authorizationCount}&state=state-${authorizationCount}`
          } else {
            // 'Sign in' is the ONLY control the collector may click. The UI
            // sign-out drawer is a stochastic race for this principal and was
            // replaced by the RP-initiated /v2/logout above; clicking any other
            // control is a regression, so fail loudly instead of emulating it.
            assert.fail(`the collector must not click '${options.name}'; sign-out is driven via /v2/logout`)
          }
        },
      }
    },
    async waitForURL(predicate) {
      if (preSignInUrl !== null) {
        // The first wait after Sign in is the departure gate and must REJECT
        // the pre-navigation URL. A predicate that already accepts it resolves
        // instantly and hands the following token wait a race against the
        // operator's typing - the defect that failed two live D1a runs on
        // 2026-08-17.
        assert.equal(
          predicate(new URL(preSignInUrl)), false,
          'the first wait after Sign in must not accept the pre-navigation URL',
        )
        preSignInUrl = null
      }
      if (predicate(new URL(currentUrl))) return
      if (pendingCallback !== null) {
        // The operator finished Universal Login: the issuer bounces through
        // the callback and the app settles on a clean URL.
        emitRequest(pendingCallback)
        pendingCallback = null
      }
      currentUrl = `${TARGET_ORIGIN}/try`
      assert.equal(predicate(new URL(currentUrl)), true)
    },
    async waitForFunction(predicate, _argument, waitOptions) {
      // A retry must spend what is LEFT of the original 60s budget, never restart a
      // fresh 60s window per attempt. This is the assertion that keeps the hardening
      // from quietly becoming an unbounded wait.
      assert.ok(
        typeof waitOptions?.timeout === 'number' && waitOptions.timeout > 0 && waitOptions.timeout <= 60_000,
        `token wait must stay inside the original 60s budget, got ${JSON.stringify(waitOptions)}`,
      )
      const injected = takeInjection(waitInjections, String(predicate))
      if (injected?.error) throw injected.error
    },
    async waitForLoadState(state) {
      assert.equal(state, 'domcontentloaded', 'the retry must settle the page before re-probing')
      loadStateWaits += 1
    },
    url() { return currentUrl },
  }
  return {
    get loadStateWaits() { return loadStateWaits },
    chromium: {
      async launch(options) {
        assert.deepEqual(options, { headless: false })
        return {
          async newContext() { return { async newPage() { return page } } },
          async close() {},
        }
      },
    },
  }
}

const collected = await collectInteractiveProof(config, { playwright: fakePlaywright(), notify: () => {} })
assert.equal(collected.authorization_cycles.length, 2)
assert.equal(collected.sessions.after_logout_refusal_status, 401)
assert.equal(collected.logout.auth0_round_trip, true)
assert.notEqual(collected.sessions.first.access_token_sha256, collected.sessions.second.access_token_sha256)


// --- Regression: the post-login navigation teardown race (third of its kind in this
// --- window, after PR #652's departure gate and PR #673's sign-out drawer).

// The protected-session probe fires while the SPA is still exchanging the auth code.
// Two teardowns in a row must be ridden out, not fatal, and the page must be settled
// between attempts.
{
  const fake = fakePlaywright({
    evaluate: injections([{ source: PROTECTED_SESSION_EVALUATE, times: 2, error: destroyedContext() }]),
  })
  const survived = await collectInteractiveProof(config, { playwright: fake, notify: () => {} })
  assert.equal(survived.sessions.first.api_status, 200)
  assert.equal(survived.authorization_cycles.length, 2)
  assert.ok(fake.loadStateWaits >= 2, `expected a settle between retries, saw ${fake.loadStateWaits}`)
}

// The token wait races the same navigation. 'Target closed' is the other teardown
// wording Playwright uses for it.
{
  const fake = fakePlaywright({
    waitForFunction: injections([
      { source: TOKEN_PRESENT_WAIT, error: destroyedContext() },
      { source: TOKEN_PRESENT_WAIT, error: new Error('page.waitForFunction: Target closed') },
    ]),
  })
  const survived = await collectInteractiveProof(config, { playwright: fake, notify: () => {} })
  assert.equal(survived.sessions.first.api_status, 200)
}

// FAIL CLOSED, 1/3: a non-navigation error is never swallowed by the retry.
await assert.rejects(
  collectInteractiveProof(config, {
    playwright: fakePlaywright({
      evaluate: injections([{ source: PROTECTED_SESSION_EVALUATE, times: 99, error: new Error('unrelated backend failure') }]),
    }),
    notify: () => {},
  }),
  /unrelated backend failure/,
  'a non-navigation error must propagate, not be retried away',
)

// FAIL CLOSED, 2/3: a token that never lands still times out. Playwright's real
// timeout wording carries no teardown phrase, so it must rethrow on the first hit
// rather than burn the whole retry budget.
await assert.rejects(
  collectInteractiveProof(config, {
    playwright: fakePlaywright({
      waitForFunction: injections([
        { source: TOKEN_PRESENT_WAIT, times: 99, error: new Error('page.waitForFunction: Timeout 60000ms exceeded.') },
      ]),
    }),
    notify: () => {},
  }),
  /Timeout 60000ms exceeded/,
  'a genuine token-wait timeout must still fail the collector',
)

// FAIL CLOSED, 3/3: the load-bearing security assertion. This script is an
// attestation artifact -- a protected session that does not return 200 must fail
// the run even when the retry path is exercised on the very same call.
await assert.rejects(
  collectInteractiveProof(config, {
    playwright: fakePlaywright({
      evaluate: injections([
        { source: PROTECTED_SESSION_EVALUATE, error: destroyedContext() },
        { source: PROTECTED_SESSION_EVALUATE, result: { status: 500, token: token('signature-one') } },
      ]),
    }),
    notify: () => {},
  }),
  /authenticated protected session did not return 200/,
  'the retry must never turn a non-200 protected session into a pass',
)

console.log('PRODUCTION_D1A_AUTH0_SPA_ORIGIN_COLLECTOR_TESTS_OK')
