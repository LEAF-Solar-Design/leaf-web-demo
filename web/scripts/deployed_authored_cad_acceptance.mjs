#!/usr/bin/env node
/**
 * Non-mocked deployed acceptance driver for tenant-authored CAD.
 *
 * Default mode is a staging-only preflight. It proves one coherent release, two
 * real tenant identities, linked Claude grants, and a clean browser workbench
 * without request interception. Preflight opens the shared curated read-only
 * drawing under both authenticated tenants; its configured acceptance IDs are
 * plans for execute mode, not browser evidence. `--execute` uploads the tracked
 * DWG through the public account path, then adds authoring, approval, read-only
 * preview, cross-tenant authority checks, pinned-write rejection probes,
 * version, orbit, undo, and redo. No mode can target a production hostname.
 */

import { createHash, randomUUID } from 'node:crypto'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const SOURCE_SHA = /^[a-f0-9]{40}$/
const DIGEST = /^sha256:[a-f0-9]{64}$/
const SAFE_ID = /^[a-z0-9][a-z0-9_-]{0,62}$/
const RUN_ID = /^[a-z0-9][a-z0-9-]{5,49}$/
const JWT = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i
const REQUIRED_SERVICES = ['app', 'broker', 'canonical-worker', 'harness', 'web']
const PRODUCTION_HOSTS = new Set([
  'api.leafdesign.ai',
  'platform.leafdesign.ai',
  'leafautomation.ai',
  'www.leafautomation.ai',
  'platform.leafautomation.ai',
])
const DEFAULT_TIMEOUT_MS = 30_000
// 25 min, not 10: a REAL staging authoring turn exceeded 12 minutes and was
// still healthy (execute accept-eb3317b-1c13326d-005 died on this budget at
// 06:56Z while its stage job completed to `staged` minutes later — run
// 30791130113). Two tenants at this ceiling still fit the workflow's job cap.
const AUTHOR_TIMEOUT_MS = 25 * 60_000
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024
const EXTERNAL_EVIDENCE_REQUIRED = [
  'expired approval rejection (no staging clock-control seam)',
  'service restart persistence',
  'canonical worker lease ownership',
  'CloudWatch logs and metrics',
  'durable audit-row inspection',
]

export class AcceptanceError extends Error {
  constructor(check, message, details = undefined) {
    super(message)
    this.name = 'AcceptanceError'
    this.check = check
    this.details = details
  }
}

// Step tracking. The driver's only failure output is one JSON line, and run
// 30806698693 proved a bare {"error":"TimeoutError"} costs a full release
// train to localize: the workflow log could not say WHICH locator starved.
// Each leg marks itself as it starts; the failure line then carries the last
// step plus a scrubbed error detail.
let currentStep = 'configuration'
export function step(name, tenantLabel = undefined) {
  currentStep = tenantLabel ? `${name}_${tenantLabel}` : name
  console.log(JSON.stringify({
    ok: true,
    check: 'step',
    step: name,
    ...(tenantLabel ? { tenant: tenantLabel } : {}),
  }))
}

// Playwright error messages embed locator text, ARIA names, and page URLs —
// never request headers — but scrub anything token-shaped before it can reach
// a log line, and cap the length so a pathological message cannot flood the
// workflow log.
export function scrubbedDetail(error) {
  return String(error?.message || '')
    .replace(/[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g, '[token]')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 700)
}

// AcceptanceError.details is internal by default. Only these narrow,
// token-free job correlation fields may cross into the one-line workflow
// failure record. Do not serialize arbitrary backend response bodies here.
export function safeFailureDetails(error) {
  if (!(error instanceof AcceptanceError) || !error.details) return undefined
  const details = {}
  const jobId = String(error.details.job_id || '')
  const terminalStatus = String(error.details.terminal_status || '')
  const errorCode = String(error.details.error_code || '')
  if (UUID.test(jobId)) details.job_id = jobId
  if (/^(failed|http_error|timeout)$/.test(terminalStatus)) {
    details.terminal_status = terminalStatus
  }
  const allowedErrorCodes = new Set([
    'BAD_PARAMS',
    'ENTITLEMENT_REQUIRED',
    'INTERNAL',
    'already_decided',
    'quota_exceeded',
    'version_conflict',
  ])
  if (allowedErrorCodes.has(errorCode)) {
    details.error_code = errorCode
  }
  if (Number.isInteger(error.details.http_status)
      && error.details.http_status >= 100
      && error.details.http_status <= 599) {
    details.http_status = error.details.http_status
  }
  return Object.keys(details).length ? details : undefined
}

export function safeFailureRecord(error, stepName = currentStep) {
  const detail = scrubbedDetail(error)
  const details = safeFailureDetails(error)
  const authoredJobFailure = error instanceof AcceptanceError && error.check === 'authored_job'
  return {
    ok: false,
    check: error instanceof AcceptanceError ? error.check : 'unexpected',
    error: error?.name || 'Error',
    message: authoredJobFailure
      ? 'authored job did not complete successfully'
      : error instanceof AcceptanceError
      ? (detail || 'acceptance check failed')
      : 'acceptance driver failed',
    step: stepName,
    ...(!authoredJobFailure && detail ? { detail } : {}),
    ...(details ? { details } : {}),
  }
}

function required(env, name) {
  const value = String(env[name] || '').trim()
  if (!value) throw new AcceptanceError('configuration', `${name} is required`)
  return value
}

function exactUrl(value, name) {
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    throw new AcceptanceError('configuration', `${name} must be an absolute URL`)
  }
  if (parsed.protocol !== 'https:') {
    throw new AcceptanceError('configuration', `${name} must use HTTPS`)
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new AcceptanceError(
      'configuration',
      `${name} cannot contain credentials, query parameters, or a fragment`,
    )
  }
  if (parsed.pathname !== '' && parsed.pathname !== '/') {
    throw new AcceptanceError('configuration', `${name} must be an origin without a path`)
  }
  parsed.pathname = ''
  return parsed
}

function canonicalHostname(value) {
  const host = String(value || '').trim().toLowerCase()
  return host.endsWith('.') ? host.slice(0, -1) : host
}

function parseAllowedHosts(env) {
  const hosts = required(env, 'LEAF_ACCEPTANCE_ALLOWED_HOSTS')
    .split(',')
    .map(canonicalHostname)
    .filter(Boolean)
  if (hosts.length === 0 || new Set(hosts).size !== hosts.length) {
    throw new AcceptanceError(
      'configuration',
      'LEAF_ACCEPTANCE_ALLOWED_HOSTS must contain unique exact hostnames',
    )
  }
  if (hosts.some((host) => PRODUCTION_HOSTS.has(host))) {
    throw new AcceptanceError(
      'production_target',
      'the acceptance allowlist cannot contain a production hostname',
    )
  }
  return new Set(hosts)
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
  if (drawingId !== `acceptance-${runId}-${label.toLowerCase()}`) {
    throw new AcceptanceError(
      'configuration',
      `${prefix}DRAWING_ID must equal acceptance-${runId}-${label.toLowerCase()}`,
    )
  }
  if (!request.includes(runId) || request.length > 1000) {
    throw new AcceptanceError(
      'configuration',
      `${prefix}REQUEST must contain the run id and be at most 1000 characters`,
    )
  }
  return { label, id, jwt, plannedDrawingId: drawingId, drawingId, request }
}

