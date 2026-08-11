#!/usr/bin/env node
/**
 * Read-only deployed surface certification for Leaf Platform staging and production.
 *
 * The driver uses two real Auth0 bearer principals, uploads a tracked DWG for
 * each principal, drives one approved count-by-layer conversation through the
 * protected API, and independently proves each isolated browser context can
 * authenticate to that API with its own bearer. It writes two artifacts:
 *
 *   * a public proof containing hashes only; and
 *   * a mode-0600 private correlation record for the workflow's database and
 *     broker-ledger verifier.
 *
 * The workflow wraps the public proof after it verifies the private correlation
 * against PostgreSQL. This file never prints or persists a bearer token.
 */

import { createHash, randomUUID } from 'node:crypto'
import {
  closeSync,
  mkdirSync,
  openSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import {
  AcceptanceError,
  provisionAcceptanceDrawing,
  requestJson,
} from './deployed_authored_cad_acceptance.mjs'

const SOURCE_SHA = /^[a-f0-9]{40}$/
const JWT = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i
const RUN_ID = /^[a-z0-9][a-z0-9-]{5,79}$/
const DIGEST = /^sha256:[a-f0-9]{64}$/
const REQUIRED_SERVICES = ['app', 'broker', 'canonical-worker', 'harness', 'web']
const VERIFIER_TENANT_LABELS = ['a', 'b']
const TARGETS = Object.freeze({
  staging: 'https://platform-staging.leafdesign.ai',
  prod: 'https://platform.leafdesign.ai',
})
const TERMINAL_STOPS = new Set([
  'end_turn', 'awaiting_approval', 'cap_hit', 'llm_quota_exhausted',
  'llm_rate_limited', 'error', 'timeout', 'interrupted',
])
const POLL_MS = 1_000
const MAX_POLLS = 1_800
const BROWSER_AUTH_PATH = '/api/deployment-identity'

function required(env, name) {
  const value = String(env[name] || '').trim()
  if (!value) throw new AcceptanceError('configuration', `${name} is required`)
  return value
}

function sha256(value) {
  return createHash('sha256').update(value).digest('hex')
}

function identifierHash(config, value) {
  return sha256(`${config.runId}\0${value}`)
}

function parseJwtClaims(jwt, name) {
  if (!JWT.test(jwt)) {
    throw new AcceptanceError('configuration', `${name} must be a compact JWT`)
  }
  try {
    const claims = JSON.parse(Buffer.from(jwt.split('.')[1], 'base64url').toString('utf8'))
    if (!claims || typeof claims !== 'object' || Array.isArray(claims)) throw new Error('claims')
    return claims
  } catch {
    throw new AcceptanceError('configuration', `${name} has an invalid JWT payload`)
  }
}

function tenantFromEnv(env, environment, label) {
  const prefix = environment === 'staging'
    ? `LEAF_ACCEPTANCE_TENANT_${label}_`
    : `LEAF_PRODUCTION_SYNTHETIC_TENANT_${label}_`
  const id = required(env, `${prefix}ID`)
  const jwt = required(env, `${prefix}JWT`)
  const claims = parseJwtClaims(jwt, `${prefix}JWT`)
  const subject = String(claims.sub || '')
  if (!subject.startsWith('auth0|')) {
    throw new AcceptanceError('tenant_identity', `${prefix}JWT is not an Auth0 account principal`)
  }
  return { label, id, jwt, subject }
}

export function validateSyntheticConfig(env = process.env, platform = process.platform) {
  if (platform === 'win32') {
    throw new AcceptanceError(
      'configuration',
      'surface synthetic execution requires a POSIX runner for mode-0600 artifacts',
    )
  }
  const environment = required(env, 'LEAF_SYNTHETIC_TARGET_ENV')
  if (!Object.hasOwn(TARGETS, environment)) {
    throw new AcceptanceError(
      'production_target',
      'LEAF_SYNTHETIC_TARGET_ENV must be exactly staging or prod',
    )
  }
  const targetUrl = required(env, 'LEAF_SYNTHETIC_TARGET_URL').replace(/\/$/, '')
  if (targetUrl !== TARGETS[environment]) {
    throw new AcceptanceError(
      'production_target',
      `LEAF_SYNTHETIC_TARGET_URL must be exactly ${TARGETS[environment]}`,
    )
  }
  const expectedRevision = required(env, 'LEAF_SYNTHETIC_EXPECTED_REVISION').toLowerCase()
  if (!SOURCE_SHA.test(expectedRevision)) {
    throw new AcceptanceError('configuration', 'expected revision must be a full lowercase SHA')
  }
  const runId = required(env, 'LEAF_SYNTHETIC_RUN_ID')
  if (!RUN_ID.test(runId)) {
    throw new AcceptanceError('configuration', 'synthetic run id is not safe')
  }
  const tenants = [tenantFromEnv(env, environment, 'A'), tenantFromEnv(env, environment, 'B')]
  if (
    tenants[0].id === tenants[1].id
    || tenants[0].jwt === tenants[1].jwt
    || tenants[0].subject === tenants[1].subject
  ) {
    throw new AcceptanceError(
      'tenant_identity',
      'the two Auth0 account principals, tenant ids, and JWTs must be distinct',
    )
  }
  return {
    environment,
    targetUrl,
    webUrl: targetUrl,
    apiUrl: targetUrl,
    expectedRevision,
    runId,
    tenants,
  }
}

function expectStatus(result, statuses, check) {
  if (!statuses.includes(result?.status)) {
    throw new AcceptanceError(check, `unexpected HTTP ${result?.status || 0}`)
  }
}

export function evaluateSurfaceIdentity(identity, config) {
  if (!identity || identity.schema !== 'leaf.deployment-identity.v1') {
    throw new AcceptanceError('deployment_identity', 'unsupported deployment identity')
  }
  const expectedEnvironment = config.environment === 'prod' ? 'production' : 'staging'
  if (![config.environment, expectedEnvironment].includes(identity.environment)) {
    throw new AcceptanceError('deployment_identity', 'deployment identity has the wrong environment')
  }
  if (identity.source_revision !== config.expectedRevision) {
    throw new AcceptanceError('deployment_identity', 'deployment identity has the wrong source revision')
  }
  if (JSON.stringify(Object.keys(identity.services || {}).sort())
      !== JSON.stringify([...REQUIRED_SERVICES].sort())) {
    throw new AcceptanceError('deployment_identity', 'deployment identity has the wrong service set')
  }
  for (const name of REQUIRED_SERVICES) {
    const service = identity.services[name]
    if (
      !service
      || !DIGEST.test(String(service.image_digest || ''))
      || service.source_revision !== config.expectedRevision
    ) {
      throw new AcceptanceError('deployment_identity', `${name} lacks exact immutable identity`)
    }
  }
  return identity
}

export function evaluateSurfaceReadiness(body, config) {
  if (
    body?.ok !== true
    || body?.ready !== true
    || body?.status !== 'ready'
    || body?.degraded_mode === true
    || body?.source_revision !== config.expectedRevision
  ) {
    throw new AcceptanceError('readiness', 'the deployed application is not exactly ready')
  }
  for (const name of ['broker', 'harness', 'database', 'worker', 'durable_stores', 'build']) {
    if (body.dependencies?.[name]?.state !== 'ready') {
      throw new AcceptanceError('readiness', `${name} is not ready`)
    }
  }
}

function canonicalJson(value) {
  if (Array.isArray(value)) return value.map(canonicalJson)
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, canonicalJson(value[key])]),
    )
  }
  return value
}

