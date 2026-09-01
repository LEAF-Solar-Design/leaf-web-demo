#!/usr/bin/env node

import { createHash, createPrivateKey, createPublicKey, randomBytes, sign, verify } from 'node:crypto'
import { chmod, open, readFile, realpath, rename, stat } from 'node:fs/promises'
import { dirname, isAbsolute, relative, resolve } from 'node:path'
import process from 'node:process'
import { createInterface } from 'node:readline/promises'
import { fileURLToPath } from 'node:url'

export const SIGNED_SCHEMA = 'leaf.staging-auth0-synthetic-interactive-signed.v1'
export const PAYLOAD_SCHEMA = 'leaf.staging-auth0-synthetic-interactive.v1'
export const READINESS_SIGNED_SCHEMA = 'leaf.staging-auth0-synthetic-collector-readiness-signed.v1'
export const READINESS_SCHEMA = 'leaf.staging-auth0-synthetic-collector-readiness.v1'
export const ACTIVATION_CONTRACT_VERSION = 'leaf.staging-auth0-synthetic-activation.v1'
export const ACTIVATION_SCHEMA = 'leaf.staging-auth0-synthetic-users.v1'
export const TARGET_ORIGIN = 'https://platform-staging.leafdesign.ai'
export const AUTH0_ORIGIN = 'https://leafautomation.us.auth0.com'
export const CLIENT_ID = 'zkJjr0ZFtcyQjyJ8e4zdkdgzoMaVWt5O'
export const AUDIENCE = 'https://api.leafdesign.ai'
export const PLAN_VERSION = '1.32'

const SHA256 = /^[0-9a-f]{64}$/
const SOURCE_SHA = /^[0-9a-f]{40}$/
const KEY_ID = /^[A-Za-z0-9._:-]{3,128}$/
const JWT = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/
const RESET_PATH = /(?:\/lo\/reset|\/u\/reset|reset-password|password-reset)/i
const MFA_INTERACTION_PATH = /\/u\/mfa-(?:otp|phone|push|webauthn-[a-z-]+|recovery-code)-(?:enrollment|challenge)/i

function fail(message) { throw new Error(message) }

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

function exactKeys(value, keys, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(`${label} must be an object`)
  if (JSON.stringify(Object.keys(value).sort()) !== JSON.stringify([...keys].sort())) fail(`${label} has unexpected fields`)
  return value
}

function timestamp(value, label) {
  if (typeof value !== 'string' || !value.endsWith('Z') || !Number.isFinite(Date.parse(value))) fail(`${label} is invalid`)
  return Date.parse(value)
}

function requireHash(value, label) {
  if (typeof value !== 'string' || !SHA256.test(value)) fail(`${label} is invalid`)
  return value
}

function validatePrincipal(value, label) {
  exactKeys(value, ['email_sha256', 'user_sha256', 'tenant_sha256', 'blocked', 'email_verified', 'mfa_enrolled', 'created', 'mutated'], `principal ${label}`)
  for (const key of ['email_sha256', 'user_sha256', 'tenant_sha256']) requireHash(value[key], `principal ${label} ${key}`)
  for (const key of ['blocked', 'email_verified', 'mfa_enrolled', 'created', 'mutated']) {
    if (typeof value[key] !== 'boolean') fail(`principal ${label} ${key} is invalid`)
  }
}