export function validateConfig(env = process.env, execute = false) {
  if (required(env, 'LEAF_ACCEPTANCE_ENVIRONMENT') !== 'staging') {
    throw new AcceptanceError(
      'production_target',
      'LEAF_ACCEPTANCE_ENVIRONMENT must be exactly staging',
    )
  }
  const runId = required(env, 'LEAF_ACCEPTANCE_RUN_ID')
  if (!RUN_ID.test(runId)) {
    throw new AcceptanceError(
      'configuration',
      'LEAF_ACCEPTANCE_RUN_ID must be 6-50 lowercase letters, digits, or hyphens',
    )
  }
  const webUrl = exactUrl(required(env, 'LEAF_ACCEPTANCE_WEB_URL'), 'LEAF_ACCEPTANCE_WEB_URL')
  const apiUrl = exactUrl(required(env, 'LEAF_ACCEPTANCE_API_URL'), 'LEAF_ACCEPTANCE_API_URL')
  const allowedHosts = parseAllowedHosts(env)
  for (const [name, url] of [['web', webUrl], ['api', apiUrl]]) {
    const hostname = canonicalHostname(url.hostname)
    if (!allowedHosts.has(hostname)) {
      throw new AcceptanceError(
        'configuration',
        `${name} hostname is not in LEAF_ACCEPTANCE_ALLOWED_HOSTS`,
      )
    }
    if (PRODUCTION_HOSTS.has(hostname)) {
      throw new AcceptanceError('production_target', `${name} points at production`)
    }
  }
  const expectedRevision = required(env, 'LEAF_ACCEPTANCE_EXPECTED_REVISION').toLowerCase()
  if (!SOURCE_SHA.test(expectedRevision)) {
    throw new AcceptanceError(
      'configuration',
      'LEAF_ACCEPTANCE_EXPECTED_REVISION is not a source revision',
    )
  }
  const tenants = [
    tenantConfig(env, 'A', runId),
    tenantConfig(env, 'B', runId),
  ]
  if (tenants[0].id === tenants[1].id || tenants[0].jwt === tenants[1].jwt) {
    throw new AcceptanceError(
      'tenant_identity',
      'the two acceptance tenants and JWTs must be distinct',
    )
  }
  if (tenants[0].request === tenants[1].request) {
    throw new AcceptanceError(
      'tenant_identity',
      'the two acceptance requests must be distinct',
    )
  }
  const publicationApprovalSecret = execute
    ? required(env, 'LEAF_ACCEPTANCE_PUBLICATION_APPROVAL_SECRET')
    : null
  if (publicationApprovalSecret && publicationApprovalSecret.length < 16) {
    throw new AcceptanceError(
      'configuration',
      'LEAF_ACCEPTANCE_PUBLICATION_APPROVAL_SECRET must be at least 16 characters',
    )
  }
  return {
    environment: 'staging',
    execute,
    runId,
    webUrl: webUrl.toString().replace(/\/$/, ''),
    apiUrl: apiUrl.toString().replace(/\/$/, ''),
    expectedRevision,
    publicationApprovalSecret,
    tenants,
  }
}

export function evaluateDeploymentIdentity(identity, expectedRevision) {
  if (!identity || identity.schema !== 'leaf.deployment-identity.v1') {
    throw new AcceptanceError('deployment_identity', 'unsupported live deployment identity')
  }
  if (identity.environment !== 'staging') {
    throw new AcceptanceError('deployment_identity', 'live deployment identity is not for staging')
  }
  if (!SOURCE_SHA.test(String(identity.source_revision || ''))) {
    throw new AcceptanceError('deployment_identity', 'live deployment identity lacks a full source SHA')
  }
  if (identity.source_revision !== expectedRevision) {
    throw new AcceptanceError(
      'mixed_revision',
      'live deployment identity source revision does not match the expected revision',
    )
  }
  const names = Object.keys(identity.services || {}).sort()
  if (JSON.stringify(names) !== JSON.stringify(REQUIRED_SERVICES)) {
    throw new AcceptanceError(
      'deployment_identity',
      `live deployment identity services must be exactly ${REQUIRED_SERVICES.join(', ')}`,
    )
  }
  for (const name of REQUIRED_SERVICES) {
    const service = identity.services[name]
    if (!service || !DIGEST.test(String(service.image_digest || ''))) {
      throw new AcceptanceError('deployment_identity', `${name} is missing an immutable image digest`)
    }
    if (service.source_revision !== expectedRevision) {
      throw new AcceptanceError(
        'mixed_revision',
        `${name} live identity was not built from the expected source revision`,
      )
    }
  }
  return identity
}

function sha256(value) {
  return createHash('sha256').update(value).digest('hex')
}

async function parseJsonResponse(response, check) {
  const length = Number(response.headers.get('content-length') || 0)
  if (length > MAX_RESPONSE_BYTES) {
    throw new AcceptanceError(check, 'response exceeds the acceptance size limit')
  }
  const text = await response.text()
  if (Buffer.byteLength(text, 'utf8') > MAX_RESPONSE_BYTES) {
    throw new AcceptanceError(check, 'response exceeds the acceptance size limit')
  }
  try {
    return text ? JSON.parse(text) : {}
  } catch {
    throw new AcceptanceError(check, 'response is not JSON')
  }
}