function stableIdentity(identity) {
  return JSON.stringify(canonicalJson(identity))
}

function stableReadiness(readiness) {
  if (!readiness || typeof readiness !== 'object' || Array.isArray(readiness)) {
    return JSON.stringify(canonicalJson(readiness))
  }
  const normalized = { ...readiness }
  delete normalized.duration_ms
  if (
    normalized.dependencies
    && typeof normalized.dependencies === 'object'
    && !Array.isArray(normalized.dependencies)
  ) {
    normalized.dependencies = Object.fromEntries(
      Object.entries(normalized.dependencies).map(([name, dependency]) => {
        if (!dependency || typeof dependency !== 'object' || Array.isArray(dependency)) {
          return [name, dependency]
        }
        const stableDependency = { ...dependency }
        delete stableDependency.latency_ms
        return [name, stableDependency]
      }),
    )
  }
  return JSON.stringify(canonicalJson(normalized))
}

async function loadPosture(config, tenant, requestImpl) {
  const identityResult = await requestImpl(config, tenant, '/api/deployment-identity')
  expectStatus(identityResult, [200], 'deployment_identity')
  const identity = evaluateSurfaceIdentity(identityResult.body, config)
  const readyResult = await requestImpl(config, tenant, '/api/ready')
  expectStatus(readyResult, [200], 'readiness')
  evaluateSurfaceReadiness(readyResult.body, config)
  return { identity, ready: readyResult.body }
}

async function loadAllPostures(config, requestImpl, phase) {
  const postures = []
  for (const tenant of config.tenants) {
    postures.push(await loadPosture(config, tenant, requestImpl))
  }
  const canonical = stableIdentity(postures[0].identity)
  if (postures.some((posture) => stableIdentity(posture.identity) !== canonical)) {
    throw new AcceptanceError(
      'deployment_identity',
      `${phase} deployment identity differs between Auth0 principals`,
    )
  }
  const canonicalReadiness = stableReadiness(postures[0].ready)
  if (postures.some((posture) => stableReadiness(posture.ready) !== canonicalReadiness)) {
    throw new AcceptanceError(
      'readiness',
      `${phase} readiness differs between Auth0 principals`,
    )
  }
  return {
    identity: postures[0].identity,
    ready: postures[0].ready,
  }
}

