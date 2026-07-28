#!/usr/bin/env node
/**
 * Production-only deployed acceptance driver for tenant-authored CAD.
 *
 * This entry point is intentionally separate from the staging driver. It can
 * run only in execute mode, against the exact production origin, with an exact
 * source/run confirmation and two short-lived Auth0 tokens carrying the signed
 * non-customer acceptance classification.
 */

import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { createRemoteJWKSet, jwtVerify } from 'jose'

import {
  AcceptanceError,
  buildReceipt,
  proveExecutedAuthorityIsolation,
  proveExecutedDrawingIsolation,
  provePinnedWriteRejections,
  provePersistedAcceptanceState,
  runApiPreflight,
  runBrowserAcceptance,
} from './deployed_authored_cad_acceptance.mjs'

const SOURCE_SHA = /^[a-f0-9]{40}$/
const RUN_ID = /^[a-z0-9][a-z0-9-]{5,49}$/
const SAFE_ID = /^[a-z0-9][a-z0-9_-]{0,62}$/
const JWT = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/
const PRODUCTION_WEB_ORIGIN = 'https://leaf-platform-web.vercel.app'
const PRODUCTION_API_ORIGIN = 'https://platform.leafdesign.ai'
const AUTH0_ISSUER = 'https://leafautomation.us.auth0.com/'
const AUTH0_AUDIENCE = 'https://api.leafdesign.ai'
const AUTH0_JWKS = new URL(`${AUTH0_ISSUER}.well-known/jwks.json`)
const CLAIM_NS = 'https://leafdesign.ai/'
const TENANT_CLASS = 'non_customer_acceptance'
const MAX_TOKEN_REMAINING_SECONDS = 90 * 60
const MAX_TOKEN_LIFETIME_SECONDS = 2 * 60 * 60

function required(env, name) {
  const value = String(env[name] || '').trim()
  if (!value) throw new AcceptanceError('configuration', `${name} is required`)
  return value
}

function exactProductionOrigin(value, name, expectedOrigin) {
  if (value !== expectedOrigin) {
    throw new AcceptanceError(
      'production_target',
      `${name} must be exactly ${expectedOrigin}`,
    )
  }
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    throw new AcceptanceError('production_target', `${name} must be an absolute URL`)
  }
  if (
    parsed.origin !== expectedOrigin
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
  ) {
    throw new AcceptanceError(
      'production_target',
      `${name} must be exactly ${expectedOrigin}`,
    )
  }
  return parsed
}

function expectedRequest(label, runId) {
  return label === 'A'
    ? `Production acceptance run ${runId} tenant A: create a drawing tool that adds a centered test prism.`
    : `Production acceptance run ${runId} tenant B: create a drawing tool that adds an offset test cylinder.`
}

function tenantConfig(env, label, runId) {
  const prefix = `LEAF_ACCEPTANCE_TENANT_${label}_`
  const id = required(env, `${prefix}ID`)
  const jwt = required(env, `${prefix}JWT`)
  const drawingId = required(env, `${prefix}DRAWING_ID`)
  const request = required(env, `${prefix}REQUEST`)
  if (!SAFE_ID.test(id)) {
    throw new AcceptanceError('configuration', `${prefix}ID is not a safe tenant id`)
  }
  if (!JWT.test(jwt)) {
    throw new AcceptanceError('configuration', `${prefix}JWT must be a compact JWT`)
  }
  if (drawingId !== `production-acceptance-${runId}-${label.toLowerCase()}`) {
    throw new AcceptanceError(
      'production_scope',
      `${prefix}DRAWING_ID is not the exact production acceptance drawing`,
    )
  }
  if (request !== expectedRequest(label, runId)) {
    throw new AcceptanceError(
      'production_scope',
      `${prefix}REQUEST is not the source-fixed production acceptance request`,
    )
  }
  return { label, id, jwt, drawingId, request }
}