export async function requestJson(
  config,
  tenant,
  path,
  {
    method = 'GET',
    body = undefined,
    extraHeaders = {},
    fetchImpl = fetch,
    timeoutMs = DEFAULT_TIMEOUT_MS,
  } = {},
) {
  const url = new URL(path, `${config.apiUrl}/`)
  if (url.origin !== new URL(config.apiUrl).origin) {
    throw new AcceptanceError('request_scope', 'acceptance request escaped the API origin')
  }
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetchImpl(url, {
      method,
      redirect: 'error',
      credentials: 'omit',
      signal: controller.signal,
      headers: {
        Accept: 'application/json',
        ...extraHeaders,
        Authorization: `Bearer ${tenant.jwt}`,
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    const parsed = await parseJsonResponse(response, path)
    return { status: response.status, body: parsed }
  } catch (error) {
    if (error instanceof AcceptanceError) throw error
    throw new AcceptanceError(path, `request failed: ${error?.name || 'Error'}`)
  } finally {
    clearTimeout(timer)
  }
}

const wait = (ms) => new Promise((done) => setTimeout(done, ms))

export async function provisionAcceptanceDrawing(
  config,
  tenant,
  sourceBytes,
  { fetchImpl = fetch, waitImpl = wait, pollMs = 1_000, maxPolls = 900 } = {},
) {
  const check = `live_dwg_${tenant.label}`
  if (!(sourceBytes instanceof Uint8Array) || sourceBytes.byteLength === 0) {
    throw new AcceptanceError(check, 'the tracked acceptance DWG is empty')
  }
  // Bounded retry on TRANSPORT failure only. The workflow is a single ~90-min
  // attempt, and a transient `fetch failed` on this one POST (observed once in
  // a local run) otherwise sinks the whole run before any browser work. A
  // non-2xx HTTP status is NOT retried here — it is inspected below as a real
  // receipt. Each attempt gets its own AbortController so a prior timeout
  // cannot abort a later try.
  const UPLOAD_ATTEMPTS = 3
  let upload
  let lastTransportError
  for (let attempt = 1; attempt <= UPLOAD_ATTEMPTS; attempt += 1) {
    const form = new FormData()
    form.append('file', new Blob([sourceBytes]), `acceptance-${config.runId}-${tenant.label}.dwg`)
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS)
    try {
      const response = await fetchImpl(new URL('/api/drawings/upload', `${config.apiUrl}/`), {
        method: 'POST',
        redirect: 'error',
        credentials: 'omit',
        signal: controller.signal,
        headers: {
          Accept: 'application/json',
          Authorization: `Bearer ${tenant.jwt}`,
        },
        body: form,
      })
      upload = { status: response.status, body: await parseJsonResponse(response, check) }
      lastTransportError = undefined
      break
    } catch (error) {
      if (error instanceof AcceptanceError) throw error
      lastTransportError = error
      if (attempt < UPLOAD_ATTEMPTS) {
        console.log(JSON.stringify({
          ok: true, check: 'upload_retry', tenant: tenant.label, attempt,
          error: error?.name || 'Error',
        }))
        await waitImpl(pollMs)
      }
    } finally {
      clearTimeout(timer)
    }
  }
  if (!upload) {
    throw new AcceptanceError(
      check,
      `DWG upload failed after ${UPLOAD_ATTEMPTS} attempts: ${lastTransportError?.name || 'Error'}`,
    )
  }
  if (upload.status !== 202
      || !UUID.test(upload.body?.drawing_id || '')
      || upload.body?.tenant_id !== tenant.id) {
    throw new AcceptanceError(check, `DWG upload returned an invalid HTTP ${upload.status} receipt`)
  }
  const drawingId = upload.body.drawing_id
  let status = upload.body
  for (let attempt = 0; status?.status !== 'ready' && attempt < maxPolls; attempt += 1) {
    if (status?.status === 'failed') {
      throw new AcceptanceError(check, status?.error?.message || 'DWG extraction failed')
    }
    await waitImpl(pollMs)
    const observed = await requestJson(
      config,
      tenant,
      `/api/drawings/${encodeURIComponent(drawingId)}/upload-status`,
      { fetchImpl },
    )
    if (observed.status !== 200) {
      throw new AcceptanceError(check, `DWG extraction status returned HTTP ${observed.status}`)
    }
    status = observed.body
  }
  if (status?.status !== 'ready') {
    throw new AcceptanceError(check, 'DWG extraction did not finish before the acceptance deadline')
  }
  return drawingId
}

export async function provisionAcceptanceDrawings(
  config,
  sourceBytes,
  { provisionImpl = provisionAcceptanceDrawing } = {},
) {
  const drawingIds = []
  for (const tenant of config.tenants) {
    drawingIds.push(await provisionImpl(config, tenant, sourceBytes))
  }
  return drawingIds
}

export async function waitForTerminalJob(
  config,
  tenant,
  jobId,
  { fetchImpl = fetch, waitImpl = wait, pollMs = 1_000, maxPolls = 900 } = {},
) {
  if (typeof jobId !== 'string' || !UUID.test(jobId)) {
    throw new AcceptanceError(
      'authored_job',
      'the authored write job id was invalid',
      { terminal_status: 'http_error' },
    )
  }
  for (let attempt = 0; attempt < maxPolls; attempt += 1) {
    let observed
    try {
      observed = await requestJson(
        config,
        tenant,
        `/api/jobs/${encodeURIComponent(jobId)}`,
        { fetchImpl },
      )
    } catch {
      throw new AcceptanceError(
        'authored_job',
        'the authored write job status could not be read',
        { job_id: jobId, terminal_status: 'http_error' },
      )
    }
    if (observed.status !== 200) {
      throw new AcceptanceError(
        'authored_job',
        `job status returned HTTP ${observed.status}`,
        { job_id: jobId, terminal_status: 'http_error', http_status: observed.status },
      )
    }
    if (observed.body?.status === 'failed') {
      const error = observed.body.error || {}
      throw new AcceptanceError(
        'authored_job',
        'the authored write job failed',
        {
          job_id: jobId,
          terminal_status: 'failed',
          ...(error.error_code ? { error_code: error.error_code } : {}),
        },
      )
    }
    if (observed.body?.status === 'complete') return observed.body
    await waitImpl(pollMs)
  }
  throw new AcceptanceError(
    'authored_job',
    'the authored write job did not finish before the acceptance deadline',
    { job_id: jobId, terminal_status: 'timeout' },
  )
}

// Like waitForTerminalJob, but RETURNS the terminal body for either outcome
// (complete or failed). The stale-head replay leg needs to inspect a job that
// is EXPECTED to fail, so it must not treat `failed` as a thrown error.
export async function waitForJobOutcome(
  config,
  tenant,
  jobId,
  { fetchImpl = fetch, waitImpl = wait, pollMs = 1_000, maxPolls = 900 } = {},
) {
  if (typeof jobId !== 'string' || !UUID.test(jobId)) {
    throw new AcceptanceError(
      'authored_job',
      'the authored replay job id was invalid',
      { terminal_status: 'http_error' },
    )
  }
  for (let attempt = 0; attempt < maxPolls; attempt += 1) {
    let observed
    try {
      observed = await requestJson(
        config,
        tenant,
        `/api/jobs/${encodeURIComponent(jobId)}`,
        { fetchImpl },
      )
    } catch {
      throw new AcceptanceError(
        'authored_job',
        'the authored replay job status could not be read',
        { job_id: jobId, terminal_status: 'http_error' },
      )
    }
    if (observed.status !== 200) {
      throw new AcceptanceError(
        'authored_job',
        `job status returned HTTP ${observed.status}`,
        { job_id: jobId, terminal_status: 'http_error', http_status: observed.status },
      )
    }
    if (observed.body?.status === 'failed' || observed.body?.status === 'complete') {
      return observed.body
    }
    await waitImpl(pollMs)
  }
  throw new AcceptanceError('authored_job', 'the replay job did not reach a terminal state before the acceptance deadline')
}

export function evaluateReadiness(body, expectedRevision) {
  if (
    body?.ok !== true ||
    body?.ready !== true ||
    body?.degraded_mode === true ||
    body?.status !== 'ready'
  ) {
    throw new AcceptanceError('readiness', 'the deployed application is not fully ready')
  }
  if (body.source_revision !== expectedRevision) {
    throw new AcceptanceError(
      'mixed_revision',
      'the live application revision does not match the image manifest',
    )
  }
  const dependencies = body.dependencies || {}
  const required = ['broker', 'harness', 'database', 'worker', 'durable_stores', 'build']
  for (const name of required) {
    if (dependencies[name]?.state !== 'ready') {
      throw new AcceptanceError('readiness', `${name} is not ready`)
    }
  }
}

function expectStatus(result, allowed, check) {
  if (!allowed.includes(result.status)) {
    throw new AcceptanceError(check, `unexpected HTTP ${result.status}`, {
      status: result.status,
      error_code: result.body?.error?.error_code || null,
    })
  }
}

export async function runApiPreflight(config, fetchImpl = fetch) {
  const [a, b] = config.tenants
  const deploymentIdentity = await requestJson(config, a, '/api/deployment-identity', { fetchImpl })
  expectStatus(deploymentIdentity, [200], 'deployment_identity')
  const identity = evaluateDeploymentIdentity(deploymentIdentity.body, config.expectedRevision)
  const readiness = await requestJson(config, a, '/api/ready', { fetchImpl })
  expectStatus(readiness, [200], 'readiness')
  evaluateReadiness(readiness.body, config.expectedRevision)

  const grants = []
  for (const [tenant, other] of [[a, b], [b, a]]) {
    const grant = await requestJson(config, tenant, '/api/tenant/claude-grant', { fetchImpl })
    expectStatus(grant, [200], `grant_${tenant.label}`)
    if (grant.body?.linked !== true || !['oauth', 'api_key'].includes(grant.body?.kind)) {
      throw new AcceptanceError(
        `grant_${tenant.label}`,
        `tenant ${tenant.label} does not have a linked Claude grant`,
      )
    }
    grants.push({ label: tenant.label, kind: grant.body.kind })

    const identity = await requestJson(
      config,
      tenant,
      '/api/session?dwg=rooftop_demo',
      {
        fetchImpl,
        extraHeaders: {
          'X-Tenant-Id': other.id,
          'X-Org-Id': other.id,
        },
      },
    )
    expectStatus(identity, [200], `identity_${tenant.label}`)
    if (identity.body?.tenant_id !== tenant.id || identity.body?.tenant_id === other.id) {
      throw new AcceptanceError(
        `identity_${tenant.label}`,
        'request headers overrode the JWT tenant identity',
      )
    }
  }
  return {
    deployment_identity: {
      source_revision: identity.source_revision,
      services: Object.fromEntries(
        REQUIRED_SERVICES.map((name) => [name, identity.services[name].image_digest]),
      ),
    },
    readiness: {
      source_revision: readiness.body.source_revision,
      dependencies: Object.fromEntries(
        Object.entries(readiness.body.dependencies).map(([name, value]) => [name, value.state]),
      ),
    },
    grants,
    tenant_header_override: 'denied',
  }
}

async function requestStagedPublicationApproval(
  config,
  tenant,
  changeSetId,
  fetchImpl,
  check,
) {
  if (!config.execute || !config.publicationApprovalSecret) {
    throw new AcceptanceError(
      check,
      'execute mode requires the independent publication approval credential',
    )
  }
  if (!UUID.test(changeSetId)) {
    throw new AcceptanceError(
      check,
      'the staged change set id is invalid',
    )
  }
  const url = new URL('/internal/customization/confirm', `${config.apiUrl}/`)
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS)
  try {
    const response = await fetchImpl(url, {
      method: 'POST',
      redirect: 'error',
      credentials: 'omit',
      signal: controller.signal,
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'X-Tenant-Id': tenant.id,
        'X-Approval-Secret': config.publicationApprovalSecret,
      },
      body: JSON.stringify({ change_set_id: changeSetId }),
    })
    const body = await parseJsonResponse(response, check)
    return { status: response.status, body }
  } catch (error) {
    if (error instanceof AcceptanceError) throw error
    throw new AcceptanceError(
      check,
      `approval authority request failed: ${error?.name || 'Error'}`,
    )
  } finally {
    clearTimeout(timer)
  }
}