async function waitForTranscript(config, tenant, sessionId, predicate, dependencies = {}) {
  const requestImpl = dependencies.requestImpl || requestJson
  const waitImpl = dependencies.waitImpl || ((ms) => new Promise((done) => setTimeout(done, ms)))
  const maxPolls = dependencies.maxPolls || MAX_POLLS
  for (let attempt = 0; attempt < maxPolls; attempt += 1) {
    const result = await requestImpl(
      config,
      tenant,
      `/api/sessions/${encodeURIComponent(sessionId)}/transcript?limit=500`,
    )
    expectStatus(result, [200], `transcript_${tenant.label}`)
    const events = Array.isArray(result.body?.events) ? result.body.events : []
    if (predicate(events)) return events
    await waitImpl(POLL_MS)
  }
  throw new AcceptanceError(`transcript_${tenant.label}`, 'conversation did not reach its required state')
}

function proposalOf(events) {
  return [...events].reverse().find((event) =>
    event?.type === 'proposed_run' && event?.data?.tool === 'count-by-layer')
}

function eventsForTurn(events, turnId) {
  return events.filter((event) => event?.turn_id === turnId)
}

function terminalEvents(events, turnId) {
  return eventsForTurn(events, turnId).filter((event) => event?.type === 'turn_complete')
}

function terminalForTurn(events, turnId) {
  return terminalEvents(events, turnId).some((event) =>
    TERMINAL_STOPS.has(event?.data?.stop_reason))
}

export function evaluateInitialApprovalEvidence(events, firstTurnId, drawingId) {
  const turn = eventsForTurn(events, firstTurnId)
  const proposals = turn.filter((event) => event?.type === 'proposed_run')
  const terminals = turn.filter((event) => event?.type === 'turn_complete')
  if (proposals.length !== 1) {
    throw new AcceptanceError(
      'conversation_approval',
      'the initial turn must contain exactly one approval proposal',
    )
  }
  const proposal = proposals[0]
  if (
    proposal?.data?.tool !== 'count-by-layer'
    || proposal?.data?.dwg !== drawingId
    || typeof proposal?.data?.confirmation_id !== 'string'
    || proposal.data.confirmation_id.length < 8
  ) {
    throw new AcceptanceError(
      'conversation_approval',
      'the initial approval proposal has the wrong tool, drawing, or confirmation id',
    )
  }
  if (
    terminals.length !== 1
    || terminals[0]?.data?.stop_reason !== 'awaiting_approval'
    || turn.indexOf(proposal) >= turn.indexOf(terminals[0])
  ) {
    throw new AcceptanceError(
      'conversation_approval',
      'the initial turn must end exactly once awaiting approval',
    )
  }
  if (turn.some((event) =>
    ['error', 'confirmation_required', 'job_linked', 'tool_result'].includes(event?.type))) {
    throw new AcceptanceError(
      'conversation_approval',
      'the initial turn contains an error, execution, or conflicting approval event',
    )
  }
  return proposal
}

export function evaluateConversationEvidence({
  events,
  drawingId,
  confirmationId,
  firstTurnId,
  resumedTurnId,
  job,
}) {
  const proposal = evaluateInitialApprovalEvidence(events, firstTurnId, drawingId)
  if (proposal.data.confirmation_id !== confirmationId) {
    throw new AcceptanceError('conversation_approval', 'the expected approval proposal is missing')
  }
  const resumed = eventsForTurn(events, resumedTurnId)
  const relevant = resumed.filter((event) =>
    ['job_linked', 'tool_result', 'turn_complete'].includes(event?.type))
  if (
    relevant.length !== 3
    || relevant[0]?.type !== 'job_linked'
    || relevant[1]?.type !== 'tool_result'
    || relevant[2]?.type !== 'turn_complete'
  ) {
    throw new AcceptanceError(
      'conversation_events',
      'the resumed turn must contain one ordered job_linked, tool_result, turn_complete sequence',
    )
  }
  if (
    resumed.some((event) =>
      event?.type === 'error'
      || event?.type === 'proposed_run'
      || event?.type === 'confirmation_required')
    || relevant[2]?.data?.stop_reason !== 'end_turn'
  ) {
    throw new AcceptanceError(
      'conversation_events',
      'the resumed turn contains a failure, second approval, or non-success stop',
    )
  }
  const linked = relevant[0]
  if (!linked || !UUID.test(String(linked.data.job_id || ''))) {
    throw new AcceptanceError('conversation_events', 'the approved turn lacks a valid job_linked event')
  }
  if (linked.data.tool !== 'count-by-layer') {
    throw new AcceptanceError('conversation_events', 'job_linked names the wrong tool')
  }
  const toolResult = relevant[1]
  if (toolResult?.data?.tool !== 'run_capability' || toolResult?.data?.ok !== true) {
    throw new AcceptanceError(
      'conversation_events',
      'tool_result must be a successful run_capability result',
    )
  }
  if (
    job?.job_id !== linked.data.job_id
    || job?.tool !== 'count-by-layer'
    || job?.dwg !== drawingId
    || job?.dwg_version !== 1
    || job?.provenance?.execution_path !== 'cloud'
    || job?.status !== 'complete'
  ) {
    throw new AcceptanceError(
      'live_job_binding',
      'the terminal job is not the exact live uploaded-drawing version 1 run',
    )
  }
  return { jobId: linked.data.job_id, toolResult, proposal }
}

