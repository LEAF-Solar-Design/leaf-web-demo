#!/usr/bin/env node
/**
 * Non-mocked deployed acceptance driver for tenant-authored CAD.
 *
 * Default mode is a staging-only preflight. It proves one coherent release, two
 * real tenant identities, linked Claude grants, and a clean browser workbench
 * without request interception. Loading the workbench may initialize version 1
 * of the two explicitly named acceptance drawings. `--execute` adds authoring,
 * approval, cross-tenant drawing checks, version, orbit, undo, and redo. No mode
 * can target a production hostname.
 */

import { createHash } from 'node:crypto'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const REVISION = /^(?:sha256:)?[a-f0-9]{7,64}$/
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
const AUTHOR_TIMEOUT_MS = 10 * 60_000
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024

export class AcceptanceError extends Error {
  constructor(check, message, details = undefined) {
    super(message)
    this.name = 'AcceptanceError'
    this.check = check
    this.details = details
  }
}

function required(env, name) {
  const value = String(env[name] || '').trim()
  if (!value) throw new AcceptanceError('configuration', `${name} is required`)
  return value
}

function canonicalHostname(value, name) {
  const hostname = String(value || '').trim().toLowerCase()
  const canonical = hostname.replace(/\.+$/, '')
  if (!canonical || canonical !== hostname) {
    const check = PRODUCTION_HOSTS.has(canonical)
      ? 'production_target'
      : 'configuration'
    throw new AcceptanceError(
      check,
      `${name} must be a canonical hostname without a trailing dot`,
    )
  }
  return canonical
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
  canonicalHostname(parsed.hostname, name)
  parsed.pathname = ''
  return parsed
}

function parseAllowedHosts(env) {
  const hosts = required(env, 'LEAF_ACCEPTANCE_ALLOWED_HOSTS')
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean)
    .map((value) => canonicalHostname(value, 'LEAF_ACCEPTANCE_ALLOWED_HOSTS'))
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
  return { label, id, jwt, drawingId, request }
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
    if (!allowedHosts.has(url.hostname.toLowerCase())) {
      throw new AcceptanceError(
        'configuration',
        `${name} hostname is not in LEAF_ACCEPTANCE_ALLOWED_HOSTS`,
      )
    }
    if (PRODUCTION_HOSTS.has(url.hostname.toLowerCase())) {
      throw new AcceptanceError('production_target', `${name} points at production`)
    }
  }
  const expectedRevision = required(env, 'LEAF_ACCEPTANCE_EXPECTED_REVISION').toLowerCase()
  if (!REVISION.test(expectedRevision)) {
    throw new AcceptanceError(
      'configuration',
      'LEAF_ACCEPTANCE_EXPECTED_REVISION is not a source revision',
    )
  }
  const manifestPath = resolve(required(env, 'LEAF_ACCEPTANCE_IMAGE_MANIFEST'))
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
    manifestPath,
    publicationApprovalSecret,
    tenants,
  }
}