export function validateActivationReceipt(receipt, config, observedAt = new Date()) {
  exactKeys(receipt, ['schema', 'plan_version', 'mode', 'source_sha', 'run', 'verified_at', 'authority', 'principals', 'phase_binding', 'safety'], 'activation receipt')
  if (receipt.schema !== ACTIVATION_SCHEMA || receipt.plan_version !== PLAN_VERSION || receipt.mode !== 'activate' || !SOURCE_SHA.test(receipt.source_sha)) fail('activation receipt identity is invalid')
  exactKeys(receipt.run, ['id', 'attempt'], 'activation run')
  if (!/^[1-9][0-9]*$/.test(receipt.run.id) || !/^[1-9][0-9]*$/.test(receipt.run.attempt)) fail('activation run identity is invalid')
  const activationAt = timestamp(receipt.verified_at, 'activation verified_at')
  const now = observedAt.getTime()
  if (activationAt > now || now - activationAt > 86_400_000) fail('activation receipt is stale or from the future')
  exactKeys(receipt.authority, ['tenant_domain_sha256', 'management_scopes_sha256', 'management_scope_count', 'active_actions_sha256', 'active_action_count', 'active_actions_source_sha'], 'activation authority')
  for (const key of ['tenant_domain_sha256', 'management_scopes_sha256', 'active_actions_sha256']) requireHash(receipt.authority[key], `activation ${key}`)
  if (!Number.isInteger(receipt.authority.management_scope_count) || !Number.isInteger(receipt.authority.active_action_count)) fail('activation authority counts are invalid')
  if (receipt.authority.active_actions_source_sha !== receipt.source_sha) fail('activation action authority source differs')
  exactKeys(receipt.principals, ['A', 'B'], 'activation principals')
  validatePrincipal(receipt.principals.A, 'A')
  validatePrincipal(receipt.principals.B, 'B')
  const principal = receipt.principals.B
  if (principal.email_sha256 !== sha256(config.email) || principal.tenant_sha256 !== sha256(config.tenantId) || principal.blocked !== false || principal.email_verified !== true) fail('activation principal B is not ready for interactive qualification')
  exactKeys(receipt.phase_binding, [
    'prior_receipt_sha256', 'prior_receipt_run_id', 'prior_receipt_run_attempt',
    'collector_readiness_sha256', 'collector_source_sha', 'collector_readiness_challenge_sha256',
    'collector_readiness_run_id', 'collector_readiness_run_attempt', 'prior_receipt_artifact_sha256',
    'collector_readiness_artifact_sha256', 'interactive_receipt_sha256', 'interactive_receipt_artifact_sha256',
    'interactive_receipt_run_id', 'interactive_receipt_run_attempt', 'interactive_signing_key_id_sha256',
    'interactive_signing_public_key_sha256', 'interactive_challenge_sha256',
  ], 'activation phase binding')
  const binding = receipt.phase_binding
  for (const key of ['prior_receipt_sha256', 'prior_receipt_artifact_sha256', 'collector_readiness_sha256', 'collector_readiness_artifact_sha256', 'collector_readiness_challenge_sha256', 'interactive_signing_key_id_sha256', 'interactive_signing_public_key_sha256']) requireHash(binding[key], `activation ${key}`)
  if (binding.collector_source_sha !== config.collectorSourceSha || binding.interactive_signing_key_id_sha256 !== sha256(config.signingKeyId) || binding.interactive_signing_public_key_sha256 !== config.signingPublicKeySha256) fail('activation collector or signing identity differs')
  if (binding.interactive_receipt_sha256 !== null || binding.interactive_receipt_artifact_sha256 !== null || binding.interactive_receipt_run_id !== null || binding.interactive_receipt_run_attempt !== null || binding.interactive_challenge_sha256 !== null) fail('activation receipt was already used for interactive finalization')
  for (const key of ['prior_receipt_run_id', 'prior_receipt_run_attempt', 'collector_readiness_run_id', 'collector_readiness_run_attempt']) {
    if (!/^[1-9][0-9]*$/.test(binding[key])) fail(`activation ${key} is invalid`)
  }
  exactKeys(receipt.safety, ['principal_a_mutation', 'delete_authority', 'password_retained', 'raw_identifiers_retained', 'raw_api_body_retained', 'interactive_credentials_outside_workflow'], 'activation safety')
  if (receipt.safety.principal_a_mutation !== false || receipt.safety.delete_authority !== false || receipt.safety.password_retained !== false || receipt.safety.raw_identifiers_retained !== false || receipt.safety.raw_api_body_retained !== false || receipt.safety.interactive_credentials_outside_workflow !== true) fail('activation safety contract differs')
  return receipt
}

export async function loadActivation(path, config, observedAt = new Date(), mode = 'interactive') {
  const bytes = await readFile(path)
  let value
  try { value = JSON.parse(bytes.toString('utf8')) } catch { fail('activation artifact is not valid UTF-8 JSON') }
  const receipt = mode === 'readiness'
    ? validatePreparationReceipt(value, config, observedAt)
    : validateActivationReceipt(value, config, observedAt)
  return { receipt, digest: sha256(bytes), bytes }
}