export async function approveStagedPublication(
  config,
  tenant,
  changeSetId,
  fetchImpl = fetch,
) {
  const check = 'independent_publication_approval'
  const result = await requestStagedPublicationApproval(
    config, tenant, changeSetId, fetchImpl, check,
  )
  if (result.status !== 200 || typeof result.body.confirmation_id !== 'string') {
    throw new AcceptanceError(check, `approval authority returned HTTP ${result.status}`)
  }
  return {
    status: 'approved',
    confirmation_hash: sha256(result.body.confirmation_id),
    _confirmation_id: result.body.confirmation_id,
  }
}

export async function rejectCrossTenantStagedPublication(
  config,
  ownerTenant,
  otherTenant,
  changeSetId,
  fetchImpl = fetch,
) {
  const check = `cross_tenant_publication_approval_${ownerTenant.label}`
  const result = await requestStagedPublicationApproval(
    config, otherTenant, changeSetId, fetchImpl, check,
  )
  if (![403, 404].includes(result.status)) {
    throw new AcceptanceError(
      check,
      `wrong-tenant approval authority returned HTTP ${result.status}`,
      { status: result.status },
    )
  }
  return { status: 'denied' }
}

export async function approveIsolatedStagedPublication(
  config,
  ownerTenant,
  otherTenant,
  changeSetId,
  fetchImpl = fetch,
) {
  await rejectCrossTenantStagedPublication(
    config, ownerTenant, otherTenant, changeSetId, fetchImpl,
  )
  return approveStagedPublication(config, ownerTenant, changeSetId, fetchImpl)
}

export function validateStagedAuthorResponse(body, tenant) {
  const changeSetId = body?.receipt?.change_set_id
  const toolName = body?.tool?.name
  const capabilities = body?.tool?.capabilities
  const allowedCapabilities = new Set(['drawing.read', 'drawing.write'])
  if (
    !UUID.test(String(changeSetId || '')) ||
    body?.receipt?.state !== 'staged' ||
    typeof toolName !== 'string' ||
    !toolName ||
    !Array.isArray(capabilities) ||
    !capabilities.includes('drawing.write') ||
    !capabilities.every((capability) => allowedCapabilities.has(capability))
  ) {
    throw new AcceptanceError(
      'author_stage',
      `tenant ${tenant.label} did not stage one novel drawing.write tool`,
    )
  }
  return { changeSetId, toolName }
}

export function requireCameraMotion(before, after) {
  if (!before || !after || before === after) {
    throw new AcceptanceError('camera_motion', 'the browser gesture did not move the camera')
  }
}

export function requireDistinctStagedResults(results, tenants) {
  if (
    results.length !== 2 ||
    results[0]._staged_tool_name === results[1]._staged_tool_name ||
    results[0]._staged_change_set_id === results[1]._staged_change_set_id ||
    results.some((result, index) => result.staged_request_hash !== sha256(tenants[index].request))
  ) {
    throw new AcceptanceError(
      'author_stage',
      'the tenants did not receive distinct staged tools and change sets bound to their requests',
    )
  }
}

export function isMutatingApiRequest(requestUrl, method, allowedOrigins) {
  const url = new URL(requestUrl)
  return allowedOrigins.has(url.origin)
    && url.pathname.startsWith('/api/')
    && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)
}

export async function openTryAuthorSurface(page) {
  try {
    const authorTab = page.getByRole('tab', { name: 'Author', exact: true })
    await authorTab.waitFor({ state: 'visible', timeout: DEFAULT_TIMEOUT_MS })
    if (!await authorTab.isEnabled()) {
      throw new AcceptanceError(
        'author_surface',
        'the /try Author tab is disabled after backend readiness',
      )
    }
    await authorTab.click()
    const authorRequest = page.getByLabel('What should the tool do?')
    await authorRequest.waitFor({ state: 'visible', timeout: DEFAULT_TIMEOUT_MS })
    return authorRequest
  } catch (error) {
    if (error instanceof AcceptanceError) throw error
    throw new AcceptanceError(
      'author_surface',
      `could not open the /try Author tab: ${error?.name || 'Error'}`,
    )
  }
}

export async function openDrawingView(page) {
  try {
    const viewTab = page.getByRole('tab', { name: 'View', exact: true })
    await viewTab.waitFor({ state: 'visible', timeout: DEFAULT_TIMEOUT_MS })
    if (!await viewTab.isEnabled()) {
      throw new AcceptanceError(
        'browser_execute',
        'the drawing View tab is disabled after the authored write completed',
      )
    }
    await viewTab.click()
  } catch (error) {
    if (error instanceof AcceptanceError) throw error
    throw new AcceptanceError(
      'browser_execute',
      `could not open the drawing View tab: ${error?.name || 'Error'}`,
    )
  }
}

export async function takeEditingCheckout(page, apiUrl, drawingId) {
  try {
    const take = page.getByRole('button', { name: 'Take edit lock', exact: true })
    await take.waitFor({ state: 'visible', timeout: DEFAULT_TIMEOUT_MS })
    const checkoutUrl = `${apiUrl}/api/drawings/${encodeURIComponent(drawingId)}/checkout`
    const checkoutResponsePromise = page.waitForResponse(
      (response) => response.url() === checkoutUrl
        && response.request().method() === 'POST',
      { timeout: DEFAULT_TIMEOUT_MS },
    )
    await take.click()
    const checkoutResponse = await checkoutResponsePromise
    if (checkoutResponse.status() !== 200) {
      throw new AcceptanceError(
        'checkout',
        `taking the edit lock returned HTTP ${checkoutResponse.status()}`,
      )
    }
    await page.getByText('You hold the edit lock', { exact: true })
      .waitFor({ state: 'visible', timeout: DEFAULT_TIMEOUT_MS })
  } catch (error) {
    if (error instanceof AcceptanceError) throw error
    throw new AcceptanceError(
      'checkout',
      `could not take the acceptance drawing edit lock: ${error?.name || 'Error'}`,
    )
  }
}