export function validateProductionConfig(env = process.env, mode = '') {
  if (mode !== 'preflight' && mode !== 'execute') {
    throw new AcceptanceError(
      'production_target',
      'production acceptance requires explicit preflight or execute mode',
    )
  }
  if (required(env, 'LEAF_ACCEPTANCE_ENVIRONMENT') !== 'production') {
    throw new AcceptanceError(
      'production_target',
      'LEAF_ACCEPTANCE_ENVIRONMENT must be exactly production',
    )
  }
  const runId = required(env, 'LEAF_ACCEPTANCE_RUN_ID')
  if (!RUN_ID.test(runId)) {
    throw new AcceptanceError(
      'configuration',
      'LEAF_ACCEPTANCE_RUN_ID must be 6-50 lowercase letters, digits, or hyphens',
    )
  }
  const expectedRevision = required(env, 'LEAF_ACCEPTANCE_EXPECTED_REVISION').toLowerCase()
  if (!SOURCE_SHA.test(expectedRevision)) {
    throw new AcceptanceError(
      'configuration',
      'LEAF_ACCEPTANCE_EXPECTED_REVISION is not a source revision',
    )
  }
  const expectedConfirmation = `production-authored-acceptance:${expectedRevision}:${runId}`
  if (required(env, 'LEAF_ACCEPTANCE_PRODUCTION_CONFIRMATION') !== expectedConfirmation) {
    throw new AcceptanceError('production_target', 'production acceptance confirmation mismatch')
  }
  const webUrl = exactProductionOrigin(
    required(env, 'LEAF_ACCEPTANCE_WEB_URL'),
    'LEAF_ACCEPTANCE_WEB_URL',
    PRODUCTION_WEB_ORIGIN,
  )
  const apiUrl = exactProductionOrigin(
    required(env, 'LEAF_ACCEPTANCE_API_URL'),
    'LEAF_ACCEPTANCE_API_URL',
    PRODUCTION_API_ORIGIN,
  )
  const allowedHosts = `${webUrl.hostname},${apiUrl.hostname}`
  if (required(env, 'LEAF_ACCEPTANCE_ALLOWED_HOSTS') !== allowedHosts) {
    throw new AcceptanceError(
      'production_target',
      'LEAF_ACCEPTANCE_ALLOWED_HOSTS must contain only the exact production web and API hostnames',
    )
  }
  const tenants = [
    tenantConfig(env, 'A', runId),
    tenantConfig(env, 'B', runId),
  ]
  if (tenants[0].id === tenants[1].id || tenants[0].jwt === tenants[1].jwt) {
    throw new AcceptanceError(
      'tenant_identity',
      'the two production acceptance tenants and JWTs must be distinct',
    )
  }
  let publicationApprovalSecret = ''
  if (mode === 'execute') {
    const secretFile = required(
      env,
      'LEAF_ACCEPTANCE_PUBLICATION_APPROVAL_SECRET_FILE',
    )
    try {
      publicationApprovalSecret = readFileSync(resolve(secretFile), 'utf8')
    } catch {
      throw new AcceptanceError(
        'configuration',
        'LEAF_ACCEPTANCE_PUBLICATION_APPROVAL_SECRET_FILE must be readable',
      )
    }
  }
  if (mode === 'execute' && publicationApprovalSecret.length < 16) {
    throw new AcceptanceError(
      'configuration',
      'production publication approval secret file must contain at least 16 characters',
    )
  }
  return {
    environment: 'production',
    execute: mode === 'execute',
    mode,
    runId,
    webUrl: webUrl.origin,
    apiUrl: apiUrl.origin,
    allowedHosts: new Set([webUrl.hostname, apiUrl.hostname]),
    expectedRevision,
    publicationApprovalSecret,
    tenants,
  }
}

export async function verifyProductionTenantTokens(
  config,
  {
    keyResolver = createRemoteJWKSet(AUTH0_JWKS),
    now = Math.floor(Date.now() / 1000),
  } = {},
) {
  const identities = []
  for (const tenant of config.tenants) {
    let payload
    try {
      ({ payload } = await jwtVerify(tenant.jwt, keyResolver, {
        algorithms: ['RS256'],
        issuer: AUTH0_ISSUER,
        audience: AUTH0_AUDIENCE,
        currentDate: new Date(now * 1000),
        requiredClaims: [
          'exp',
          'iat',
          'sub',
          'jti',
          `${CLAIM_NS}tenant_id`,
          `${CLAIM_NS}tenant_class`,
        ],
      }))
    } catch {
      throw new AcceptanceError(
        'tenant_identity',
        `tenant ${tenant.label} token signature or registered claims are invalid`,
      )
    }
    if (
      payload[`${CLAIM_NS}tenant_id`] !== tenant.id
      || payload[`${CLAIM_NS}tenant_class`] !== TENANT_CLASS
    ) {
      throw new AcceptanceError(
        'tenant_classification',
        `tenant ${tenant.label} is not a signed non-customer acceptance tenant`,
      )
    }
    if (
      !Number.isInteger(payload.iat)
      || !Number.isInteger(payload.exp)
      || payload.iat > now + 60
      || payload.exp <= now
      || payload.exp - now > MAX_TOKEN_REMAINING_SECONDS
      || payload.exp - payload.iat > MAX_TOKEN_LIFETIME_SECONDS
    ) {
      throw new AcceptanceError(
        'tenant_identity',
        `tenant ${tenant.label} token lifetime is outside the production acceptance bound`,
      )
    }
    identities.push({ sub: payload.sub, jti: payload.jti })
  }
  if (
    identities[0].sub === identities[1].sub
    || identities[0].jti === identities[1].jti
  ) {
    throw new AcceptanceError(
      'tenant_identity',
      'the production acceptance tokens must have distinct subject and jti claims',
    )
  }
  return { classification: TENANT_CLASS, tenants: ['A', 'B'] }
}