export async function loadOutsideRepositorySigner(config) {
  const repository = await realpath(resolve(dirname(fileURLToPath(import.meta.url)), '../..'))
  const keyPath = await realpath(config.signingKeyPath)
  const relation = relative(repository, keyPath)
  if (!relation.startsWith('..') && !isAbsolute(relation)) fail('signing key must be outside the repository')
  const keyStat = await stat(keyPath)
  if (!keyStat.isFile() || (process.platform !== 'win32' && (keyStat.mode & 0o077) !== 0)) fail('signing key must be a mode-0600 regular file')
  const privateKey = createPrivateKey(await readFile(keyPath))
  if (privateKey.asymmetricKeyType !== 'ed25519') fail('signing key is not Ed25519')
  const publicKey = createPublicKey(privateKey)
  const jwk = publicKey.export({ format: 'jwk' })
  const raw = Buffer.from(jwk.x || '', 'base64url')
  if (jwk.kty !== 'OKP' || jwk.crv !== 'Ed25519' || raw.length !== 32) fail('Ed25519 public key is invalid')
  return { privateKey, publicKey, publicKeySha256: sha256(raw) }
}

function parseSessionToken(token, principal) {
  if (typeof token !== 'string' || !JWT.test(token)) fail('staging access token is not a compact JWT')
  let header, claims
  try {
    header = JSON.parse(Buffer.from(token.split('.')[0], 'base64url').toString('utf8'))
    claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString('utf8'))
  } catch { fail('staging access token is invalid') }
  const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud]
  const subject = claims.sub
  const tenant = claims['https://leafdesign.ai/tenant_id']
  if (header.alg !== 'RS256' || claims.iss !== `${AUTH0_ORIGIN}/` || !audiences.some((aud) => aud === AUDIENCE) || claims.azp !== CLIENT_ID || typeof subject !== 'string' || typeof tenant !== 'string' || sha256(subject) !== principal.user_sha256 || sha256(tenant) !== principal.tenant_sha256) fail('authenticated session is not exact principal B')
  return { subjectSha256: sha256(subject), tenantSha256: sha256(tenant), tokenSha256: sha256(token) }
}

function authorizationObservation(url) {
  const query = url.searchParams
  return url.origin === AUTH0_ORIGIN && url.pathname.endsWith('/authorize') && query.get('response_type') === 'code' && query.get('redirect_uri') === TARGET_ORIGIN && query.get('audience') === AUDIENCE && query.get('client_id') === CLIENT_ID && !!query.get('state') && !!query.get('code_challenge') && query.get('code_challenge_method') === 'S256'
}

async function defaultConfirm(message) {
  const input = createInterface({ input: process.stdin, output: process.stderr })
  try { await input.question(message) } finally { input.close() }
}