async function runConversation(config, tenant, drawingId, dependencies = {}) {
  const requestImpl = dependencies.requestImpl || requestJson
  const session = await requestImpl(config, tenant, '/api/sessions', {
    method: 'POST', body: { drawing_id: drawingId },
  })
  expectStatus(session, [200], `session_${tenant.label}`)
  const sessionId = session.body?.session_id
  if (!UUID.test(String(sessionId || ''))) {
    throw new AcceptanceError(`session_${tenant.label}`, 'session creation returned an invalid id')
  }
  const first = await requestImpl(
    config,
    tenant,
    `/api/sessions/${encodeURIComponent(sessionId)}/messages`,
    { method: 'POST', body: { text: 'RUN:count-by-layer' } },
  )
  expectStatus(first, [202], `conversation_${tenant.label}`)
  const firstTurnId = first.body?.turn_id
  if (!UUID.test(String(firstTurnId || ''))) {
    throw new AcceptanceError(`conversation_${tenant.label}`, 'first turn returned an invalid id')
  }
  const proposedEvents = await waitForTranscript(
    config,
    tenant,
    sessionId,
    (events) => {
      const terminal = terminalForTurn(events, firstTurnId)
      if (terminal) {
        evaluateInitialApprovalEvidence(events, firstTurnId, drawingId)
      }
      return Boolean(proposalOf(events) && terminal)
    },
    dependencies,
  )
  const proposal = evaluateInitialApprovalEvidence(proposedEvents, firstTurnId, drawingId)
  const confirmationId = proposal?.data?.confirmation_id
  if (typeof confirmationId !== 'string' || confirmationId.length < 8) {
    throw new AcceptanceError('conversation_approval', 'the proposed run lacks a confirmation id')
  }
  const decision = await requestImpl(
    config,
    tenant,
    `/api/agent/approvals/${encodeURIComponent(confirmationId)}`,
    { method: 'POST', body: { approved: true } },
  )
  expectStatus(decision, [200], `approval_${tenant.label}`)
  if (decision.body?.resolved !== true || decision.body?.approved !== true) {
    throw new AcceptanceError(`approval_${tenant.label}`, 'approval was not durably recorded')
  }
  const resumed = await requestImpl(
    config,
    tenant,
    `/api/sessions/${encodeURIComponent(sessionId)}/messages`,
    { method: 'POST', body: { confirm: { confirmationId, approved: true } } },
  )
  expectStatus(resumed, [202], `approval_resume_${tenant.label}`)
  const resumedTurnId = resumed.body?.turn_id
  if (!UUID.test(String(resumedTurnId || ''))) {
    throw new AcceptanceError(`approval_resume_${tenant.label}`, 'resumed turn returned an invalid id')
  }
  const completedEvents = await waitForTranscript(
    config,
    tenant,
    sessionId,
    (events) => {
      const terminal = terminalForTurn(events, resumedTurnId)
      const linked = events.some((event) =>
        event?.turn_id === resumedTurnId && event?.type === 'job_linked')
      if (terminal && !linked) {
        throw new AcceptanceError(
          'conversation_events',
          'the approved turn ended without a job_linked event',
        )
      }
      return terminal && linked
    },
    dependencies,
  )
  const linked = [...completedEvents].reverse().find((event) =>
    event?.turn_id === resumedTurnId && event?.type === 'job_linked')
  const jobId = linked?.data?.job_id
  if (!UUID.test(String(jobId || ''))) {
    throw new AcceptanceError('conversation_events', 'job_linked has an invalid id')
  }
  const jobResult = await requestImpl(config, tenant, `/api/jobs/${encodeURIComponent(jobId)}`)
  expectStatus(jobResult, [200], `job_${tenant.label}`)
  evaluateConversationEvidence({
    events: completedEvents,
    drawingId,
    confirmationId,
    firstTurnId,
    resumedTurnId,
    job: jobResult.body,
  })
  const replay = await requestImpl(
    config,
    tenant,
    `/api/sessions/${encodeURIComponent(sessionId)}/messages`,
    { method: 'POST', body: { confirm: { confirmationId, approved: true } } },
  )
  expectStatus(replay, [409], `approval_replay_${tenant.label}`)
  if (!/already consumed/i.test(String(replay.body?.error?.message || ''))) {
    throw new AcceptanceError(`approval_replay_${tenant.label}`, 'consumed approval replay was not refused')
  }
  return {
    sessionId,
    firstTurnId,
    resumedTurnId,
    confirmationId,
    jobId,
    job: jobResult.body,
  }
}

