#!/usr/bin/env node

import { createHash } from 'node:crypto'
import { chmod, open, rename } from 'node:fs/promises'
import { resolve } from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

export const SCHEMA = 'leaf.auth0-spa-origin-interactive-proof.v1'
export const TARGET_ORIGIN = 'https://platform-staging.leafdesign.ai'
export const ISSUER = 'https://leafautomation.us.auth0.com/'
export const AUDIENCE = 'https://api.leafdesign.ai'
export const CLIENT_ID = 'zkJjr0ZFtcyQjyJ8e4zdkdgzoMaVWt5O'
// Universal Login serves its own SDK bundle from Auth0's first-party CDN, so a real
// interactive run observes it (live-verified 2026-08-17 19:47Z: performance entries on
// the ULP are exactly cdn.auth0.com + the issuer). Enumerated, never inferred, and it
// MUST stay identical to ORIGIN_SHAPES in the server-side verifier
// (scripts/verify_leaf_platform_auth0_spa_origin_receipt.py, leaf-automation-aws-terraform
// #831). A collector that disagrees with the verifier is only discoverable by spending an
// interactive operator login round, which is how staging lost four prepare windows.
export const AUTH0_CDN_ORIGIN = 'https://cdn.auth0.com'
export const ORIGIN_SHAPES = [
  [TARGET_ORIGIN, new URL(ISSUER).origin].sort(),
  [TARGET_ORIGIN, new URL(ISSUER).origin, AUTH0_CDN_ORIGIN].sort(),
]

const SHA256 = /^[0-9a-f]{64}$/
const SOURCE_SHA = /^[0-9a-f]{40}$/
const JWT = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/
const TASK_DEFINITION = /^arn:aws:ecs:us-east-1:807034087062:task-definition\/leaf-platform-web(?:-alt)?:[1-9][0-9]*$/
const IMAGE_DIGEST = /^sha256:[0-9a-f]{64}$/
const ASSET_PATH = /^\/assets\/[A-Za-z0-9_-]+\.js$/

function fail(message) {
  throw new Error(message)
}

export function sha256(value) {
  return createHash('sha256').update(value).digest('hex')
}

function exactKeys(value, expected, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(`${label} must be an object`)
  const actual = Object.keys(value).sort()
  const wanted = [...expected].sort()
  if (JSON.stringify(actual) !== JSON.stringify(wanted)) fail(`${label} has unexpected fields`)
}

function requiredString(value, label) {
  if (typeof value !== 'string' || !value) fail(`${label} must be a non-empty string`)
  return value
}

function parseJwt(token) {
  if (!JWT.test(token)) fail('browser access token is not a compact JWT')
  let claims
  try {
    claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString('utf8'))
  } catch {
    fail('browser access token payload is invalid')
  }
  const subject = requiredString(claims.sub, 'JWT subject')
  const tenant = requiredString(claims['https://leafdesign.ai/tenant_id'], 'JWT tenant claim')
  if (!subject.startsWith('auth0|')) fail('interactive principal is not an Auth0 database account')
  if (claims.iss !== ISSUER) fail('JWT issuer is not exact')
  const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud]
  if (!audiences.some((aud) => aud === AUDIENCE)) fail('JWT audience is not exact')
  if (claims.azp !== CLIENT_ID) fail('JWT authorized party is not the staging SPA')
  return {
    subject_sha256: sha256(subject),
    tenant_id_sha256: sha256(tenant),
    access_token_sha256: sha256(token),
  }
}

function validateAuthorizationCycle(cycle, sequence) {
  exactKeys(cycle, ['sequence', 'authorize_request', 'callback'], `authorization cycle ${sequence}`)
  if (cycle.sequence !== sequence) fail('authorization cycles are out of order')
  exactKeys(cycle.authorize_request, [
    'origin_exact', 'response_type_code', 'redirect_uri_exact', 'audience_exact',
    'client_id_exact', 'state_present', 'code_challenge_present', 'code_challenge_method_s256',
  ], `authorization cycle ${sequence} request`)
  if (Object.values(cycle.authorize_request).some((value) => value !== true)) {
    fail(`authorization cycle ${sequence} is not exact PKCE`)
  }
  exactKeys(cycle.callback, ['origin_exact', 'code_present', 'state_present'], `authorization cycle ${sequence} callback`)
  if (Object.values(cycle.callback).some((value) => value !== true)) {
    fail(`authorization cycle ${sequence} callback is incomplete`)
  }
}