export async function collectPrincipalB(config, activation, dependencies = {}) {
  const playwright = dependencies.playwright || await import('@playwright/test')
  const fetchImpl = dependencies.fetch || fetch
  const confirm = dependencies.confirm || defaultConfirm
  const notify = dependencies.notify || ((message) => process.stderr.write(message))
  const now = dependencies.now || (() => new Date())
  const browser = await playwright.chromium.launch({ headless: false })
  let resetObserved = false
  let mfaEnrolledAt = null
  let authorizeExact = false
  let authorizeCount = 0
  let callbackCount = 0
  try {
    const context = await browser.newContext()
    const page = await context.newPage()
    const observeRequest = (request) => {
      const url = new URL(request.url())
      if (url.origin === AUTH0_ORIGIN && RESET_PATH.test(url.pathname)) resetObserved = true
      if (url.origin === AUTH0_ORIGIN && MFA_INTERACTION_PATH.test(url.pathname) && !mfaEnrolledAt) mfaEnrolledAt = now().toISOString()
      if (url.origin === AUTH0_ORIGIN && url.pathname.endsWith('/authorize')) {
        authorizeCount += 1
        authorizeExact = authorizationObservation(url)
      }
      if (url.origin === TARGET_ORIGIN && url.searchParams.has('code') && url.searchParams.has('state')) callbackCount += 1
    }
    const observePage = (candidate) => candidate.on('request', observeRequest)
    observePage(page)
    context.on?.('page', observePage)
    await page.goto(`${TARGET_ORIGIN}/try`, { waitUntil: 'networkidle', timeout: 60_000 })
    const resetResponse = await fetchImpl(`${AUTH0_ORIGIN}/dbconnections/change_password`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ client_id: CLIENT_ID, email: config.email, connection: 'Username-Password-Authentication' }),
    })
    if (resetResponse.status !== 200) fail('Auth0 password reset request was not accepted')
    notify('Open the reset email link in this visible browser, set a new password, then return here. Do not paste the password or MFA value into this terminal.\n')
    await confirm('Press Enter only after the password reset completes in the visible browser. ')
    if (!resetObserved) fail('a real Auth0 password reset route was not observed in the visible browser')
    const passwordResetAt = now().toISOString()
    if (timestamp(passwordResetAt, 'password reset time') <= timestamp(activation.receipt.verified_at, 'activation time')) fail('password reset did not occur after activation')
    await page.goto(`${TARGET_ORIGIN}/try`, { waitUntil: 'networkidle', timeout: 60_000 })
    const signedOut = await page.evaluate(async () => (await fetch('/api/session?dwg=rooftop_demo')).status)
    if (signedOut !== 401) fail('principal B was not signed out before PKCE login')
    await page.getByRole('button', { name: 'Sign in', exact: true }).first().click()
    notify('Complete Universal Login and MFA enrollment in the visible browser. Do not paste the password or MFA value into this terminal.\n')
    await page.waitForURL((url) => url.origin === TARGET_ORIGIN && !url.searchParams.has('code') && !url.searchParams.has('state'), { timeout: 10 * 60_000 })
    await page.waitForFunction(() => !!window.localStorage.getItem('leaf.jwt'), null, { timeout: 60_000 })
    if (authorizeCount !== 1 || callbackCount !== 1 || !authorizeExact) fail('exactly one PKCE S256 authorization and callback were not observed')
    if (!mfaEnrolledAt || timestamp(mfaEnrolledAt, 'MFA interaction time') < timestamp(passwordResetAt, 'password reset time')) fail('post-reset MFA enrollment or challenge was not observed')
    const session = await page.evaluate(async () => {
      const token = window.localStorage.getItem('leaf.jwt') || ''
      const response = await fetch('/api/session?dwg=rooftop_demo', { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      return { status: response.status, token }
    })
    if (session.status !== 200) fail('principal B protected session did not return 200')
    parseSessionToken(session.token, activation.receipt.principals.B)
    const pkceLoginAt = now().toISOString()
    const collectedAt = now().toISOString()
    return {
      schema: PAYLOAD_SCHEMA,
      plan_version: PLAN_VERSION,
      source_sha: activation.receipt.source_sha,
      key_id: config.signingKeyId,
      activation_receipt_sha256: activation.digest,
      challenge_sha256: sha256(`leaf-staging-auth0-interactive:${activation.digest}`),
      principal_b_email_sha256: activation.receipt.principals.B.email_sha256,
      principal_b_user_sha256: activation.receipt.principals.B.user_sha256,
      principal_b_tenant_sha256: activation.receipt.principals.B.tenant_sha256,
      email_verified_at: activation.receipt.verified_at,
      password_reset_at: passwordResetAt,
      mfa_enrolled_at: mfaEnrolledAt,
      pkce_login_at: pkceLoginAt,
      collected_at: collectedAt,
      nonce_sha256: sha256(randomBytes(32)),
    }
  } finally { await browser.close() }
}

export function signEnvelope(payload, signer) {
  const signature = sign(null, Buffer.from(canonical(payload)), signer.privateKey)
  return { schema: SIGNED_SCHEMA, payload, signature_b64: signature.toString('base64') }
}