async function runBrowserTenant(config, tenant, browser, execute) {
  const context = await browser.newContext({
    baseURL: config.webUrl,
    serviceWorkers: 'block',
  })
  const unexpected = []
  const mutatingApiRequests = []
  const allowedOrigins = new Set([
    new URL(config.webUrl).origin,
    new URL(config.apiUrl).origin,
  ])
  try {
    await context.addInitScript(({ token, drawingId }) => {
      window.localStorage.setItem('leaf.jwt', token)
      window.sessionStorage.setItem('leaf.cat.workbench.id.v1', drawingId)
    }, { token: tenant.jwt, drawingId: tenant.drawingId })
    const page = await context.newPage()
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (!allowedOrigins.has(url.origin)) {
        unexpected.push(`${request.method()} ${url.origin}${url.pathname}`)
      }
      if (isMutatingApiRequest(url, request.method(), allowedOrigins)) {
        mutatingApiRequests.push(`${request.method()} ${url.pathname}`)
      }
    })
    step('browser_open', tenant.label)
    await page.goto('/try', { waitUntil: 'domcontentloaded', timeout: 120_000 })
    const operatorPhase = page.getByTestId('operator-phase')
    try {
      await operatorPhase
        .filter({ hasText: 'Backend ready' })
        .waitFor({ state: 'visible', timeout: 120_000 })
    } catch {
      throw new AcceptanceError('browser_preflight', `tenant ${tenant.label} is not backend-ready`)
    }
    const command = page.getByRole('textbox', { name: 'Command bar' })
    if (await command.inputValue() !== '') {
      throw new AcceptanceError('preloaded_request', 'the command bar was not empty')
    }
    if (!await page.getByText('Live services').isVisible()) {
      throw new AcceptanceError('browser_preflight', 'the live-services marker is missing')
    }
    if (await page.getByText('Deterministic browser proof.').count()) {
      throw new AcceptanceError('mock_route', 'the deployed browser entered proof mode')
    }
    const workbenchText = await page.locator('.tc-bar-proj').innerText()
    if (workbenchText !== tenant.drawingId) {
      throw new AcceptanceError(
        'browser_preflight',
        'the browser did not use the exact acceptance drawing id',
      )
    }
    await page.reload({ waitUntil: 'domcontentloaded', timeout: 120_000 })
    try {
      await operatorPhase
        .filter({ hasText: 'Backend ready' })
        .waitFor({ state: 'visible', timeout: 120_000 })
    } catch {
      throw new AcceptanceError(
        'browser_preflight',
        `tenant ${tenant.label} is not backend-ready after reload`,
      )
    }
    if (await page.locator('.tc-bar-proj').innerText() !== workbenchText) {
      throw new AcceptanceError('browser_preflight', 'the workbench id changed across reload')
    }
    if (await command.inputValue() !== '') {
      throw new AcceptanceError('preloaded_request', 'reload populated the command bar')
    }
    if (unexpected.length) {
      throw new AcceptanceError(
        'request_scope',
        `the deployed browser contacted an unapproved origin: ${unexpected[0]}`,
      )
    }

    const result = {
      label: tenant.label,
      tenant_hash: sha256(tenant.id),
      workbench_hash: sha256(workbenchText),
      _workbench_id: workbenchText,
      blank_request: true,
      reload_stable: true,
      route_interceptions: 0,
      executed: false,
    }
    if (!execute) return result

    step('checkout', tenant.label)
    await takeEditingCheckout(page, config.apiUrl, tenant.drawingId)
    step('author_surface', tenant.label)
    const authorRequest = await openTryAuthorSurface(page)
    await authorRequest.fill(tenant.request)
    const stageResponsePromise = page.waitForResponse(
      (response) => response.url() === `${config.apiUrl}/api/author/stage`
        && response.request().method() === 'POST',
      { timeout: AUTHOR_TIMEOUT_MS },
    )
    await page.getByRole('button', { name: 'Generate tool', exact: true }).click()
    const stageResponse = await stageResponsePromise
    const stageStatus = stageResponse.status()
    if (stageStatus !== 200 && stageStatus !== 202) {
      throw new AcceptanceError('author_stage', `authoring returned HTTP ${stageStatus}`)
    }
    let stagedBody = await stageResponse.json()
    if (stageStatus === 202) {
      // leaf.customization-stage-job.v1 (#400): staging is now a DURABLE JOB —
      // POST answers 202 {change_set_id, poll_url} and the surface itself
      // polls GET /api/author/stages/{change_set_id} (web/src/api.js
      // pollAuthorStage) until a terminal status. #400 changed the server and
      // the surface but not this driver, so the first post-#400 execute died
      // here with "authoring returned HTTP 202". Wait for the surface's own
      // terminal poll response and use its staged result exactly as the sync
      // 200 body was used.
      const acceptedId = stagedBody?.change_set_id
      if (!acceptedId) {
        throw new AcceptanceError('author_stage', 'accepted authoring job carried no change_set_id')
      }
      // Progress marker: the driver was previously SILENT from launch to its
      // terminal line, which made the budget timeout undiagnosable from the
      // workflow log alone.
      console.log(JSON.stringify({ ok: true, check: 'author_stage_accepted', tenant: tenant.label, change_set_id: acceptedId }))
      const pollPath = `${config.apiUrl}/api/author/stages/${encodeURIComponent(acceptedId)}`
      const terminalStatuses = new Set(['staged', 'complete', 'completed', 'succeeded'])
      const failedStatuses = new Set(['failed', 'error'])
      // EXACT mirror of web/src/api.js authorStageResult (all four cases, in
      // order): the live server returns top-level `receipt` + `result: {tool}`
      // (customization_service.py:765), which must graft to
      // {...result, receipt} — returning the whole envelope loses `.tool` and
      // the validator would reject every successful async stage.
      const stagedFrom = (body) => {
        if (body?.result?.receipt) return body.result
        if (body?.receipt && body?.result && typeof body.result === 'object') {
          return { ...body.result, receipt: body.receipt }
        }
        if (body?.staged?.receipt) return body.staged
        if (body?.receipt) return body
        return null
      }
      const pollResponse = await page.waitForResponse(
        async (response) => {
          if (response.url() !== pollPath || response.request().method() !== 'GET') return false
          try {
            const body = await response.json()
            const status = String(body?.status || '').toLowerCase()
            if (failedStatuses.has(status)) return true // surfaced as a failure below
            return !!stagedFrom(body) && (terminalStatuses.has(status) || !status)
          } catch { return false }
        },
        { timeout: AUTHOR_TIMEOUT_MS },
      )
      const pollBody = await pollResponse.json()
      const pollStatus = String(pollBody?.status || '').toLowerCase()
      if (failedStatuses.has(pollStatus)) {
        throw new AcceptanceError('author_stage', `authoring job failed: ${pollBody?.message || pollStatus}`)
      }
      stagedBody = stagedFrom(pollBody)
      if (stagedBody && !stagedBody.receipt && pollBody?.receipt) {
        stagedBody = { ...stagedBody, receipt: pollBody.receipt }
      }
      if (!stagedBody) {
        throw new AcceptanceError('author_stage', 'authoring job reached terminal status without a staged result')
      }
      console.log(JSON.stringify({ ok: true, check: 'author_stage_staged', tenant: tenant.label, change_set_id: acceptedId }))
    }
    const staged = validateStagedAuthorResponse(stagedBody, tenant)
    const stageRequest = stageResponse.request().postDataJSON()
    if (stageRequest?.description !== tenant.request) {
      throw new AcceptanceError(
        'author_stage',
        `tenant ${tenant.label} staged a change set for a different request`,
      )
    }
    // AuthorPanel's staged copy is "Staged and ready to publish…" (the
    // "awaiting approval" wording the driver waited 25 minutes for no longer
    // exists anywhere in web/src). Match the staged state, not one sentence.
    step('staged_copy', tenant.label)
    await page.getByText(/Staged and ready to publish/, { exact: false })
      .waitFor({ state: 'visible', timeout: AUTHOR_TIMEOUT_MS })

    step('publication_approval', tenant.label)
    const otherTenant = config.tenants.find((candidate) => candidate.id !== tenant.id)
    const independentApproval = await approveIsolatedStagedPublication(
      config,
      tenant,
      otherTenant,
      staged.changeSetId,
    )
    // Publication moved to the request/approval endpoint (api.js:1158) and the
    // button reads "Request publication"; the old /api/author/register +
    // "Publish tool" pair no longer exists in the surface.
    step('request_publication', tenant.label)
    const publishResponsePromise = page.waitForResponse(
      (response) => response.url() === `${config.apiUrl}/api/author/publication-requests`
        && response.request().method() === 'POST',
      { timeout: AUTHOR_TIMEOUT_MS },
    )
    await page.getByRole('button', { name: 'Request publication', exact: true }).click()
    const publishResponse = await publishResponsePromise
    if (publishResponse.status() !== 200) {
      throw new AcceptanceError(
        'author_publish',
        `publication returned HTTP ${publishResponse.status()}`,
      )
    }
    step('run_published_tool', tenant.label)
    await page.getByRole('button', { name: 'Run it now', exact: true })
      .waitFor({ state: 'visible', timeout: AUTHOR_TIMEOUT_MS })
    await page.getByRole('button', { name: 'Run it now', exact: true }).click()

    const runButton = page.getByRole('button', {
      name: `Run ${staged.toolName}`,
      exact: true,
    })
    await runButton.waitFor({ state: 'visible', timeout: 30_000 })
    const runResponsePromise = page.waitForResponse(
      (response) => response.url() === `${config.apiUrl}/api/run`
        && response.request().method() === 'POST',
      { timeout: AUTHOR_TIMEOUT_MS },
    )
    await runButton.click()
    const runResponse = await runResponsePromise
    const acceptedRunRequest = runResponse.request()
    const runRequest = acceptedRunRequest.postDataJSON()
    const acceptedRunHeaders = acceptedRunRequest.headers()
    const runReplayHeaders = Object.fromEntries(
      ['idempotency-key', 'x-checkout-capability']
        .filter((name) => typeof acceptedRunHeaders[name] === 'string')
        .map((name) => [name, acceptedRunHeaders[name]]),
    )
    if (
      runResponse.status() !== 202 ||
      runRequest?.tool !== staged.toolName ||
      runRequest?.dwg !== tenant.drawingId ||
      runRequest?.dwg_version !== 1 ||
      runRequest?.params?.drawing_id !== tenant.drawingId
    ) {
      throw new AcceptanceError(
        'exact_write',
        'the exact staged tool was not submitted against acceptance drawing version 1',
      )
    }
    const runBody = await runResponse.json()
    const jobId = runBody?.job_id
    if (typeof jobId !== 'string' || !UUID.test(jobId)) {
      throw new AcceptanceError('exact_write', 'the accepted write did not return a job id')
    }
    step('authored_job', tenant.label)
    const job = await waitForTerminalJob(config, tenant, jobId)
    const newVersion = job?.result?.result?.new_version
    if (job?.result?.ok !== true
        || newVersion?.drawing_id !== tenant.drawingId
        || newVersion?.version !== 2) {
      throw new AcceptanceError('authored_job', 'the completed authored write did not create exact version 2')
    }
    step('version_head', tenant.label)
    await page.getByTestId('version-head').filter({ hasText: 'Version 2' })
      .waitFor({ state: 'visible', timeout: AUTHOR_TIMEOUT_MS })
    step('camera_proof', tenant.label)
    await openDrawingView(page)
    const camera = page.getByTestId('camera-controls')
    await camera.waitFor({ state: 'visible', timeout: 120_000 })
    await page.getByTestId('focus-3d').click()
    const cameraMount = page.locator(
      '.viewer-canvas[data-view-mode="panel-sculpture"][data-camera-position][data-camera-target]',
    )
    await cameraMount.waitFor({ state: 'visible', timeout: 30_000 })
    const canvas = cameraMount.locator('canvas')
    const beforeCameraPose = await cameraMount.evaluate((mount) =>
      `${mount.dataset.cameraPosition}|${mount.dataset.cameraTarget}`,
    )
    const box = await canvas.boundingBox()
    if (!box) throw new AcceptanceError('browser_execute', '3D canvas has no visible bounds')
    await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.5)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width * 0.7, box.y + box.height * 0.4, { steps: 8 })
    await page.mouse.up()
    await page.waitForFunction(
      ({ previous }) => {
        const mount = document.querySelector(
          '.viewer-canvas[data-view-mode="panel-sculpture"][data-camera-position][data-camera-target]',
        )
        return mount && `${mount.dataset.cameraPosition}|${mount.dataset.cameraTarget}` !== previous
      },
      { previous: beforeCameraPose },
      { timeout: 30_000 },
    )
    const afterCameraPose = await cameraMount.evaluate((mount) =>
      `${mount.dataset.cameraPosition}|${mount.dataset.cameraTarget}`,
    )
    requireCameraMotion(beforeCameraPose, afterCameraPose)
    // Focus 3D hid both rails and the command bar (`.tc-focus-hidden` is
    // `display: none !important`, landing.css:147), so every rail control is
    // invisible until focus view is left — the same testid now reads
    // "Show controls" and restores the rails. Run 30806698693 died here: the
    // orbit succeeded, then the Undo click starved against a hidden rail.
    step('focus_exit', tenant.label)
    await page.getByTestId('focus-3d').click()
    // Undo/Redo are Execution-tab chips (ToolCast.jsx:1233-1234); the View
    // tab this leg selected does not render them.
    const executionTab = page.getByRole('tab', { name: 'Execution', exact: true })
    await executionTab.waitFor({ state: 'visible', timeout: DEFAULT_TIMEOUT_MS })
    await executionTab.click()
    step('undo_redo', tenant.label)
    const undo = page.getByRole('button', { name: 'Undo', exact: true })
    await undo.click()
    // Undo is asynchronous: the Redo chip stays disabled while
    // drawing.versionBusy holds and flips enabled only once the undone head
    // lands (ToolCast.jsx:1234). A one-shot isEnabled() read raced that and
    // failed local run local-95aa027f-001. Prove the undo landed (head back
    // on Version 1), then click Redo — the click auto-waits for enablement,
    // so a never-enabled Redo still fails loudly.
    await page.getByTestId('version-head').filter({ hasText: 'Version 1' })
      .waitFor({ state: 'visible', timeout: 120_000 })
    const redo = page.getByRole('button', { name: 'Redo', exact: true })
    await redo.waitFor({ state: 'visible', timeout: DEFAULT_TIMEOUT_MS })
    await redo.click()
    await page.getByTestId('version-head').filter({ hasText: 'Version 2' })
      .waitFor({ state: 'visible', timeout: 120_000 })

    // /try's version history is a TAB in the Operations rail (a region), not
    // /app's modal dialog: this block previously targeted /app's VersionHistory
    // component ('History' button, dialog, vh-row-v1, 'Close version history'),
    // none of which this surface renders — a repo-wide string sweep hid it
    // because those literals do exist, on the other surface.
    // See web/src/site/ToolCast.jsx:1205 (tab) and :1322-1344 (panel + rows).
    step('version_preview', tenant.label)
    const previewMutationCount = mutatingApiRequests.length
    await page.getByRole('tab', { name: /^Versions/ }).click()
    const history = page.getByRole('region', { name: 'Version history' })
    await history.waitFor({ state: 'visible' })
    await history.getByTestId('try-version-v1').getByRole('button').first().click()
    await history.getByText(/Viewing v1 read-only/).waitFor({ state: 'visible' })
    // NOTE — a deliberate assertion CHANGE, not a silent drop. The old check
    // (`runButton.isDisabled()`) tested /app's contract: there, previewing a
    // version disables writes. On /try, `drawing.previewing` gates ONLY the
    // preview note and the active-row highlight (ToolCast.jsx:1328-1347);
    // `writeLocked` is `checkout.writeLocked || drawing.mutationsBlocked`
    // (:532) and preview contributes to neither. The locator was also stale by
    // this point (RoutePanel's transient confirm chip unmounts 180 ms after
    // dismissal). So the check could never have passed, and passing it would
    // have proven nothing about read-only-ness.
    // What IS proven below, and is what read-only actually means here: the
    // drawing head never moved, and the preview issued no mutating request.
    if (!await page.getByTestId('version-head').innerText().then((text) => text.includes('Version 2'))) {
      throw new AcceptanceError('read_only_preview', 'version preview changed the drawing head')
    }
    await history.getByRole('button', { name: 'Back to head', exact: true }).click()
    // No close control exists on this panel — leaving the tab selected is the
    // surface's own resting state; the mutation-count assertion below is what
    // actually proves the preview stayed read-only.
    if (mutatingApiRequests.length !== previewMutationCount) {
      throw new AcceptanceError('read_only_preview', 'version preview sent a mutating API request')
    }
    if (unexpected.length) {
      throw new AcceptanceError(
        'request_scope',
        `the deployed browser contacted an unapproved origin: ${unexpected[0]}`,
      )
    }
    return {
      ...result,
      authored_tool_hash: sha256(staged.toolName),
      staged_change_hash: sha256(staged.changeSetId),
      staged_request_hash: sha256(stageRequest.description),
      _staged_tool_name: staged.toolName,
      _staged_change_set_id: staged.changeSetId,
      _run_request: runRequest,
      _run_headers: runReplayHeaders,
      _job_id: jobId,
      _publication_confirmation_id: independentApproval._confirmation_id,
      cross_tenant_publication_approval: 'denied',
      independent_publication_approval: independentApproval.status,
      publication_confirmation_hash: independentApproval.confirmation_hash,
      exact_write_approval: true,
      read_only_preview: true,
      executed: true,
      version: 2,
      orbit: true,
      undo: true,
      redo: true,
    }
  } finally {
    await context.close()
  }
}

