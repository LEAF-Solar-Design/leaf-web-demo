#!/usr/bin/env node

import { createHash, createPrivateKey, createPublicKey, randomBytes, sign } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { chmod, open, readFile, realpath, rename, stat } from 'node:fs/promises'
import { dirname, isAbsolute, relative, resolve } from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

export const SCHEMA = 'leaf.production-auth0-spa-origin-interactive-proof.v1'
export const SIGNED_SCHEMA = 'leaf.production-auth0-spa-origin-interactive-signed.v1'
export const TARGET_ORIGIN = 'https://platform.leafdesign.ai'
export const ISSUER = 'https://leafautomation.us.auth0.com/'
export const AUDIENCE = 'https://api.leafdesign.ai'
export const CLIENT_ID = 'zkJjr0ZFtcyQjyJ8e4zdkdgzoMaVWt5O'
export const COLLECTOR_REPOSITORY = 'LEAF-Solar-Design/leaf-web-demo'
export const COLLECTOR_PATH = 'web/scripts/deployed_auth0_production_spa_origin_acceptance.mjs'

const SHA256 = /^[0-9a-f]{64}$/
const SOURCE_SHA = /^[0-9a-f]{40}$/
const JWT = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/
const TASK_DEFINITION = /^arn:aws:ecs:us-east-1:807034087062:task-definition\/leaf-platform-web:[1-9][0-9]*$/
const TASK_ARN = /^arn:aws:ecs:us-east-1:807034087062:task\/leaf-automation-production\/[0-9a-f]{32}$/
const IMAGE_DIGEST = /^sha256:[0-9a-f]{64}$/
const TARGET_GROUP = /^arn:aws:elasticloadbalancing:us-east-1:807034087062:targetgroup\/leaf-prod-platform-web\/[0-9a-f]+$/
const ASSET_PATH = /^\/assets\/[A-Za-z0-9_-]+\.js$/
const KEY_ID = /^[A-Za-z0-9._:-]{3,128}$/

function fail(message) {
  throw new Error(message)
}

export function sha256(value) {
  return createHash('sha256').update(value).digest('hex')
}

export function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
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
  let header
  let claims
  try {
    header = JSON.parse(Buffer.from(token.split('.')[0], 'base64url').toString('utf8'))
    claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString('utf8'))
  } catch {
    fail('browser access token payload is invalid')
  }
  if (header.alg !== 'RS256') fail('browser access token is not Auth0 RS256 signed')
  const subject = requiredString(claims.sub, 'JWT subject')
  const tenant = requiredString(claims['https://leafdesign.ai/tenant_id'], 'JWT tenant claim')
  if (!subject.startsWith('auth0|')) fail('interactive principal is not an Auth0 database account')
  if (claims.iss !== ISSUER) fail('JWT issuer is not exact')
  const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud]
  if (!audiences.includes(AUDIENCE)) fail('JWT audience is not exact')
  if (claims.azp !== CLIENT_ID) fail('JWT authorized party is not the production SPA')
  return {
    subject_sha256: sha256(subject),
    tenant_id_sha256: sha256(tenant),
    access_token_sha256: sha256(token),
    jwt_alg_rs256: true,
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
  exactKeys(session, ['api_status', 'subject_sha256', 'tenant_id_sha256', 'access_token_sha256', 'jwt_alg_rs256'], label)
  if (session.api_status !== 200 || session.jwt_alg_rs256 !== true) fail(`${label} did not prove a signed protected session`)
  for (const key of ['subject_sha256', 'tenant_id_sha256', 'access_token_sha256']) {
    if (!SHA256.test(session[key])) fail(`${label} ${key} is invalid`)
  }
}

function validateSourceTuple(value, label = 'deployed web source tuple') {
  exactKeys(value, [
    'schema', 'environment', 'target_origin', 'service_name', 'task_definition_arn',
    'task_arn', 'task_private_ipv4_sha256', 'target_group_arn', 'target_port', 'target_health',
    'image_digest', 'source_revision', 'asset_path', 'asset_sha256',
  ], label)
  if (value.schema !== 'leaf.production-auth0-spa-origin-web-source.v1' || value.environment !== 'production' || value.target_origin !== TARGET_ORIGIN) fail(`${label} identity is invalid`)
  if (value.service_name !== 'leaf-platform-web') fail(`${label} service is invalid`)
  if (!TASK_DEFINITION.test(value.task_definition_arn) || value.task_definition_arn.split('/').at(-1).split(':')[0] !== value.service_name) fail(`${label} task definition is invalid`)
  if (!TASK_ARN.test(value.task_arn) || !SHA256.test(value.task_private_ipv4_sha256) ||
      !TARGET_GROUP.test(value.target_group_arn) || value.target_port !== 8080 || value.target_health !== 'healthy') fail(`${label} task target binding is invalid`)
  if (!IMAGE_DIGEST.test(value.image_digest) || !SOURCE_SHA.test(value.source_revision)) fail(`${label} immutable source is invalid`)
  if (!ASSET_PATH.test(value.asset_path) || !SHA256.test(value.asset_sha256)) fail(`${label} asset identity is invalid`)
}