function comparisonBody(body, identifiers = []) {
  let encoded = JSON.stringify(body ?? null)
  for (const identifier of identifiers) {
    encoded = encoded.split(identifier).join('[queried-id]')
  }
  return encoded
}

export function requireUnknownEquivalent(foreign, control, check, identifiers = []) {
  if (foreign?.status !== control?.status) {
    throw new AcceptanceError(check, 'foreign and unknown resources had different status')
  }
  if (![403, 404].includes(foreign?.status)) {
    throw new AcceptanceError(check, `foreign resource returned HTTP ${foreign?.status || 0}`)
  }
  if (
    comparisonBody(foreign.body, identifiers)
    !== comparisonBody(control.body, identifiers)
  ) {
    throw new AcceptanceError(check, 'foreign and unknown resources had distinguishable bodies')
  }
}

async function proveNonEnumeration(config, results, dependencies = {}) {
  const requestImpl = dependencies.requestImpl || requestJson
  for (const [index, tenant] of config.tenants.entries()) {
    const other = results[index === 0 ? 1 : 0]
    const unknown = {
      drawing: randomUUID(),
      job: randomUUID(),
      session: randomUUID(),
    }
    for (const [kind, foreignId, controlId, foreignPath, controlPath] of [
      [
        'drawing',
        other.drawingId,
        unknown.drawing,
        `/api/drawings/${encodeURIComponent(other.drawingId)}/intake`,
        `/api/drawings/${unknown.drawing}/intake`,
      ],
      [
        'job',
        other.jobId,
        unknown.job,
        `/api/jobs/${encodeURIComponent(other.jobId)}`,
        `/api/jobs/${unknown.job}`,
      ],
      [
        'transcript',
        other.sessionId,
        unknown.session,
        `/api/sessions/${encodeURIComponent(other.sessionId)}/transcript?limit=500`,
        `/api/sessions/${unknown.session}/transcript?limit=500`,
      ],
    ]) {
      const foreign = await requestImpl(config, tenant, foreignPath)
      const control = await requestImpl(config, tenant, controlPath)
      requireUnknownEquivalent(
        foreign,
        control,
        `non_enumeration_${tenant.label}_${kind}`,
        [foreignId, controlId],
      )
    }
  }
}