function validateSession(session, label) {
  exactKeys(session, ['api_status', 'subject_sha256', 'tenant_id_sha256', 'access_token_sha256'], label)
  if (session.api_status !== 200) fail(`${label} did not reach the protected session`)
  for (const key of ['subject_sha256', 'tenant_id_sha256', 'access_token_sha256']) {
    if (!SHA256.test(session[key])) fail(`${label} ${key} is invalid`)
  }
}

function validateSourceTuple(value, label = 'deployed web source tuple') {
  exactKeys(value, [
    'schema', 'environment', 'target_origin', 'service_name', 'task_definition_arn',
    'image_digest', 'source_revision', 'asset_path', 'asset_sha256',
  ], label)
  if (value.schema !== 'leaf.auth0-spa-origin-web-source.v1' || value.environment !== 'staging' || value.target_origin !== TARGET_ORIGIN) fail(`${label} identity is invalid`)
  if (!['leaf-platform-web', 'leaf-platform-web-alt'].includes(value.service_name)) fail(`${label} service is invalid`)
  if (!TASK_DEFINITION.test(value.task_definition_arn) || value.task_definition_arn.split('/').at(-1).split(':')[0] !== value.service_name) fail(`${label} task definition is invalid`)
  if (!IMAGE_DIGEST.test(value.image_digest) || !SOURCE_SHA.test(value.source_revision)) fail(`${label} immutable source is invalid`)
  if (!ASSET_PATH.test(value.asset_path) || !SHA256.test(value.asset_sha256)) fail(`${label} asset identity is invalid`)
}

function validateExpectedPrincipal(value) {
  exactKeys(value, ['subject_sha256', 'tenant_id_sha256', 'reviewed'], 'expected principal A')
  if (!SHA256.test(value.subject_sha256) || !SHA256.test(value.tenant_id_sha256) || value.reviewed !== true) fail('expected principal A is invalid')
}

