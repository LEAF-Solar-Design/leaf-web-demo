import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  AUDIENCE,
  CLIENT_ID,
  ISSUER,
  SCHEMA,
  TARGET_ORIGIN,
  collectInteractiveProof,
  parseArgs,
  sha256,
  validateInteractiveProof,
} from './deployed_auth0_spa_origin_acceptance.mjs'

const rawSubject = 'auth0|interactive-principal'
const rawTenant = 'interactive-tenant'
const assetSha = '9'.repeat(64)
const config = {
  challenge: 'A'.repeat(43),
  preparationPayloadSha256: '1'.repeat(64),
  sourceSha: 'a'.repeat(40),
  targetOrigin: TARGET_ORIGIN,
  deployedWebSource: {
    schema: 'leaf.auth0-spa-origin-web-source.v1', environment: 'staging', target_origin: TARGET_ORIGIN,
    service_name: 'leaf-platform-web',
    task_definition_arn: 'arn:aws:ecs:us-east-1:807034087062:task-definition/leaf-platform-web:42',
    image_digest: `sha256:${'8'.repeat(64)}`, source_revision: 'a'.repeat(40),
    asset_path: '/assets/index-proof.js', asset_sha256: assetSha,
  },
  expectedPrincipalA: { subject_sha256: sha256(rawSubject), tenant_id_sha256: sha256(rawTenant), reviewed: true },
}

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
    plan_version: '1.31',
    environment: 'staging',
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
      first: { api_status: 200, subject_sha256: sha256(rawSubject), tenant_id_sha256: sha256(rawTenant), access_token_sha256: '4'.repeat(64) },
      after_logout_refusal_status: 401,
      second: { api_status: 200, subject_sha256: sha256(rawSubject), tenant_id_sha256: sha256(rawTenant), access_token_sha256: '5'.repeat(64) },
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
  }
}

assert.equal(validateInteractiveProof(proof(), config).schema, SCHEMA)

const parsed = parseArgs([
  '--challenge', config.challenge,
  '--preparation-payload-sha256', config.preparationPayloadSha256,
  '--source-sha', config.sourceSha,
  '--web-service', config.deployedWebSource.service_name,
  '--web-task-definition', config.deployedWebSource.task_definition_arn,
  '--web-image-digest', config.deployedWebSource.image_digest,
  '--asset-path', config.deployedWebSource.asset_path,
  '--asset-sha256', config.deployedWebSource.asset_sha256,
  '--expected-subject-sha256', config.expectedPrincipalA.subject_sha256,
  '--expected-tenant-id-sha256', config.expectedPrincipalA.tenant_id_sha256,
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
  (value) => { value.preparation.challenge_sha256 = '8'.repeat(64) },
  (value) => { value.deployed_web_source.asset_sha256 = '0'.repeat(64) },
  (value) => { value.expected_principal_a.subject_sha256 = '0'.repeat(64) },
  (value) => { value.sessions.first.subject_sha256 = '0'.repeat(64) },
  (value) => { value.raw_tokens_recorded = true },
  (value) => { value.leaked_jwt = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhdXRoMHx4In0.signature' },
]) {
  const value = proof()
  mutate(value)
  assert.throws(() => validateInteractiveProof(value, config))
}

const collectorSource = readFileSync(new URL('./deployed_auth0_spa_origin_acceptance.mjs', import.meta.url), 'utf8')
assert.equal(collectorSource.includes('.route('), false)
assert.equal(collectorSource.includes('--password'), false)
assert.equal(collectorSource.includes('process.env'), false)

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

function fakePlaywright() {
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
            emitRequest(`${new URL(ISSUER).origin}/v2/logout`)
            currentUrl = `${TARGET_ORIGIN}/`
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
    async waitForFunction() {},
    url() { return currentUrl },
  }
  return {
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

console.log('D1A_AUTH0_SPA_ORIGIN_COLLECTOR_TESTS_OK')