export async function runRealBrowser(config, results, chromiumImpl = undefined) {
  const chromium = chromiumImpl || (await import('@playwright/test')).chromium
  const browser = await chromium.launch({ headless: true })
  const observations = []
  try {
    for (const [index, tenant] of config.tenants.entries()) {
      const context = await browser.newContext({
        baseURL: config.webUrl,
        serviceWorkers: 'block',
      })
      const unexpectedOrigins = []
      try {
        await context.addInitScript(({ token, drawingId }) => {
          window.localStorage.setItem('leaf.jwt', token)
          window.sessionStorage.setItem('leaf.cat.workbench.id.v1', drawingId)
        }, { token: tenant.jwt, drawingId: results[index].drawingId })
        const page = await context.newPage()
        page.on('request', (request) => {
          const url = new URL(request.url())
          if (url.origin !== config.targetUrl) {
            unexpectedOrigins.push(url.origin)
          }
        })
        await page.goto('/try', { waitUntil: 'domcontentloaded', timeout: 120_000 })
        await page.getByTestId('operator-phase')
          .filter({ hasText: 'Backend ready' })
          .waitFor({ state: 'visible', timeout: 120_000 })
        const protectedRequest = await page.evaluate(async ({ path }) => {
          const token = window.localStorage.getItem('leaf.jwt')
          if (!token) {
            return { status: 0, ok: false, response_url: null, bearer_sha256: null }
          }
          const digest = await globalThis.crypto.subtle.digest(
            'SHA-256',
            new TextEncoder().encode(token),
          )
          const bearerSha256 = [...new Uint8Array(digest)]
            .map((byte) => byte.toString(16).padStart(2, '0'))
            .join('')
          const response = await window.fetch(path, {
            method: 'GET',
            headers: { authorization: `Bearer ${token}` },
            credentials: 'omit',
            cache: 'no-store',
            redirect: 'error',
          })
          return {
            status: response.status,
            ok: response.ok,
            response_url: response.url,
            bearer_sha256: bearerSha256,
          }
        }, { path: BROWSER_AUTH_PATH })
        const protectedUrl = protectedRequest.response_url
          ? new URL(protectedRequest.response_url)
          : null
        if (
          protectedRequest.status !== 200
          || protectedRequest.ok !== true
          || protectedRequest.bearer_sha256 !== sha256(tenant.jwt)
          || protectedUrl?.origin !== config.targetUrl
          || protectedUrl?.pathname !== BROWSER_AUTH_PATH
          || protectedUrl?.search
          || protectedUrl?.hash
        ) {
          throw new AcceptanceError(
            'browser_auth',
            'browser did not authenticate the exact protected same-origin API with its assigned bearer',
          )
        }
        if (await page.getByText('Deterministic browser proof.').count()) {
          throw new AcceptanceError('mock_route', 'browser entered deterministic proof mode')
        }
        const drawing = await page.locator('.tc-bar-proj').innerText()
        if (drawing !== results[index].drawingId) {
          throw new AcceptanceError('browser_drawing', 'browser opened the wrong uploaded drawing')
        }
        if (unexpectedOrigins.length) {
          throw new AcceptanceError('request_scope', 'browser contacted an unexpected origin')
        }
        const finalUrl = new URL(page.url())
        if (
          finalUrl.origin !== config.targetUrl
          || finalUrl.pathname !== '/try'
          || finalUrl.search
          || finalUrl.hash
        ) {
          throw new AcceptanceError(
            'browser_navigation',
            'browser did not remain on the exact target /try surface',
          )
        }
        observations.push({
          label: VERIFIER_TENANT_LABELS[index],
          drawing_hash: identifierHash(config, drawing),
          backend_ready: true,
          live_services: await page.getByText('Live services').isVisible(),
          protected_api_authenticated: true,
          browser_bearer_hash: identifierHash(config, tenant.jwt),
          route_interceptions: 0,
        })
      } finally {
        await context.close()
      }
    }
  } finally {
    await browser.close()
  }
  return observations
}

const ORDERED_ASSERTIONS = [
  'target_origin_exact',
  'deployment_identity_exact',
  'runtime_identity_exact',
  'readiness_green',
  'two_distinct_auth0_accounts',
  'two_uploads_ready',
  'real_browser_zero_interception',
  'conversation_terminal',
  'tool_result_terminal',
  'aps_live_ledger_durable',
  'tenant_a_cannot_enumerate_b',
  'tenant_b_cannot_enumerate_a',
  'confirmation_replay_refused',
  'deployment_identity_unchanged',
  'recovery_receipt_linked',
]

export function buildPublicProof(config, before, after, results, browser, timestamps) {
  const assertions = ORDERED_ASSERTIONS.map((name) => ({
    name,
    status: ['aps_live_ledger_durable', 'recovery_receipt_linked'].includes(name)
      ? 'requires_workflow_verification'
      : 'passed',
  }))
  return {
    schema: 'leaf.surface-reconciliation-proof-input.v1',
    environment: config.environment,
    target_origin: config.targetUrl,
    source_revision: config.expectedRevision,
    run_id_hash: identifierHash(config, config.runId),
    deployment_identity_before_sha256: sha256(stableIdentity(before.identity)),
    deployment_identity_after_sha256: sha256(stableIdentity(after.identity)),
    runtime_images: Object.fromEntries(REQUIRED_SERVICES.map((name) => [
      name,
      before.identity.services[name].image_digest,
    ])),
    resource_hashes: {
      tenant_ids: config.tenants.map((tenant) => identifierHash(config, tenant.id)),
      subjects: config.tenants.map((tenant) => identifierHash(config, tenant.subject)),
      drawing_ids: results.map((result) => identifierHash(config, result.drawingId)),
      session_ids: results.map((result) => identifierHash(config, result.sessionId)),
      turn_ids: results.map((result) => [
        identifierHash(config, result.firstTurnId),
        identifierHash(config, result.resumedTurnId),
      ]),
      confirmation_ids: results.map((result) =>
        identifierHash(config, result.confirmationId)),
      job_ids: results.map((result) => identifierHash(config, result.jobId)),
    },
    tenants: results.map((result, index) => ({
      label: VERIFIER_TENANT_LABELS[index],
      tenant_hash: identifierHash(config, config.tenants[index].id),
      subject_hash: identifierHash(config, config.tenants[index].subject),
      drawing_hash: identifierHash(config, result.drawingId),
      session_hash: identifierHash(config, result.sessionId),
      job_hash: identifierHash(config, result.jobId),
      confirmation_hash: identifierHash(config, result.confirmationId),
      drawing_version: 1,
    })),
    // Keep the retained browser schema stable and hash-only. backend_ready is
    // true only after the UI readiness check and the assigned-bearer protected
    // API proof above both pass.
    browser: browser.map((entry, index) => ({
      label: VERIFIER_TENANT_LABELS[index],
      drawing_hash: entry.drawing_hash,
      backend_ready: entry.backend_ready,
      live_services: entry.live_services,
      route_interceptions: entry.route_interceptions,
    })),
    assertions,
    started_at: timestamps.startedAt,
    stopped_at: timestamps.stoppedAt,
    identifiers_recorded: 'hashed_only',
    secrets_recorded: false,
  }
}