export function parseProductionArgs(argv) {
  const args = { mode: null, receipt: null, state: null, verifyState: null }
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === '--execute') {
      if (args.mode) throw new AcceptanceError('configuration', 'choose one production mode')
      args.mode = 'execute'
    } else if (argv[index] === '--preflight') {
      if (args.mode) throw new AcceptanceError('configuration', 'choose one production mode')
      args.mode = 'preflight'
    }
    else if (argv[index] === '--receipt' && argv[index + 1]) args.receipt = argv[++index]
    else if (argv[index] === '--state' && argv[index + 1]) args.state = argv[++index]
    else if (argv[index] === '--verify-state' && argv[index + 1]) args.verifyState = argv[++index]
    else if (argv[index] === '--help') args.help = true
    else throw new AcceptanceError('configuration', `unknown argument: ${argv[index]}`)
  }
  return args
}

function buildPrivateState(config, browser) {
  return {
    schema: 'leaf.production-authored-cad-private-state.v1',
    run_id: config.runId,
    source_revision: config.expectedRevision,
    tenants: browser.map((result) => ({
      label: result.label,
      drawing_id: result._workbench_id,
      tool_name: result._staged_tool_name,
      change_set_id: result._staged_change_set_id,
      job_id: result._job_id,
      publication_confirmation_id: result._publication_confirmation_id,
      publication_catalog_digest: result._publication_catalog_digest,
      tool_content_sha256: result._tool_content_hash,
      tool_catalog_digest: result._tool_catalog_digest,
      drawing_content_sha256: result._drawing_content_hash,
    })),
  }
}

function writeReceipt(path, receipt) {
  const target = resolve(path)
  mkdirSync(dirname(target), { recursive: true })
  writeFileSync(target, `${JSON.stringify(receipt, null, 2)}\n`, {
    encoding: 'utf8',
    flag: 'wx',
    mode: 0o600,
  })
}

export async function main(argv = process.argv.slice(2), env = process.env) {
  try {
    const args = parseProductionArgs(argv)
    if (args.help) {
      console.log(
        'Usage: node web/scripts/deployed_production_authored_cad_acceptance.mjs ' +
        '(--preflight | --execute) --receipt <new-json-path> ' +
        '[--state <new-private-json-path> | --verify-state <private-json-path>]',
      )
      return 0
    }
    if (!args.receipt) {
      throw new AcceptanceError('configuration', '--receipt is required')
    }
    if (!args.mode) {
      throw new AcceptanceError('configuration', 'explicit --preflight or --execute is required')
    }
    if (args.state && args.mode !== 'execute') {
      throw new AcceptanceError('configuration', '--state is execute-only')
    }
    if (args.mode === 'execute' && !args.state) {
      throw new AcceptanceError('configuration', 'production execute requires --state')
    }
    if (args.verifyState && args.mode !== 'preflight') {
      throw new AcceptanceError('configuration', '--verify-state requires --preflight')
    }
    if (args.state && args.verifyState) {
      throw new AcceptanceError('configuration', 'choose --state or --verify-state')
    }
    const config = validateProductionConfig(env, args.mode)
    const classification = await verifyProductionTenantTokens(config)
    const startedAt = new Date().toISOString()
    const api = await runApiPreflight(config)
    if (args.verifyState) {
      api.persisted_acceptance_state = await provePersistedAcceptanceState(
        config,
        JSON.parse(readFileSync(resolve(args.verifyState), 'utf8')),
      )
    }
    const browser = config.execute ? await runBrowserAcceptance(config, true) : []
    if (config.execute) {
      api.executed_drawing_isolation = await proveExecutedDrawingIsolation(config, browser)
      api.executed_authority_isolation = await proveExecutedAuthorityIsolation(config, browser)
      api.pinned_write_rejections = await provePinnedWriteRejections(config, browser)
    }
    const stoppedAt = new Date().toISOString()
    const common = buildReceipt(
      config,
      {
        schema: 'leaf.deployment-identity.v1',
        environment: config.environment,
        source_revision: api.deployment_identity.source_revision,
        services: Object.fromEntries(
          Object.entries(api.deployment_identity.services).map(([name, image_digest]) => [
            name,
            { image_digest, source_revision: api.deployment_identity.source_revision },
          ]),
        ),
      },
      api,
      browser,
      startedAt,
      stoppedAt,
    )
    const receipt = {
      ...common,
      schema: config.execute
        ? 'leaf.deployed-production-authored-cad-acceptance.v1'
        : 'leaf.deployed-production-authored-cad-preflight.v1',
      tenant_classification: classification.classification,
      tenants: common.tenants.map(({ label, drawing_hash }) => (
        config.execute ? { label, drawing_hash } : { label }
      )),
      customer_data_accessed: false,
    }
    if (args.state) writeReceipt(args.state, buildPrivateState(config, browser))
    writeReceipt(args.receipt, receipt)
    console.log(JSON.stringify({
      ok: true,
      mode: receipt.mode,
      source_revision: receipt.source_revision,
      receipt: resolve(args.receipt),
    }))
    return 0
  } catch (error) {
    const safe = {
      ok: false,
      check: error instanceof AcceptanceError ? error.check : 'unexpected',
      error: error?.name || 'Error',
      message: error instanceof AcceptanceError ? error.message : 'acceptance driver failed',
    }
    console.error(JSON.stringify(safe))
    return 1
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  process.exitCode = await main()
}