export function buildReadiness(preparation, config, signer, issuedAt = new Date()) {
  const receipt = validatePreparationReceipt(preparation.receipt, config, issuedAt)
  const expiresAt = new Date(issuedAt.getTime() + 3_600_000).toISOString()
  const payload = {
    schema: READINESS_SCHEMA, plan_version: PLAN_VERSION,
    activation_contract_version: ACTIVATION_CONTRACT_VERSION,
    provisioner_source_sha: receipt.source_sha, collector_source_sha: config.collectorSourceSha,
    key_id: config.signingKeyId, signing_public_key_sha256: signer.publicKeySha256,
    issued_at: issuedAt.toISOString(), expires_at: expiresAt,
    capability_nonce_sha256: sha256(randomBytes(32)),
    prior_receipt_sha256: preparation.digest,
    prior_receipt_artifact_sha256: config.priorArtifactSha256,
  }
  payload.challenge_sha256 = sha256(canonical({
    activation_contract_version: payload.activation_contract_version,
    provisioner_source_sha: payload.provisioner_source_sha,
    collector_source_sha: payload.collector_source_sha, key_id: payload.key_id,
    signing_public_key_sha256: payload.signing_public_key_sha256,
    issued_at: payload.issued_at, expires_at: payload.expires_at,
    capability_nonce_sha256: payload.capability_nonce_sha256,
    prior_receipt_sha256: payload.prior_receipt_sha256,
    prior_receipt_artifact_sha256: payload.prior_receipt_artifact_sha256,
  }))
  return { schema: READINESS_SIGNED_SCHEMA, payload, signature_b64: sign(null, Buffer.from(canonical(payload)), signer.privateKey).toString('base64') }
}

export function validatePreparationReceipt(receipt, config, observedAt = new Date()) {
  const prepared = structuredClone(receipt)
  prepared.mode = 'activate'
  prepared.principals.B.blocked = false
  prepared.phase_binding = {
    prior_receipt_sha256: '0'.repeat(64), prior_receipt_run_id: '1', prior_receipt_run_attempt: '1',
    prior_receipt_artifact_sha256: '0'.repeat(64), collector_readiness_sha256: '0'.repeat(64),
    collector_source_sha: config.collectorSourceSha, collector_readiness_challenge_sha256: '0'.repeat(64),
    collector_readiness_run_id: '1', collector_readiness_run_attempt: '1', collector_readiness_artifact_sha256: '0'.repeat(64),
    interactive_receipt_sha256: null, interactive_receipt_artifact_sha256: null,
    interactive_receipt_run_id: null, interactive_receipt_run_attempt: null,
    interactive_signing_key_id_sha256: sha256(config.signingKeyId),
    interactive_signing_public_key_sha256: config.signingPublicKeySha256, interactive_challenge_sha256: null,
  }
  validateActivationReceipt(prepared, config, observedAt)
  if (receipt.mode !== 'prepare' || receipt.principals.B.blocked !== true) fail('preparation principal B is not quarantined')
  return receipt
}