export function validateInteractiveProof(proof, config) {
  exactKeys(proof, [
    'schema', 'plan_version', 'environment', 'target_origin', 'expected_source_revision', 'observed_source_revision',
    'issuer', 'audience', 'client_id_sha256', 'deployed_web_source', 'expected_principal_a', 'preparation', 'authorization_cycles', 'logout',
    'sessions', 'route_interceptions', 'observed_origins', 'operator_interactive', 'started_at',
    'completed_at', 'raw_tokens_recorded', 'raw_auth_codes_recorded', 'raw_subjects_recorded',
    'raw_tenant_ids_recorded', 'passwords_recorded', 'mfa_values_recorded', 'secrets_recorded',
  ], 'interactive proof')
  if (proof.schema !== SCHEMA || proof.plan_version !== '1.31' || proof.environment !== 'staging') fail('interactive proof schema, plan, or environment is invalid')
  if (proof.target_origin !== TARGET_ORIGIN || proof.target_origin !== config.targetOrigin) fail('target origin is not exact')
  if (proof.expected_source_revision !== config.sourceSha || proof.observed_source_revision !== config.sourceSha) {
    fail('deployed source revision is not exact')
  }
  if (proof.issuer !== ISSUER || proof.audience !== AUDIENCE || proof.client_id_sha256 !== sha256(CLIENT_ID)) {
    fail('Auth0 public identity is not exact')
  }
  validateSourceTuple(proof.deployed_web_source)
  if (JSON.stringify(proof.deployed_web_source) !== JSON.stringify(config.deployedWebSource)) fail('deployed web source tuple differs from preparation')
  validateExpectedPrincipal(proof.expected_principal_a)
  if (JSON.stringify(proof.expected_principal_a) !== JSON.stringify(config.expectedPrincipalA)) fail('expected principal differs from preparation')
  exactKeys(proof.preparation, ['challenge_sha256', 'payload_sha256'], 'preparation link')
  if (proof.preparation.challenge_sha256 !== sha256(config.challenge) ||
      proof.preparation.payload_sha256 !== config.preparationPayloadSha256) fail('preparation link is invalid')
  if (!Array.isArray(proof.authorization_cycles) || proof.authorization_cycles.length !== 2) {
    fail('exactly two authorization cycles are required')
  }
  proof.authorization_cycles.forEach((cycle, index) => validateAuthorizationCycle(cycle, index + 1))
  exactKeys(proof.logout, ['auth0_round_trip', 'return_origin_exact', 'local_token_absent'], 'logout')
  if (Object.values(proof.logout).some((value) => value !== true)) fail('logout was not complete')
  exactKeys(proof.sessions, ['before_login_refusal_status', 'first', 'after_logout_refusal_status', 'second'], 'sessions')
  if (proof.sessions.before_login_refusal_status !== 401 || proof.sessions.after_logout_refusal_status !== 401) {
    fail('protected signed-out refusal was not exact')
  }
  validateSession(proof.sessions.first, 'first session')
  validateSession(proof.sessions.second, 'second session')
  for (const label of ['first', 'second']) {
    if (proof.sessions[label].subject_sha256 !== config.expectedPrincipalA.subject_sha256 ||
        proof.sessions[label].tenant_id_sha256 !== config.expectedPrincipalA.tenant_id_sha256) {
      fail(`${label} session is not reviewed principal A`)
    }
  }
  if (proof.sessions.first.subject_sha256 !== proof.sessions.second.subject_sha256 ||
      proof.sessions.first.tenant_id_sha256 !== proof.sessions.second.tenant_id_sha256) {
    fail('identity changed across re-login')
  }
  if (proof.sessions.first.access_token_sha256 === proof.sessions.second.access_token_sha256) {
    fail('re-login reused the first access token')
  }
  if (proof.route_interceptions !== 0) fail('browser request interception is forbidden')
  // Closed world: membership in an enumerated set, never a subset test. Dropping the
  // target, dropping the issuer, or adding a fourth origin all still fail closed.
  const observedOrigins = Array.isArray(proof.observed_origins) ? [...proof.observed_origins].sort() : null
  if (!observedOrigins || !ORIGIN_SHAPES.some((shape) => JSON.stringify(shape) === JSON.stringify(observedOrigins))) {
    fail('browser contacted an unexpected origin')
  }
  if (proof.operator_interactive !== true) fail('operator interaction was not recorded')
  for (const key of [
    'raw_tokens_recorded', 'raw_auth_codes_recorded', 'raw_subjects_recorded', 'raw_tenant_ids_recorded',
    'passwords_recorded', 'mfa_values_recorded', 'secrets_recorded',
  ]) {
    if (proof[key] !== false) fail(`${key} must be false`)
  }
  if (typeof proof.started_at !== 'string' || typeof proof.completed_at !== 'string' ||
      !Number.isFinite(Date.parse(proof.started_at)) || !Number.isFinite(Date.parse(proof.completed_at)) ||
      Date.parse(proof.completed_at) < Date.parse(proof.started_at)) fail('proof timestamps are invalid')
  return proof
}

function authorizationObservation(url) {
  const parsed = new URL(url)
  const query = parsed.searchParams
  return {
    origin_exact: parsed.origin === new URL(ISSUER).origin,
    response_type_code: query.get('response_type') === 'code',
    redirect_uri_exact: query.get('redirect_uri') === TARGET_ORIGIN,
    audience_exact: query.get('audience') === AUDIENCE,
    client_id_exact: query.get('client_id') === CLIENT_ID,
    state_present: !!query.get('state'),
    code_challenge_present: !!query.get('code_challenge'),
    code_challenge_method_s256: query.get('code_challenge_method') === 'S256',
  }
}

function callbackObservation(url) {
  const parsed = new URL(url)
  return {
    origin_exact: parsed.origin === TARGET_ORIGIN,
    code_present: !!parsed.searchParams.get('code'),
    state_present: !!parsed.searchParams.get('state'),
  }
}