export function validateDeploymentManifest(manifest, config) {
  if (!manifest || manifest.schema !== 'leaf.deployment-image-manifest.v1') {
    throw new AcceptanceError('image_manifest', 'unsupported deployment image manifest')
  }
  if (manifest.environment !== 'staging') {
    throw new AcceptanceError('image_manifest', 'image manifest is not for staging')
  }
  if (manifest.source_revision !== config.expectedRevision) {
    throw new AcceptanceError(
      'mixed_revision',
      'image manifest source revision does not match the expected revision',
    )
  }
  const names = Object.keys(manifest.services || {}).sort()
  if (JSON.stringify(names) !== JSON.stringify(REQUIRED_SERVICES)) {
    throw new AcceptanceError(
      'image_manifest',
      `image manifest services must be exactly ${REQUIRED_SERVICES.join(', ')}`,
    )
  }
  for (const name of REQUIRED_SERVICES) {
    const service = manifest.services[name]
    if (!service || !DIGEST.test(String(service.image_digest || ''))) {
      throw new AcceptanceError('image_manifest', `${name} is missing an immutable image digest`)
    }
    if (service.source_revision !== config.expectedRevision) {
      throw new AcceptanceError(
        'mixed_revision',
        `${name} was not built from the expected source revision`,
      )
    }
  }
  return manifest
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

export async function approveStagedPublication(
  config,
  tenant,
  changeSetId,
  fetchImpl = fetch,
) {
  if (!config.execute || !config.publicationApprovalSecret) {
    throw new AcceptanceError(
      'independent_publication_approval',
      'execute mode requires the independent publication approval credential',
    )
  }
  if (!UUID.test(changeSetId)) {
    throw new AcceptanceError(
      'independent_publication_approval',
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
    const body = await parseJsonResponse(response, 'independent_publication_approval')
    if (response.status !== 200 || typeof body.confirmation_id !== 'string') {
      throw new AcceptanceError(
        'independent_publication_approval',
        `approval authority returned HTTP ${response.status}`,
      )
    }
    return { status: 'approved', confirmation_hash: sha256(body.confirmation_id) }
  } catch (error) {
    if (error instanceof AcceptanceError) throw error
    throw new AcceptanceError(
      'independent_publication_approval',
      `approval authority request failed: ${error?.name || 'Error'}`,
    )
  } finally {
    clearTimeout(timer)
  }
}

function validateStagedAuthorResponse(body, tenant) {
  const changeSetId = body?.receipt?.change_set_id
  const toolName = body?.tool?.name
  const capabilities = body?.tool?.capabilities
  if (
    !UUID.test(String(changeSetId || '')) ||
    body?.receipt?.state !== 'staged' ||
    typeof toolName !== 'string' ||
    !toolName ||
    !Array.isArray(capabilities) ||
    !capabilities.includes('drawing.write')
  ) {
    throw new AcceptanceError(
      'author_stage',
      `tenant ${tenant.label} did not stage one novel drawing.write tool`,
    )
  }
  return { changeSetId, toolName }
}

async function runBrowserTenant(config, tenant, browser, execute) {
  const context = await browser.newContext({
    baseURL: config.webUrl,
    serviceWorkers: 'block',
  })
  const unexpected = []
  try {
    await context.addInitScript(({ token, drawingId }) => {
      window.localStorage.setItem('leaf.jwt', token)
      window.sessionStorage.setItem('leaf.cat.workbench.id.v1', drawingId)
    }, { token: tenant.jwt, drawingId: tenant.drawingId })
    const page = await context.newPage()
    page.on('request', (request) => {
      const url = new URL(request.url())
      const allowedOrigins = new Set([
        new URL(config.webUrl).origin,
        new URL(config.apiUrl).origin,
      ])
      if (!allowedOrigins.has(url.origin)) {
        unexpected.push(`${request.method()} ${url.origin}${url.pathname}`)
      }
    })
    await page.goto('/try', { waitUntil: 'networkidle', timeout: 120_000 })
    await page.getByTestId('operator-phase').waitFor({ state: 'visible' })
    const phase = await page.getByTestId('operator-phase').innerText()
    if (!phase.includes('Backend ready')) {
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
    await page.reload({ waitUntil: 'networkidle', timeout: 120_000 })
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

    const authorSection = page.locator('.author-section')
    if (await authorSection.getByRole('button', { name: /Author a tool/ }).getAttribute('aria-expanded') !== 'true') {
      await authorSection.getByRole('button', { name: /Author a tool/ }).click()
    }
    const authorRequest = page.getByLabel('What should the tool do?')
    await authorRequest.fill(tenant.request)
    const stageResponsePromise = page.waitForResponse(
      (response) => response.url() === `${config.apiUrl}/api/author/stage`
        && response.request().method() === 'POST',
      { timeout: AUTHOR_TIMEOUT_MS },
    )
    await page.getByRole('button', { name: 'Generate tool', exact: true }).click()
    const stageResponse = await stageResponsePromise
    if (stageResponse.status() !== 200) {
      throw new AcceptanceError('author_stage', `authoring returned HTTP ${stageResponse.status()}`)
    }
    const stagedBody = await stageResponse.json()
    const staged = validateStagedAuthorResponse(stagedBody, tenant)
    await page.getByText('Staged and awaiting approval.', { exact: false })
      .waitFor({ state: 'visible', timeout: AUTHOR_TIMEOUT_MS })

    const independentApproval = await approveStagedPublication(
      config,
      tenant,
      staged.changeSetId,
    )
    const publishResponsePromise = page.waitForResponse(
      (response) => response.url() === `${config.apiUrl}/api/author/register`
        && response.request().method() === 'POST',
      { timeout: AUTHOR_TIMEOUT_MS },
    )
    await page.getByRole('button', { name: 'Publish tool', exact: true }).click()
    const publishResponse = await publishResponsePromise
    if (publishResponse.status() !== 200) {
      throw new AcceptanceError(
        'author_publish',
        `publication returned HTTP ${publishResponse.status()}`,
      )
    }
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
    const runRequest = runResponse.request().postDataJSON()
    if (
      runResponse.status() !== 202 ||
      runRequest?.tool !== staged.toolName ||
      runRequest?.dwg !== tenant.drawingId ||
      runRequest?.dwg_version !== 1
    ) {
      throw new AcceptanceError(
        'exact_write',
        'the exact staged tool was not submitted against acceptance drawing version 1',
      )
    }
    await page.getByTestId('version-head').filter({ hasText: 'Version 2' })
      .waitFor({ state: 'visible', timeout: AUTHOR_TIMEOUT_MS })
    const camera = page.getByTestId('camera-controls')
    await camera.waitFor({ state: 'visible', timeout: 120_000 })
    const canvas = page.locator('canvas').first()
    const box = await canvas.boundingBox()
    if (!box) throw new AcceptanceError('browser_execute', '3D canvas has no visible bounds')
    await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.5)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width * 0.7, box.y + box.height * 0.4, { steps: 8 })
    await page.mouse.up()
    const undo = page.getByRole('button', { name: 'Undo', exact: true })
    await undo.click()
    const redo = page.getByRole('button', { name: 'Redo', exact: true })
    await redo.waitFor({ state: 'visible' })
    if (!await redo.isEnabled()) {
      throw new AcceptanceError('browser_execute', 'redo was not enabled after undo')
    }
    await redo.click()
    await page.getByTestId('version-head').filter({ hasText: 'Version 2' })
      .waitFor({ state: 'visible', timeout: 120_000 })
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
      independent_publication_approval: independentApproval.status,
      publication_confirmation_hash: independentApproval.confirmation_hash,
      exact_write_approval: true,
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

export async function runBrowserAcceptance(config, execute = false, chromiumImpl = undefined) {
  const chromium = chromiumImpl || (await import('@playwright/test')).chromium
  const browser = await chromium.launch({ headless: true })
  try {
    const results = []
    for (const tenant of config.tenants) {
      results.push(await runBrowserTenant(config, tenant, browser, execute))
    }
    if (results[0].workbench_hash === results[1].workbench_hash) {
      throw new AcceptanceError(
        'browser_isolation',
        'the two tenant browser sessions received the same workbench',
      )
    }
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
  for (const [tenant, other, forbiddenHash] of [
    [a, b, own[1]],
    [b, a, own[0]],
  ]) {
    const cross = await requestJson(
      config,
      tenant,
      `/api/drawings/${encodeURIComponent(other.drawingId)}/intake`,
      {
        fetchImpl,
        extraHeaders: {
          'X-Tenant-Id': other.id,
          'X-Org-Id': other.id,
        },
      },
    )
    if ([403, 404].includes(cross.status)) continue
    expectStatus(cross, [200], `cross_tenant_drawing_${tenant.label}`)
    if (!cross.body?.intake || typeof cross.body.intake !== 'object') {
      throw new AcceptanceError(
        `cross_tenant_drawing_${tenant.label}`,
        'the cross-tenant response is missing intake data',
      )
    }
    const observed = sha256(JSON.stringify(cross.body?.intake))
    if (observed === forbiddenHash) {
      throw new AcceptanceError(
        'tenant_isolation',
        `tenant ${tenant.label} read the other tenant's drawing bytes`,
      )
    }
  }
  return { status: 'denied', distinct_result_hashes: true }
}

export function buildReceipt(
  config,
  manifest,
  api,
  browser,
  startedAt,
  stoppedAt,
  manifestBytes = JSON.stringify(manifest),
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
    image_manifest_sha256: sha256(manifestBytes),
    images: Object.fromEntries(
      REQUIRED_SERVICES.map((name) => [name, manifest.services[name].image_digest]),
    ),
    tenants: config.tenants.map((tenant) => ({
      label: tenant.label,
      tenant_hash: sha256(tenant.id),
      drawing_hash: sha256(tenant.drawingId),
    })),
    api,
    browser: browser.map(({ _workbench_id, ...safe }) => safe),
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
    const manifestBytes = readFileSync(config.manifestPath, 'utf8')
    const manifest = validateDeploymentManifest(JSON.parse(manifestBytes), config)
    const startedAt = new Date().toISOString()
    const api = await runApiPreflight(config)
    const browser = await runBrowserAcceptance(config, args.execute)
    api.executed_drawing_isolation = await proveExecutedDrawingIsolation(config, browser)
    const stoppedAt = new Date().toISOString()
    const receipt = buildReceipt(
      config,
      manifest,
      api,
      browser,
      startedAt,
      stoppedAt,
      manifestBytes,
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