export function buildPrivateCorrelation(config, before, after, results, timestamps) {
  return {
    schema: 'leaf.surface-reconciliation-private-correlation.v1',
    environment: config.environment,
    target_origin: config.targetUrl,
    source_revision: config.expectedRevision,
    run_id: config.runId,
    deployment_identity_before: before.identity,
    deployment_identity_after: after.identity,
    tenants: results.map((result, index) => ({
      label: VERIFIER_TENANT_LABELS[index],
      tenant_id: config.tenants[index].id,
      subject: config.tenants[index].subject,
      drawing_id: result.drawingId,
      drawing_version: 1,
      session_id: result.sessionId,
      turn_ids: [result.firstTurnId, result.resumedTurnId],
      confirmation_id: result.confirmationId,
      job_id: result.jobId,
    })),
    started_at: timestamps.startedAt,
    stopped_at: timestamps.stoppedAt,
    secrets_recorded: false,
  }
}

export function assertVerifierTenantLabelContract(proof, correlation) {
  for (const [entries, name] of [
    [proof?.tenants, 'public proof tenants'],
    [proof?.browser, 'public proof browser'],
    [correlation?.tenants, 'private correlation tenants'],
  ]) {
    const labels = entries?.map((tenant) => tenant?.label)
    if (JSON.stringify(labels) !== JSON.stringify(VERIFIER_TENANT_LABELS)) {
      throw new AcceptanceError(
        'artifact_contract',
        `${name} tenant labels must be exactly lowercase a, b`,
      )
    }
  }
}

export function assertPublicProofIsCredentialFree(proof, config, results) {
  const encoded = JSON.stringify(proof)
  const forbidden = [
    ...config.tenants.flatMap((tenant) => [tenant.jwt, tenant.id, tenant.subject]),
    ...results.flatMap((result) => [
      result.drawingId,
      result.sessionId,
      result.firstTurnId,
      result.resumedTurnId,
      result.confirmationId,
      result.jobId,
    ]),
  ]
  if (forbidden.some((value) => value && encoded.includes(value))) {
    throw new AcceptanceError('public_proof', 'public proof contains a raw identifier or secret')
  }
}

export function writePrivateJson(path, value) {
  const target = resolve(path)
  mkdirSync(dirname(target), { recursive: true })
  const fd = openSync(target, 'wx', 0o600)
  try {
    writeFileSync(fd, `${JSON.stringify(value, null, 2)}\n`, { encoding: 'utf8' })
  } finally {
    closeSync(fd)
  }
}

export function writeArtifactPair(proofPath, proof, correlationPath, correlation) {
  const proofTarget = resolve(proofPath)
  const correlationTarget = resolve(correlationPath)
  mkdirSync(dirname(proofTarget), { recursive: true })
  mkdirSync(dirname(correlationTarget), { recursive: true })
  let proofFd
  let correlationFd
  let complete = false
  try {
    // Reserve both names before either artifact receives content. This avoids a
    // half-published public/private pair when one requested path already exists.
    proofFd = openSync(proofTarget, 'wx', 0o600)
    correlationFd = openSync(correlationTarget, 'wx', 0o600)
    writeFileSync(proofFd, `${JSON.stringify(proof, null, 2)}\n`, { encoding: 'utf8' })
    writeFileSync(correlationFd, `${JSON.stringify(correlation, null, 2)}\n`, {
      encoding: 'utf8',
    })
    complete = true
  } finally {
    if (proofFd !== undefined) closeSync(proofFd)
    if (correlationFd !== undefined) closeSync(correlationFd)
    if (!complete) {
      if (proofFd !== undefined) unlinkSync(proofTarget)
      if (correlationFd !== undefined) unlinkSync(correlationTarget)
    }
  }
}