export function browserTenantForMode(tenant, execute) {
  return execute ? tenant : { ...tenant, drawingId: 'rooftop_demo' }
}

export async function runBrowserAcceptance(config, execute = false, chromiumImpl = undefined) {
  const chromium = chromiumImpl || (await import('@playwright/test')).chromium
  const browser = await chromium.launch({ headless: true })
  try {
    const results = []
    for (const tenant of config.tenants) {
      results.push(await runBrowserTenant(
        config, browserTenantForMode(tenant, execute), browser, execute,
      ))
    }
    if (execute && results[0].workbench_hash === results[1].workbench_hash) {
      throw new AcceptanceError(
        'browser_isolation',
        'the two tenant browser sessions received the same workbench',
      )
    }
    if (execute) requireDistinctStagedResults(results, config.tenants)
    return results
  } finally {
    await browser.close()
  }
}

export async function proveExecutedDrawingIsolation(config, browserResults, fetchImpl = fetch) {
  if (!config.execute || browserResults.some((result) => !result.executed)) {
    return { status: 'not_run_in_preflight' }
  }
  const [a, b] = config.tenants
  const own = []
  for (const tenant of config.tenants) {
    const result = await requestJson(
      config,
      tenant,
      `/api/drawings/${encodeURIComponent(tenant.drawingId)}/intake`,
      { fetchImpl },
    )
    expectStatus(result, [200], `executed_drawing_${tenant.label}`)
    if (!result.body?.intake || typeof result.body.intake !== 'object') {
      throw new AcceptanceError(
        `executed_drawing_${tenant.label}`,
        'the executed drawing response is missing intake data',
      )
    }
    own.push(sha256(JSON.stringify(result.body?.intake)))
  }
  if (own[0] === own[1]) {
    throw new AcceptanceError(
      'tenant_isolation',
      'the two distinct acceptance requests produced indistinguishable drawings',
    )
  }
  // The real isolation invariant is CONFIDENTIALITY: one tenant must never
  // receive another tenant's drawing. A 403/404 satisfies that, but so does the
  // /try surface's actual posture — `GET /api/drawings/{id}/intake` derives the
  // tenant from the JWT (server/routers/drawings.py:112, require_active_tenant,
  // so the forged X-Tenant-Id header is ignored) and `intake_view` MISSES the
  // caller's store for a drawing they do not own, falling back to the caller's
  // OWN seat with HTTP 200. Confirmed live at ee150b8: tenant A reading tenant
  // B's real drawing id returns A's own seat (echoed tenant_id = A, head 1),
  // byte-identical to A reading a random UUID and never equal to B's own
  // intake. A blanket 403/404 assertion mistook that non-disclosing fallback
  // for a leak. Assert the invariant that matters: a non-denied read must not
  // disclose the other tenant's drawing.
  const forged = (other) => ({
    fetchImpl,
    extraHeaders: { 'X-Tenant-Id': other.id, 'X-Org-Id': other.id },
  })
  let contained = false
  for (const [tenant, other] of [
    [a, b],
    [b, a],
  ]) {
    const cross = await requestJson(
      config,
      tenant,
      `/api/drawings/${encodeURIComponent(other.drawingId)}/intake`,
      forged(other),
    )
    if ([403, 404].includes(cross.status)) continue
    if (cross.status !== 200) {
      throw new AcceptanceError(
        `cross_tenant_drawing_${tenant.label}`,
        `cross-tenant drawing read returned HTTP ${cross.status}`,
        { status: cross.status },
      )
    }
    // SCOPE — what this leg can and cannot prove. The real isolation guarantee
    // is SERVER-SIDE: GET /api/drawings/{id}/intake derives the tenant from the
    // JWT (server/routers/drawings.py, require_active_tenant — the forged
    // X-Tenant-Id is ignored) and intake_view is tenant-scoped; a non-owned id
    // MISSES the caller's store and is bootstrapped per-tenant from the cached
    // demo (write_loop.ensure_demo_drawing — "any first-seen id"), so the caller
    // reaches only its OWN rows, never the other tenant's. A client cannot prove
    // that server-side scoping against an adversarial server that returns a
    // transformed copy of the other tenant's data identically for every probe —
    // no client-side equality check can, since the client has no independent
    // channel to the other tenant's private bytes. That adversarial-complete
    // confidentiality is DEFERRED, by the acceptance's own design, to the
    // receipt's external_evidence ("durable audit-row inspection"), exactly as
    // restart persistence and expired-approval are.
    //
    // What IS proven here, and is sufficient to catch an ACCIDENTAL isolation
    // regression (the realistic failure — a bug that serves the wrong tenant's
    // drawing, untransformed): (a) the echoed identity is the caller's, so a
    // forged X-Tenant-Id did not take; (b) the cross read equals a CLEAN
    // random-id control (caller JWT, no forged header), so the response depends
    // on neither the other tenant's specific drawing id nor the forged header;
    // and (c) the cross read is not byte-equal to EITHER tenant's actual private
    // acceptance drawing (own[0]/own[1]) — in particular not the other tenant's
    // real intake. Verified live at ee150b8: the forged cross read, a clean
    // random-id read, and the caller's own demo base all return the caller's own
    // bootstrapped demo seat, distinct from either tenant's head-2 drawing.
    const control = await requestJson(
      config,
      tenant,
      `/api/drawings/${randomUUID()}/intake`,
      { fetchImpl },
    )
    const crossHash = sha256(JSON.stringify(cross.body?.intake ?? null))
    const controlHash = sha256(JSON.stringify(control.body?.intake ?? null))
    if (
      control.status !== 200 ||
      cross.body?.tenant_id !== tenant.id ||
      control.body?.tenant_id !== tenant.id ||
      // Independent of the other tenant's specific id and of the forged header.
      crossHash !== controlHash ||
      // Not either tenant's actual private acceptance drawing — in particular
      // not the other tenant's real intake (the accidental-regression signature).
      crossHash === own[0] ||
      crossHash === own[1]
    ) {
      throw new AcceptanceError(
        `cross_tenant_drawing_${tenant.label}`,
        "a cross-tenant drawing read disclosed the other tenant's drawing",
        { status: cross.status },
      )
    }
    contained = true
  }
  return {
    status: contained ? 'no_client_observable_disclosure' : 'denied',
    distinct_result_hashes: true,
    adversarial_confidentiality: 'requires_external_audit',
  }
}