// Post-login the SPA is still exchanging the authorization code, so a navigation can
// tear down the evaluation context mid-call and kill an otherwise healthy roundtrip.
// This is the THIRD race in that same window (PR #652 gated the departure to the
// issuer, PR #673 replaced the sign-out drawer), and each one cost a real operator
// login cycle. Retry ONLY the navigation teardown, bounded by a wall-clock deadline:
// any other error, and the deadline itself, rethrows unchanged, so the collector
// still fails closed on a genuinely broken session. Never widen this pattern to
// swallow an assertion -- every fail() below stays outside the retry.
const NAVIGATION_TEARDOWN = /Execution context was destroyed|Target closed|navigation/i
const NAVIGATION_RETRY_MS = 60_000

async function retryThroughNavigation(page, attempt) {
  const deadline = Date.now() + NAVIGATION_RETRY_MS
  for (;;) {
    try {
      return await attempt(Math.max(1_000, deadline - Date.now()))
    } catch (error) {
      if (Date.now() >= deadline) throw error
      if (!NAVIGATION_TEARDOWN.test(error?.message ?? '')) throw error
      await page.waitForLoadState('domcontentloaded').catch(() => {})
    }
  }
}

async function protectedSession(page) {
  const result = await retryThroughNavigation(page, () => page.evaluate(async () => {
    const token = window.localStorage.getItem('leaf.jwt') || ''
    const response = await fetch('/api/session?dwg=rooftop_demo', {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    return { status: response.status, token }
  }))
  if (result.status !== 200) fail('authenticated protected session did not return 200')
  return { api_status: result.status, ...parseJwt(result.token) }
}

async function signedOutStatus(page) {
  return page.evaluate(async () => (await fetch('/api/session?dwg=rooftop_demo')).status)
}

async function waitForInteractiveLogin(page, cycles, callbacks, sequence, notify) {
  await page.getByRole('button', { name: 'Sign in', exact: true }).first().click()
  notify(`Complete Universal Login and MFA in the visible browser for cycle ${sequence}.\n`)
  // Gate on the departure to the issuer BEFORE waiting for the return: the
  // return predicate below also matches the pre-navigation URL, so without
  // this it resolves instantly and the 60s token wait races the operator's
  // Universal Login typing instead of starting when they come back.
  await page.waitForURL((url) => url.origin === new URL(ISSUER).origin, { timeout: 2 * 60_000 })
  await page.waitForURL((url) => url.origin === TARGET_ORIGIN && !url.searchParams.has('code') && !url.searchParams.has('state'), {
    timeout: 10 * 60_000,
  })
  // Same teardown race as protectedSession: the token wait keeps its original 60s
  // total budget, spent across retries rather than lost to the first navigation.
  await retryThroughNavigation(page, (remainingMs) => page.waitForFunction(
    () => !!window.localStorage.getItem('leaf.jwt'), null, { timeout: remainingMs },
  ))
  if (cycles.length !== sequence || callbacks.length !== sequence) {
    fail(`authorization request and callback ${sequence} were not observed exactly once`)
  }
  return { sequence, authorize_request: cycles[sequence - 1], callback: callbacks[sequence - 1] }
}

export async function collectInteractiveProof(config, dependencies = {}) {
  const playwright = dependencies.playwright || await import('@playwright/test')
  const notify = dependencies.notify || ((message) => process.stderr.write(message))
  const browser = await playwright.chromium.launch({ headless: false })
  const startedAt = new Date().toISOString()
  try {
    const context = await browser.newContext()
    const page = await context.newPage()
    const origins = new Set()
    const authorizationRequests = []
    const callbacks = []
    let logoutRequestObserved = false
    page.on('request', (request) => {
      const url = new URL(request.url())
      origins.add(url.origin)
      if (url.origin === new URL(ISSUER).origin && url.pathname.endsWith('/authorize')) {
        authorizationRequests.push(authorizationObservation(url))
      }
      if (url.origin === TARGET_ORIGIN && url.searchParams.has('code') && url.searchParams.has('state')) {
        callbacks.push(callbackObservation(url))
      }
      if (url.origin === new URL(ISSUER).origin && url.pathname === '/v2/logout') logoutRequestObserved = true
    })
    await page.goto(`${TARGET_ORIGIN}/try`, { waitUntil: 'networkidle', timeout: 60_000 })
    const health = await page.evaluate(async () => {
      const response = await fetch('/health.json', { cache: 'no-store' })
      return { status: response.status, body: await response.json() }
    })
    if (health.status !== 200 || health.body?.source_sha !== config.sourceSha) fail('deployed web source is not exact')
    const asset = await page.evaluate(async (assetPath) => {
      const response = await fetch(assetPath, { cache: 'no-store' })
      const bytes = await response.arrayBuffer()
      const digest = await crypto.subtle.digest('SHA-256', bytes)
      return { status: response.status, sha256: [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('') }
    }, config.deployedWebSource.asset_path)
    if (asset.status !== 200 || asset.sha256 !== config.deployedWebSource.asset_sha256) fail('deployed web asset identity is not exact')
    const before = await signedOutStatus(page)
    const firstCycle = await waitForInteractiveLogin(page, authorizationRequests, callbacks, 1, notify)
    const first = await protectedSession(page)
    // Sign out via the app's own logout mechanics (auth.js:156), driven directly:
    // clear the local token and navigate the RP-initiated Auth0 /v2/logout with
    // returnTo the exact origin. The UI drawer path (Trust -> Account details ->
    // Sign out) is NOT used, and must not be reintroduced: any authed 401 wipes
    // leaf.jwt and latches the session controller (api.js noteUnauthorized
    // :94-107), and the ops-scoped /api/telemetry/summary poll 401s valid
    // non-ops tokens on a ~5-minute wall-clock cadence, so the drawer is a
    // stochastic race for this principal. The /v2/logout request observer still
    // records the real roundtrip, so every logout assertion in the proof (round
    // trip, exact return origin, token absent, 401 after) is produced identically.
    try {
      await page.evaluate(({ issuerOrigin, clientId, returnTo }) => {
        try {
          window.localStorage.removeItem('leaf.jwt')
          window.localStorage.removeItem('leaf.inflightAuthor.v1')
        } catch { /* storage unavailable */ }
        const target = new URL('/v2/logout', issuerOrigin)
        target.searchParams.set('client_id', clientId)
        target.searchParams.set('returnTo', returnTo)
        window.location.assign(target.toString())
      }, { issuerOrigin: new URL(ISSUER).origin, clientId: CLIENT_ID, returnTo: TARGET_ORIGIN })
    } catch (signOutError) {
      // Never fail blind: record where we were and what was actually clickable.
      const url = page.url()
      let controls = []
      try {
        controls = await page.evaluate(() => [...document.querySelectorAll('button, [role=button], [role=tab], a')]
          .map((el) => (el.getAttribute('aria-label') || el.textContent || '').trim())
          .filter(Boolean).slice(0, 60))
      } catch { /* page unavailable */ }
      fail(`interactive sign-out failed at ${url}; visible controls: ${JSON.stringify(controls)}; original: ${signOutError.message}`)
    }
    await page.waitForURL((url) => url.origin === TARGET_ORIGIN, { timeout: 60_000 })
    await page.waitForFunction(() => !window.localStorage.getItem('leaf.jwt'), null, { timeout: 60_000 })
    const logoutReturnOriginExact = new URL(page.url()).origin === TARGET_ORIGIN
    const logoutTokenAbsent = await page.evaluate(() => !window.localStorage.getItem('leaf.jwt'))
    // returnTo lands on the bare origin, which carries NO 'Sign in' button, so
    // cycle 2 has to be driven from the /try surface. Same origin, so
    // observed_origins is unchanged.
    await page.goto(`${TARGET_ORIGIN}/try`, { waitUntil: 'networkidle', timeout: 60_000 })
    const afterLogout = await signedOutStatus(page)
    const secondCycle = await waitForInteractiveLogin(page, authorizationRequests, callbacks, 2, notify)
    const second = await protectedSession(page)
    const proof = {
      schema: SCHEMA,
      plan_version: '1.31',
      environment: 'staging',
      target_origin: TARGET_ORIGIN,
      expected_source_revision: config.sourceSha,
      observed_source_revision: health.body.source_sha,
      issuer: ISSUER,
      audience: AUDIENCE,
      client_id_sha256: sha256(CLIENT_ID),
      deployed_web_source: { ...config.deployedWebSource, source_revision: health.body.source_sha, asset_sha256: asset.sha256 },
      expected_principal_a: config.expectedPrincipalA,
      preparation: { challenge_sha256: sha256(config.challenge), payload_sha256: config.preparationPayloadSha256 },
      authorization_cycles: [firstCycle, secondCycle],
      logout: { auth0_round_trip: logoutRequestObserved, return_origin_exact: logoutReturnOriginExact, local_token_absent: logoutTokenAbsent },
      sessions: { before_login_refusal_status: before, first, after_logout_refusal_status: afterLogout, second },
      route_interceptions: 0,
      observed_origins: [...origins].sort(),
      operator_interactive: true,
      started_at: startedAt,
      completed_at: new Date().toISOString(),
      raw_tokens_recorded: false,
      raw_auth_codes_recorded: false,
      raw_subjects_recorded: false,
      raw_tenant_ids_recorded: false,
      passwords_recorded: false,
      mfa_values_recorded: false,
      secrets_recorded: false,
    }
    return validateInteractiveProof(proof, config)
  } finally {
    await browser.close()
  }
}

export function parseArgs(argv) {
  const values = {}
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index]
    const value = argv[index + 1]
    if (!name?.startsWith('--') || value === undefined) fail('arguments must be --name value pairs')
    values[name.slice(2)] = value
  }
  const expected = [
    'challenge', 'preparation-payload-sha256', 'source-sha', 'web-service', 'web-task-definition',
    'web-image-digest', 'asset-path', 'asset-sha256', 'expected-subject-sha256',
    'expected-tenant-id-sha256', 'output',
  ]
  if (JSON.stringify(Object.keys(values).sort()) !== JSON.stringify(expected.sort())) fail('unexpected or missing arguments')
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(values.challenge)) fail('challenge is invalid')
  if (!SHA256.test(values['preparation-payload-sha256'])) fail('preparation payload digest is invalid')
  if (!SOURCE_SHA.test(values['source-sha'])) fail('source SHA is invalid')
  const deployedWebSource = {
    schema: 'leaf.auth0-spa-origin-web-source.v1', environment: 'staging', target_origin: TARGET_ORIGIN,
    service_name: values['web-service'], task_definition_arn: values['web-task-definition'],
    image_digest: values['web-image-digest'], source_revision: values['source-sha'],
    asset_path: values['asset-path'], asset_sha256: values['asset-sha256'],
  }
  validateSourceTuple(deployedWebSource)
  const expectedPrincipalA = {
    subject_sha256: values['expected-subject-sha256'], tenant_id_sha256: values['expected-tenant-id-sha256'], reviewed: true,
  }
  validateExpectedPrincipal(expectedPrincipalA)
  return {
    challenge: values.challenge,
    preparationPayloadSha256: values['preparation-payload-sha256'],
    sourceSha: values['source-sha'],
    deployedWebSource,
    expectedPrincipalA,
    targetOrigin: TARGET_ORIGIN,
    output: resolve(values.output),
  }
}

async function writePrivate(path, value) {
  const temporary = `${path}.tmp-${process.pid}`
  const handle = await open(temporary, 'wx', 0o600)
  try {
    await handle.writeFile(`${JSON.stringify(value)}\n`, { encoding: 'utf8' })
  } finally {
    await handle.close()
  }
  await rename(temporary, path)
  await chmod(path, 0o600)
}

async function main() {
  const config = parseArgs(process.argv.slice(2))
  const proof = await collectInteractiveProof(config)
  await writePrivate(config.output, proof)
  process.stdout.write(`Wrote sanitized interactive proof to ${config.output}\n`)
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`D1A_AUTH0_SPA_ORIGIN_ERROR=${error.message}\n`)
    process.exitCode = 1
  })
}