function validateExpectedPrincipal(value) {
  exactKeys(value, ['subject_sha256', 'tenant_id_sha256', 'reviewed'], 'expected principal A')
  if (!SHA256.test(value.subject_sha256) || !SHA256.test(value.tenant_id_sha256) || value.reviewed !== true) fail('expected principal A is invalid')
}

function validateCollectorCode(value) {
  exactKeys(value, ['repository', 'source_sha', 'script_path', 'script_sha256'], 'collector code identity')
  if (value.repository !== COLLECTOR_REPOSITORY || !SOURCE_SHA.test(value.source_sha) ||
      value.script_path !== COLLECTOR_PATH || !SHA256.test(value.script_sha256)) fail('collector code identity is invalid')
}

export async function deriveCollectorCode(dependencies = {}) {
  const repository = await realpath(resolve(dirname(fileURLToPath(import.meta.url)), '../..'))
  const script = resolve(repository, COLLECTOR_PATH)
  const run = dependencies.execFileSync || execFileSync
  const sourceSha = String(run('git', ['-C', repository, 'rev-parse', 'HEAD'], { encoding: 'utf8' })).trim()
  const dirty = String(run('git', ['-C', repository, 'status', '--porcelain', '--untracked-files=no', '--', COLLECTOR_PATH], { encoding: 'utf8' })).trim()
  if (!SOURCE_SHA.test(sourceSha) || dirty) fail('collector checkout is not an exact clean commit')
  return {
    repository: COLLECTOR_REPOSITORY,
    source_sha: sourceSha,
    script_path: COLLECTOR_PATH,
    script_sha256: sha256(await readFile(script)),
  }
}