function parseArgs(argv) {
  const args = { proof: null, correlation: null }
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === '--proof' && argv[index + 1]) args.proof = argv[++index]
    else if (argv[index] === '--correlation' && argv[index + 1]) args.correlation = argv[++index]
    else if (argv[index] === '--help') args.help = true
    else throw new AcceptanceError('configuration', `unknown argument: ${argv[index]}`)
  }
  return args
}

export function safeSyntheticFailure(error) {
  const scrub = (value) => String(value || '')
    .replace(/[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g, '[token]')
    .replace(/auth0\|[A-Za-z0-9._|:-]+/g, '[subject]')
    .replace(/[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}/gi, '[id]')
    .replace(/\b[A-Za-z0-9_-]{32,}\b/g, '[secret]')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 300)
  return {
    ok: false,
    check: error instanceof AcceptanceError ? error.check : 'unexpected',
    error: error?.name || 'Error',
    message: error instanceof AcceptanceError ? scrub(error.message) : 'synthetic failed',
    secrets_recorded: false,
  }
}

export async function runSynthetic(config, dependencies = {}) {
  const requestImpl = dependencies.requestImpl || requestJson
  const provisionImpl = dependencies.provisionImpl || provisionAcceptanceDrawing
  const browserImpl = dependencies.browserImpl || runRealBrowser
  const sourceBytes = dependencies.sourceBytes || readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), '../../data/rooftop_demo.dwg'),
  )
  const startedAt = new Date().toISOString()
  const before = await loadAllPostures(config, requestImpl, 'before')
  const results = []
  for (const tenant of config.tenants) {
    const drawingId = await provisionImpl(config, tenant, sourceBytes, dependencies.provisionOptions)
    const versions = await requestImpl(
      config,
      tenant,
      `/api/drawings/${encodeURIComponent(drawingId)}/versions`,
    )
    expectStatus(versions, [200], `drawing_version_${tenant.label}`)
    if (versions.body?.head !== 1 || versions.body?.latest !== 1) {
      throw new AcceptanceError('drawing_binding', 'fresh uploaded drawing is not at version 1')
    }
    const conversation = await runConversation(config, tenant, drawingId, dependencies)
    results.push({ label: tenant.label, drawingId, ...conversation })
  }
  if (results[0].drawingId === results[1].drawingId) {
    throw new AcceptanceError('upload_isolation', 'the two uploads returned the same drawing id')
  }
  await proveNonEnumeration(config, results, dependencies)
  const browser = await browserImpl(config, results, dependencies.chromiumImpl)
  if (
    browser.length !== 2
    || browser[0].drawing_hash === browser[1].drawing_hash
    || browser.some((entry, index) =>
      entry.route_interceptions !== 0
      || entry.backend_ready !== true
      || entry.live_services !== true
      || entry.protected_api_authenticated !== true
      || entry.browser_bearer_hash !== identifierHash(config, config.tenants[index].jwt))
  ) {
    throw new AcceptanceError('browser', 'browser proof is incomplete or intercepted')
  }
  const after = await loadAllPostures(config, requestImpl, 'after')
  if (stableIdentity(before.identity) !== stableIdentity(after.identity)) {
    throw new AcceptanceError('identity_drift', 'deployment identity changed during the synthetic')
  }
  if (stableReadiness(before.ready) !== stableReadiness(after.ready)) {
    throw new AcceptanceError('readiness_drift', 'readiness changed during the synthetic')
  }
  const timestamps = { startedAt, stoppedAt: new Date().toISOString() }
  const proof = buildPublicProof(config, before, after, results, browser, timestamps)
  assertPublicProofIsCredentialFree(proof, config, results)
  const correlation = buildPrivateCorrelation(config, before, after, results, timestamps)
  assertVerifierTenantLabelContract(proof, correlation)
  return {
    proof,
    correlation,
  }
}

export async function main(argv = process.argv.slice(2), env = process.env, dependencies = {}) {
  try {
    const args = parseArgs(argv)
    if (args.help) {
      console.log(
        'Usage: node web/scripts/deployed_surface_synthetic_acceptance.mjs ' +
        '--proof <new-public-json> --correlation <new-private-json>',
      )
      return 0
    }
    if (!args.proof || !args.correlation || resolve(args.proof) === resolve(args.correlation)) {
      throw new AcceptanceError('configuration', 'distinct --proof and --correlation paths are required')
    }
    const config = validateSyntheticConfig(env)
    const result = await runSynthetic(config, dependencies)
    writeArtifactPair(args.proof, result.proof, args.correlation, result.correlation)
    console.log(JSON.stringify({
      ok: true,
      environment: config.environment,
      source_revision: config.expectedRevision,
      proof: resolve(args.proof),
      correlation: resolve(args.correlation),
      secrets_recorded: false,
    }))
    return 0
  } catch (error) {
    console.error(JSON.stringify(safeSyntheticFailure(error)))
    return 1
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  process.exitCode = await main()
}