function requireDenied(result, check) {
  if (![403, 404].includes(result.status)) {
    throw new AcceptanceError(check, `cross-tenant authority read returned HTTP ${result.status}`, {
      status: result.status,
    })
  }
}

export async function proveExecutedAuthorityIsolation(config, browserResults, fetchImpl = fetch) {
  if (!config.execute || browserResults.some((result) => !result.executed)) {
    return { status: 'not_run_in_preflight' }
  }
  const [a, b] = config.tenants
  const checks = []
  for (const [tenant, other, ownResult, otherResult] of [
    [a, b, browserResults[0], browserResults[1]],
    [b, a, browserResults[1], browserResults[0]],
  ]) {
    const ownCatalog = await requestJson(config, tenant, '/api/tools', { fetchImpl })
    expectStatus(ownCatalog, [200], `own_catalog_${tenant.label}`)
    const ownNames = (ownCatalog.body?.tools || []).map((tool) => tool?.name)
    if (!ownNames.includes(ownResult._staged_tool_name)) {
      throw new AcceptanceError(`own_catalog_${tenant.label}`, 'the published tool is absent')
    }
    if (ownNames.includes(otherResult._staged_tool_name)) {
      throw new AcceptanceError(`cross_tenant_catalog_${tenant.label}`, 'another tenant tool leaked')
    }

    const staged = await requestJson(
      config,
      tenant,
      '/api/author/publication-requests',
      { method: 'POST', body: { change_set_id: otherResult._staged_change_set_id }, fetchImpl },
    )
    requireDenied(staged, `cross_tenant_repository_${tenant.label}`)

    const job = await requestJson(
      config,
      tenant,
      `/api/jobs/${encodeURIComponent(otherResult._job_id)}`,
      { fetchImpl },
    )
    requireDenied(job, `cross_tenant_job_${tenant.label}`)

    const ownAudit = await requestJson(config, tenant, '/api/agent/audit?limit=100', { fetchImpl })
    expectStatus(ownAudit, [200], `own_audit_${tenant.label}`)
    const forgedAudit = await requestJson(
      config,
      tenant,
      '/api/agent/audit?limit=100',
      { fetchImpl, extraHeaders: { 'X-Tenant-Id': other.id, 'X-Org-Id': other.id } },
    )
    expectStatus(forgedAudit, [200], `cross_tenant_audit_${tenant.label}`)
    if (JSON.stringify(forgedAudit.body) !== JSON.stringify(ownAudit.body)) {
      throw new AcceptanceError(
        `cross_tenant_audit_${tenant.label}`,
        'forged tenant headers changed the audit projection',
      )
    }
    checks.push(tenant.label)
  }
  return {
    status: 'denied',
    authorities: ['repository', 'catalog', 'job', 'audit'],
    tenants: checks,
  }
}