export function validateSignedEnvelope(envelope, activation, config, signer, observedAt = new Date()) {
  exactKeys(envelope, ['schema', 'payload', 'signature_b64'], 'signed interactive envelope')
  if (envelope.schema !== SIGNED_SCHEMA || typeof envelope.signature_b64 !== 'string') fail('signed interactive envelope identity is invalid')
  const signature = Buffer.from(envelope.signature_b64, 'base64')
  if (!/^[A-Za-z0-9+/]{86}==$/.test(envelope.signature_b64) || signature.length !== 64 || signature.toString('base64') !== envelope.signature_b64 || !verify(null, Buffer.from(canonical(envelope.payload)), signer.publicKey, signature)) fail('interactive Ed25519 signature is invalid')
  const payload = exactKeys(envelope.payload, ['schema', 'plan_version', 'source_sha', 'key_id', 'activation_receipt_sha256', 'challenge_sha256', 'principal_b_email_sha256', 'principal_b_user_sha256', 'principal_b_tenant_sha256', 'email_verified_at', 'password_reset_at', 'mfa_enrolled_at', 'pkce_login_at', 'collected_at', 'nonce_sha256'], 'interactive payload')
  if (payload.schema !== PAYLOAD_SCHEMA || payload.plan_version !== PLAN_VERSION || payload.source_sha !== activation.receipt.source_sha || payload.key_id !== config.signingKeyId || payload.activation_receipt_sha256 !== activation.digest || payload.challenge_sha256 !== sha256(`leaf-staging-auth0-interactive:${activation.digest}`)) fail('interactive activation or challenge binding differs')
  for (const key of ['principal_b_email_sha256', 'principal_b_user_sha256', 'principal_b_tenant_sha256']) {
    requireHash(payload[key], key)
    if (payload[key] !== activation.receipt.principals.B[key.replace('principal_b_', '')]) fail('interactive principal B binding differs')
  }
  requireHash(payload.nonce_sha256, 'interactive nonce')
  const activationAt = timestamp(activation.receipt.verified_at, 'activation time')
  const emailAt = timestamp(payload.email_verified_at, 'email verified time')
  const resetAt = timestamp(payload.password_reset_at, 'password reset time')
  const mfaAt = timestamp(payload.mfa_enrolled_at, 'MFA enrollment time')
  const loginAt = timestamp(payload.pkce_login_at, 'PKCE login time')
  const collectedAt = timestamp(payload.collected_at, 'collection time')
  const now = observedAt.getTime()
  if (!(emailAt <= collectedAt && activationAt < resetAt && resetAt <= mfaAt && mfaAt <= loginAt && loginAt <= collectedAt && collectedAt <= now && now - collectedAt <= 3_600_000)) fail('interactive event order or freshness is invalid')
  const encoded = canonical(envelope)
  if (encoded.includes(config.email) || encoded.includes(config.tenantId) || /(?:^|[^A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:$|[^A-Za-z0-9_-])/.test(encoded)) fail('signed envelope retained a raw identifier or token')
  return envelope
}

export function parseArgs(argv) {
  const values = {}
  for (let index = 0; index < argv.length; index += 2) {
    if (!argv[index]?.startsWith('--') || argv[index + 1] === undefined) fail('arguments must be --name value pairs')
    values[argv[index].slice(2)] = argv[index + 1]
  }
  const expected = ['mode', 'receipt', 'output']
  if (JSON.stringify(Object.keys(values).sort()) !== JSON.stringify(expected.sort()) || !['readiness', 'interactive'].includes(values.mode)) fail('collector arguments are invalid')
  const email = process.env.LEAF_STAGING_SYNTHETIC_TENANT_B_EMAIL || ''
  const tenantId = process.env.LEAF_STAGING_SYNTHETIC_TENANT_B_ID || ''
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(tenantId)) fail('principal B environment identity is invalid')
  const collectorSourceSha = process.env.GITHUB_SHA || ''
  const signingKeyPath = process.env.LEAF_STAGING_SYNTHETIC_INTERACTIVE_ED25519_PRIVATE_KEY_PATH || ''
  const signingKeyId = process.env.LEAF_STAGING_SYNTHETIC_INTERACTIVE_ED25519_KEY_ID || ''
  const priorArtifactSha256 = process.env.LEAF_STAGING_SYNTHETIC_PRIOR_ARTIFACT_SHA256 || ''
  if (!SOURCE_SHA.test(collectorSourceSha) || !KEY_ID.test(signingKeyId) || !signingKeyPath || (values.mode === 'readiness' && !SHA256.test(priorArtifactSha256))) fail('collector protected environment is invalid')
  return { mode: values.mode, activationPath: resolve(values.receipt), collectorSourceSha, signingKeyPath: resolve(signingKeyPath), signingKeyId, priorArtifactSha256, output: resolve(values.output), email, tenantId }
}

async function writePrivate(path, value) {
  const temporary = `${path}.tmp-${process.pid}`
  const handle = await open(temporary, 'wx', 0o600)
  try { await handle.writeFile(`${canonical(value)}\n`, 'utf8') } finally { await handle.close() }
  await rename(temporary, path)
  await chmod(path, 0o600)
}

async function main() {
  const config = parseArgs(process.argv.slice(2))
  const signer = await loadOutsideRepositorySigner(config)
  config.signingPublicKeySha256 = signer.publicKeySha256
  const activation = await loadActivation(config.activationPath, config, new Date(), config.mode)
  const envelope = config.mode === 'readiness'
    ? buildReadiness(activation, config, signer)
    : signEnvelope(await collectPrincipalB(config, activation), signer)
  if (config.mode === 'interactive') validateSignedEnvelope(envelope, activation, config, signer)
  await writePrivate(config.output, envelope)
  process.stdout.write(`Wrote sanitized signed principal B proof to ${config.output}\n`)
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`STAGING_AUTH0_PRINCIPAL_B_COLLECTOR_ERROR=${error.message}\n`)
    process.exitCode = 1
  })
}