export function validateInteractiveProof(proof, config) {
  exactKeys(proof, [
    'schema', 'plan_version', 'environment', 'target_origin', 'expected_source_revision', 'observed_source_revision',
    'issuer', 'audience', 'client_id_sha256', 'deployed_web_source', 'expected_principal_a', 'preparation', 'authorization_cycles', 'logout',
    'sessions', 'route_interceptions', 'observed_origins', 'operator_interactive', 'started_at',
    'completed_at', 'raw_tokens_recorded', 'raw_auth_codes_recorded', 'raw_subjects_recorded',
    'raw_tenant_ids_recorded', 'passwords_recorded', 'mfa_values_recorded', 'secrets_recorded',
    'signing', 'collector_code', 'nonce_sha256',
  ], 'interactive proof')
  if (proof.schema !== SCHEMA || proof.plan_version !== '1.32' || proof.environment !== 'production') fail('interactive proof schema, plan, or environment is invalid')
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
  const expectedOrigins = [TARGET_ORIGIN, new URL(ISSUER).origin].sort()
  if (!Array.isArray(proof.observed_origins) || JSON.stringify([...proof.observed_origins].sort()) !== JSON.stringify(expectedOrigins)) {
    fail('browser contacted an unexpected origin')
  }
  if (proof.operator_interactive !== true) fail('operator interaction was not recorded')
  exactKeys(proof.signing, ['algorithm', 'key_id', 'public_key_sha256'], 'interactive signing identity')
  if (proof.signing.algorithm !== 'Ed25519' || proof.signing.key_id !== config.signingKeyId ||
      proof.signing.public_key_sha256 !== config.signingPublicKeySha256) fail('interactive signing identity is not exact')
  validateCollectorCode(proof.collector_code)
  if (JSON.stringify(proof.collector_code) !== JSON.stringify(config.collectorCode)) fail('collector code differs from reviewed preparation')
  if (!SHA256.test(proof.nonce_sha256)) fail('interactive replay nonce digest is invalid')
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

async function protectedSession(page) {
  const result = await page.evaluate(async () => {
    const token = window.localStorage.getItem('leaf.jwt') || ''
    const response = await fetch('/api/session?dwg=rooftop_demo', {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    return { status: response.status, token }
  })
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
  await page.waitForFunction(() => !!window.localStorage.getItem('leaf.jwt'), null, { timeout: 60_000 })
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
    await page.getByRole('button', { name: 'Sign out', exact: true }).first().click()
    await page.waitForURL((url) => url.origin === TARGET_ORIGIN, { timeout: 60_000 })
    await page.waitForFunction(() => !window.localStorage.getItem('leaf.jwt'), null, { timeout: 60_000 })
    const logoutReturnOriginExact = new URL(page.url()).origin === TARGET_ORIGIN
    const logoutTokenAbsent = await page.evaluate(() => !window.localStorage.getItem('leaf.jwt'))
    const afterLogout = await signedOutStatus(page)
    const secondCycle = await waitForInteractiveLogin(page, authorizationRequests, callbacks, 2, notify)
    const second = await protectedSession(page)
    const proof = {
      schema: SCHEMA,
      plan_version: '1.32',
      environment: 'production',
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
      signing: {
        algorithm: 'Ed25519', key_id: config.signingKeyId,
        public_key_sha256: config.signingPublicKeySha256,
      },
      collector_code: config.collectorCode,
      nonce_sha256: sha256(randomBytes(32)),
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
    'web-task-arn', 'web-task-private-ip-sha256', 'web-target-group-arn',
    'web-image-digest', 'asset-path', 'asset-sha256', 'expected-subject-sha256',
    'expected-tenant-id-sha256', 'signing-key', 'signing-key-id', 'output',
  ]
  if (JSON.stringify(Object.keys(values).sort()) !== JSON.stringify(expected.sort())) fail('unexpected or missing arguments')
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(values.challenge)) fail('challenge is invalid')
  if (!SHA256.test(values['preparation-payload-sha256'])) fail('preparation payload digest is invalid')
  if (!SOURCE_SHA.test(values['source-sha'])) fail('source SHA is invalid')
  if (!KEY_ID.test(values['signing-key-id'])) fail('signing key ID is invalid')
  const deployedWebSource = {
    schema: 'leaf.production-auth0-spa-origin-web-source.v1', environment: 'production', target_origin: TARGET_ORIGIN,
    service_name: values['web-service'], task_definition_arn: values['web-task-definition'],
    task_arn: values['web-task-arn'], task_private_ipv4_sha256: values['web-task-private-ip-sha256'],
    target_group_arn: values['web-target-group-arn'], target_port: 8080, target_health: 'healthy',
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
    signingKeyPath: resolve(values['signing-key']),
    signingKeyId: values['signing-key-id'],
    output: resolve(values.output),
  }
}

export async function loadOutsideRepositorySigner(config) {
  const repository = await realpath(resolve(dirname(fileURLToPath(import.meta.url)), '../..'))
  const keyPath = await realpath(config.signingKeyPath)
  const relation = relative(repository, keyPath)
  if (!relation.startsWith('..') && !isAbsolute(relation)) {
    fail('signing key must be outside the repository')
  }
  const keyStat = await stat(keyPath)
  if (!keyStat.isFile() || (process.platform !== 'win32' && (keyStat.mode & 0o077) !== 0)) {
    fail('signing key must be a mode-0600 regular file')
  }
  const privateKey = createPrivateKey(await readFile(keyPath))
  if (privateKey.asymmetricKeyType !== 'ed25519') fail('signing key is not Ed25519')
  const jwk = createPublicKey(privateKey).export({ format: 'jwk' })
  if (jwk.kty !== 'OKP' || jwk.crv !== 'Ed25519' || typeof jwk.x !== 'string') fail('Ed25519 public key export is invalid')
  const publicKey = Buffer.from(jwk.x, 'base64url')
  if (publicKey.length !== 32) fail('Ed25519 public key length is invalid')
  return { privateKey, publicKeySha256: sha256(publicKey) }
}

export function signInteractiveProof(proof, config, signer) {
  if (signer.publicKeySha256 !== config.signingPublicKeySha256) fail('signing key does not match preparation trust')
  validateInteractiveProof(proof, config)
  const signature = sign(null, Buffer.from(canonical(proof), 'utf8'), signer.privateKey)
  if (signature.length !== 64) fail('Ed25519 signature length is invalid')
  return { schema: SIGNED_SCHEMA, payload: proof, signature_b64: signature.toString('base64') }
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
  config.collectorCode = await deriveCollectorCode()
  const signer = await loadOutsideRepositorySigner(config)
  config.signingPublicKeySha256 = signer.publicKeySha256
  const proof = await collectInteractiveProof(config)
  const envelope = signInteractiveProof(proof, config, signer)
  await writePrivate(config.output, envelope)
  process.stdout.write(`Wrote sanitized signed interactive proof to ${config.output}\n`)
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`PRODUCTION_D1A_AUTH0_SPA_ORIGIN_ERROR=${error.message}\n`)
    process.exitCode = 1
  })
}