export async function provePinnedWriteRejections(config, browserResults, fetchImpl = fetch) {
  if (!config.execute || browserResults.some((result) => !result.executed)) {
    return { status: 'not_run_in_preflight' }
  }
  for (const [index, tenant] of config.tenants.entries()) {
    const original = browserResults[index]._run_request
    const replayHeaders = browserResults[index]._run_headers || {}
    if (!original || typeof original !== 'object') {
      throw new AcceptanceError('write_rejection', 'the accepted write request was not captured')
    }
    const before = await requestJson(
      config,
      tenant,
      `/api/drawings/${encodeURIComponent(tenant.drawingId)}/versions`,
      { fetchImpl },
    )
    expectStatus(before, [200], `write_rejection_head_${tenant.label}`)
    const head = before.body?.head
    if (head !== 2) {
      throw new AcceptanceError(`write_rejection_head_${tenant.label}`, 'expected drawing head 2')
    }
    // Signature the WHOLE version state, not just `head`. A stale write could
    // land as a new immutable version that bumps `latest` and grows `versions`
    // WITHOUT moving `head` (sol-critic PR #412 round 1); a head-only check
    // would miss that landing. Capture head + latest + the sorted version set
    // now and require every part unchanged after the rejected replays.
    const versionSignature = (body) => JSON.stringify({
      head: body?.head ?? null,
      latest: body?.latest ?? null,
      versions: (body?.versions || []).map((entry) => entry?.v).sort((x, y) => x - y),
    })
    const beforeSignature = versionSignature(before.body)

    // The exact approved write, replayed after the head moved to 2, must be
    // REJECTED without landing — but for a plain catalog run the rejection is
    // ASYNCHRONOUS. The synchronous head pin (server/routers/jobs.py:501,
    // "drawing head changed after approval") only fires when the request
    // carries `expected_drawing_head`, which the browser sends only on the
    // conversational exact-pin path (LEAF_EXACT_WRITE_PINS_REQUIRED=1 or a
    // backedge tenant — neither true for these M2M acceptance tenants). So the
    // submit returns 202 and the WORKER rejects the stale head, failing the job
    // ("stale drawing head: expected 1, current 2"). Confirmed live at ee150b8.
    // Accept either shape; the load-bearing proof is the unchanged head asserted
    // below. A 202 that COMPLETED would be a real stale-write landing and is
    // rejected here.
    const staleHeadReplay = await requestJson(
      config,
      tenant,
      '/api/run',
      { method: 'POST', body: original, extraHeaders: replayHeaders, fetchImpl },
    )
    const staleHeadPattern = /drawing head changed after approval|stale drawing head/i
    if (staleHeadReplay.status === 409) {
      if (!staleHeadPattern.test(staleHeadReplay.body?.error?.message || '')) {
        throw new AcceptanceError(
          `stale_head_replay_${tenant.label}`,
          'the synchronous rejection was not the stale drawing-head pin',
        )
      }
    } else if (staleHeadReplay.status === 202 && UUID.test(staleHeadReplay.body?.job_id || '')) {
      const outcome = await waitForJobOutcome(
        config, tenant, staleHeadReplay.body.job_id, { fetchImpl },
      )
      if (outcome.status !== 'failed' || !staleHeadPattern.test(outcome.error?.message || '')) {
        throw new AcceptanceError(
          `stale_head_replay_${tenant.label}`,
          'the accepted replay job did not fail on the stale drawing-head pin',
        )
      }
    } else {
      throw new AcceptanceError(
        `stale_head_replay_${tenant.label}`,
        `the exact replay returned unexpected HTTP ${staleHeadReplay.status}`,
        { status: staleHeadReplay.status },
      )
    }

    const staleCatalog = await requestJson(
      config,
      tenant,
      '/api/run',
      {
        method: 'POST',
        body: { ...original, catalog_digest: `sha256:${'0'.repeat(64)}` },
        extraHeaders: replayHeaders,
        fetchImpl,
      },
    )
    expectStatus(staleCatalog, [409], `stale_catalog_${tenant.label}`)
    if (!/catalog tool changed/i.test(staleCatalog.body?.error?.message || '')) {
      throw new AcceptanceError(
        `stale_catalog_${tenant.label}`,
        'the changed digest was not rejected by the catalog pin',
      )
    }

    const after = await requestJson(
      config,
      tenant,
      `/api/drawings/${encodeURIComponent(tenant.drawingId)}/versions`,
      { fetchImpl },
    )
    expectStatus(after, [200], `write_rejection_head_${tenant.label}`)
    if (versionSignature(after.body) !== beforeSignature) {
      throw new AcceptanceError(
        'write_rejection',
        'a rejected write changed the drawing version state (head, latest, or version set)',
      )
    }
  }
  return {
    status: 'denied_without_mutation',
    stale_head: true,
    stale_catalog: true,
    replayed_exact_request: true,
    expired_approval: 'requires_external_evidence',
  }
}

export function buildReceipt(
  config,
  deploymentIdentity,
  api,
  browser,
  startedAt,
  stoppedAt,
) {
  return {
    schema: 'leaf.deployed-authored-cad-acceptance.v1',
    environment: 'staging',
    mode: config.execute ? 'execute' : 'preflight',
    ok: true,
    run_id: config.runId,
    source_revision: config.expectedRevision,
    web_origin: new URL(config.webUrl).origin,
    api_origin: new URL(config.apiUrl).origin,
    deployment_identity_sha256: sha256(JSON.stringify(deploymentIdentity)),
    images: Object.fromEntries(
      REQUIRED_SERVICES.map((name) => [name, deploymentIdentity.services[name].image_digest]),
    ),
    tenants: config.tenants.map((tenant, index) => ({
      label: tenant.label,
      tenant_hash: sha256(tenant.id),
      drawing_hash: config.execute
        ? sha256(tenant.drawingId)
        : browser[index].workbench_hash,
      planned_drawing_hash: config.execute
        ? null
        : sha256(tenant.plannedDrawingId),
    })),
    api,
    browser: browser.map(({
      _workbench_id,
      _staged_tool_name,
      _staged_change_set_id,
      _run_request,
      _run_headers,
      _job_id,
      _publication_confirmation_id,
      ...safe
    }) => safe),
    external_evidence: {
      status: 'required',
      requirements: EXTERNAL_EVIDENCE_REQUIRED,
    },
    started_at: startedAt,
    stopped_at: stoppedAt,
    secrets_recorded: false,
    route_interceptions: 0,
  }
}

function parseArgs(argv) {
  const args = { execute: false, receipt: null }
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--execute') args.execute = true
    else if (argv[i] === '--receipt' && argv[i + 1]) args.receipt = argv[++i]
    else if (argv[i] === '--help') args.help = true
    else throw new AcceptanceError('configuration', `unknown argument: ${argv[i]}`)
  }
  return args
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
  let args
  try {
    args = parseArgs(argv)
    if (args.help) {
      console.log(
        'Usage: node web/scripts/deployed_authored_cad_acceptance.mjs ' +
        '[--execute] --receipt <new-json-path>',
      )
      return 0
    }
    if (!args.receipt) {
      throw new AcceptanceError('configuration', '--receipt is required')
    }
    const config = validateConfig(env, args.execute)
    const startedAt = new Date().toISOString()
    step('api_preflight')
    const api = await runApiPreflight(config)
    if (args.execute) {
      step('provision_drawings')
      const sourcePath = resolve(dirname(fileURLToPath(import.meta.url)), '../../data/rooftop_demo.dwg')
      const sourceBytes = readFileSync(sourcePath)
      const drawingIds = await provisionAcceptanceDrawings(config, sourceBytes)
      config.tenants = config.tenants.map((tenant, index) => ({
        ...tenant,
        drawingId: drawingIds[index],
      }))
      api.live_dwg_inputs = { status: 'ready', count: drawingIds.length }
    }
    const browser = await runBrowserAcceptance(config, args.execute)
    step('drawing_isolation')
    api.executed_drawing_isolation = await proveExecutedDrawingIsolation(config, browser)
    step('authority_isolation')
    api.executed_authority_isolation = await proveExecutedAuthorityIsolation(config, browser)
    step('pinned_write_rejections')
    api.pinned_write_rejections = await provePinnedWriteRejections(config, browser)
    const stoppedAt = new Date().toISOString()
    const receipt = buildReceipt(
      config,
      {
        schema: 'leaf.deployment-identity.v1',
        environment: 'staging',
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
    writeReceipt(args.receipt, receipt)
    console.log(JSON.stringify({
      ok: true,
      mode: receipt.mode,
      source_revision: receipt.source_revision,
      receipt: resolve(args.receipt),
    }))
    return 0
  } catch (error) {
    console.error(JSON.stringify(safeFailureRecord(error)))
    return 1
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  process.exitCode = await main()
}
