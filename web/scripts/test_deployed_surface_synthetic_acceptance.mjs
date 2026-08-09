import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, readFileSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, it } from 'node:test'

import { AcceptanceError } from './deployed_authored_cad_acceptance.mjs'
import {
  assertPublicProofIsCredentialFree,
  assertVerifierTenantLabelContract,
  buildPrivateCorrelation,
  buildPublicProof,
  evaluateConversationEvidence,
  requireUnknownEquivalent,
  runRealBrowser,
  runSynthetic,
  safeSyntheticFailure,
  validateSyntheticConfig,
  writeArtifactPair,
  writePrivateJson,
} from './deployed_surface_synthetic_acceptance.mjs'

const SHA = 'a'.repeat(40)
const DIGESTS = Object.fromEntries(
  ['app', 'broker', 'canonical-worker', 'harness', 'web'].map((name, index) => [
    name,
    { image_digest: `sha256:${String(index + 1).repeat(64)}`, source_revision: SHA },
  ]),
)

function jwt(subject, tenant) {
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString('base64url')
  return `${encode({ alg: 'none' })}.${encode({ sub: subject, 'https://leafdesign.ai/tenant_id': tenant })}.signature`
}

function environment(overrides = {}) {
  return {
    LEAF_SYNTHETIC_TARGET_ENV: 'staging',
    LEAF_SYNTHETIC_TARGET_URL: 'https://platform-staging.leafdesign.ai',
    LEAF_SYNTHETIC_EXPECTED_REVISION: SHA,
    LEAF_SYNTHETIC_RUN_ID: 'synthetic-123456',
    LEAF_ACCEPTANCE_TENANT_A_ID: 'tenant-a',
    LEAF_ACCEPTANCE_TENANT_A_JWT: jwt('auth0|account-a', 'tenant-a'),
    LEAF_ACCEPTANCE_TENANT_B_ID: 'tenant-b',
    LEAF_ACCEPTANCE_TENANT_B_JWT: jwt('auth0|account-b', 'tenant-b'),
    ...overrides,
  }
}

const IDS = {
  A: {
    drawing: '11111111-1111-4111-8111-111111111111',
    session: '11111111-1111-4111-9111-111111111111',
    first: '11111111-1111-4111-a111-111111111111',
    resumed: '11111111-1111-4111-b111-111111111111',
    job: '11111111-1111-4111-8111-222222222222',
    confirmation: 'confirmation-a-private',
  },
  B: {
    drawing: '22222222-2222-4222-8222-222222222222',
    session: '22222222-2222-4222-9222-222222222222',
    first: '22222222-2222-4222-a222-222222222222',
    resumed: '22222222-2222-4222-b222-222222222222',
    job: '22222222-2222-4222-8222-111111111111',
    confirmation: 'confirmation-b-private',
  },
}

function identity(revision = SHA) {
  return {
    schema: 'leaf.deployment-identity.v1',
    environment: 'staging',
    source_revision: revision,
    services: Object.fromEntries(Object.entries(DIGESTS).map(([name, service]) => [
      name,
      { ...service, source_revision: revision },
    ])),
  }
}

function readiness(revision = SHA) {
  return {
    ok: true,
    ready: true,
    status: 'ready',
    degraded_mode: false,
    source_revision: revision,
    duration_ms: 12,
    dependencies: Object.fromEntries(
      ['broker', 'harness', 'database', 'worker', 'durable_stores', 'build']
        .map((name) => [name, { state: 'ready', latency_ms: 3 }]),
    ),
  }
}

function eventsFor(label) {
  const ids = IDS[label]
  return [
    {
      seq: 1,
      type: 'proposed_run',
      turn_id: ids.first,
      data: {
        confirmation_id: ids.confirmation,
        tool: 'count-by-layer',
        params: {},
        dwg: ids.drawing,
        drawing_version: 1,
        capability: 'drawing.read',
      },
    },
    {
      seq: 2,
      type: 'turn_complete',
      turn_id: ids.first,
      data: { stop_reason: 'awaiting_approval' },
    },
    {
      seq: 3,
      type: 'job_linked',
      turn_id: ids.resumed,
      data: { job_id: ids.job, tool: 'count-by-layer' },
    },
    {
      seq: 4,
      type: 'tool_result',
      turn_id: ids.resumed,
      data: { tool: 'run_capability', ok: true, summary: 'complete' },
    },
    {
      seq: 5,
      type: 'turn_complete',
      turn_id: ids.resumed,
      data: { stop_reason: 'end_turn' },
    },
  ]
}

function terminalJob(label) {
  const ids = IDS[label]
  return {
    job_id: ids.job,
    tool: 'count-by-layer',
    dwg: ids.drawing,
    dwg_version: 1,
    provenance: { execution_path: 'cloud' },
    status: 'complete',
  }
}

function fakeDependencies({
  drift = false,
  principalMismatch = false,
  readinessDrift = false,
  readinessPrincipalMismatch = false,
  readinessTimingDrift = false,
  readinessDependencySemanticDrift = false,
  isolationStatus = 404,
  replayStatus = 409,
  events = {},
} = {}) {
  const messageCount = { A: 0, B: 0 }
  let identityReads = 0
  let readinessReads = 0
  const calls = []
  const requestImpl = async (_config, tenant, path, options = {}) => {
    calls.push({ label: tenant.label, path, method: options.method || 'GET' })
    const ids = IDS[tenant.label]
    if (path === '/api/deployment-identity') {
      identityReads += 1
      const body = identity(drift && identityReads > 2 ? 'b'.repeat(40) : SHA)
      if (principalMismatch && identityReads === 2) {
        body.services.web.image_digest = `sha256:${'9'.repeat(64)}`
      }
      return { status: 200, body }
    }
    if (path === '/api/ready') {
      readinessReads += 1
      const body = readiness()
      if (readinessTimingDrift) {
        body.duration_ms = readinessReads * 101
        for (const [index, dependency] of Object.values(body.dependencies).entries()) {
          dependency.latency_ms = readinessReads * 1000 + index
        }
      }
      if (readinessPrincipalMismatch && readinessReads === 2) {
        body.release_marker = 'other-principal'
      }
      if (readinessDrift && readinessReads > 2) body.release_marker = 'after'
      if (readinessDependencySemanticDrift && readinessReads > 2) {
        body.dependencies.broker.authority = 'changed'
      }
      return { status: 200, body }
    }
    if (path === `/api/drawings/${ids.drawing}/versions`) {
      return { status: 200, body: { head: 1, latest: 1, versions: [{ v: 1 }] } }
    }
    if (path === '/api/sessions' && options.method === 'POST') {
      assert.equal(options.body.drawing_id, ids.drawing)
      return { status: 200, body: { session_id: ids.session } }
    }
    if (path === `/api/sessions/${ids.session}/messages`) {
      messageCount[tenant.label] += 1
      if (messageCount[tenant.label] === 1) {
        assert.equal(options.body.text, 'RUN:count-by-layer')
        return { status: 202, body: { turn_id: ids.first } }
      }
      if (messageCount[tenant.label] === 2) {
        assert.deepEqual(options.body.confirm, {
          confirmationId: ids.confirmation,
          approved: true,
        })
        return { status: 202, body: { turn_id: ids.resumed } }
      }
      return {
        status: replayStatus,
        body: { error: { error_code: 'BAD_PARAMS', message: 'confirmation was already consumed' } },
      }
    }
    if (path === `/api/sessions/${ids.session}/transcript?limit=500`) {
      return { status: 200, body: { events: events[tenant.label] || eventsFor(tenant.label) } }
    }
    if (path === `/api/agent/approvals/${ids.confirmation}`) {
      return { status: 200, body: { resolved: true, approved: true } }
    }
    if (path === `/api/jobs/${ids.job}`) {
      return { status: 200, body: terminalJob(tenant.label) }
    }
    // All foreign and random controls deliberately collapse to one response.
    if (/^\/api\/(drawings|jobs|sessions)\//.test(path)) {
      return {
        status: isolationStatus,
        body: { error: { error_code: 'BAD_PARAMS', message: 'unknown resource' } },
      }
    }
    throw new Error(`unexpected fake request ${path}`)
  }
  return {
    calls,
    requestImpl,
    sourceBytes: new Uint8Array([1, 2, 3]),
    provisionImpl: async (_config, tenant) => IDS[tenant.label].drawing,
    browserImpl: async (config, results) => results.map((result) => ({
      label: result.label,
      drawing_hash: result.label === 'A' ? 'a'.repeat(64) : 'b'.repeat(64),
      backend_ready: true,
      live_services: true,
      route_interceptions: 0,
      target: config.targetUrl,
    })),
    waitImpl: async () => {},
    maxPolls: 2,
  }
}

function fakeChromium(
  config,
  results,
  { extraOrigin = null, finalPath = '/try', finalOrigin = config.targetUrl } = {},
) {
  const state = {
    contextOptions: [],
    initPayloads: [],
    primedStorage: [],
    gotos: [],
    routeCalls: 0,
    contextCloses: 0,
    browserCloses: 0,
  }
  let contextIndex = 0
  const chromium = {
    async launch(options) {
      assert.deepEqual(options, { headless: true })
      return {
        async newContext(contextOptions) {
          const index = contextIndex++
          const result = results[index]
          state.contextOptions.push(contextOptions)
          let initPayload
          return {
            async addInitScript(script, payload) {
              initPayload = payload
              state.initPayloads.push(payload)
              const local = new Map()
              const session = new Map()
              const priorWindow = globalThis.window
              globalThis.window = {
                localStorage: { setItem: (key, value) => local.set(key, value) },
                sessionStorage: { setItem: (key, value) => session.set(key, value) },
              }
              try {
                script(payload)
              } finally {
                if (priorWindow === undefined) delete globalThis.window
                else globalThis.window = priorWindow
              }
              state.primedStorage.push({ local, session })
            },
            async newPage() {
              const requestHandlers = []
              let currentUrl = `${finalOrigin}${finalPath}`
              return {
                on(name, handler) {
                  if (name === 'request') requestHandlers.push(handler)
                },
                async route() {
                  state.routeCalls += 1
                },
                async goto(path) {
                  state.gotos.push(path)
                  assert.equal(path, '/try')
                  assert.equal(initPayload.drawingId, result.drawingId)
                  const request = (url) => ({ url: () => url })
                  for (const handler of requestHandlers) {
                    handler(request(`${config.targetUrl}/try`))
                    if (extraOrigin) handler(request(`${extraOrigin}/asset.js`))
                  }
                  currentUrl = `${finalOrigin}${finalPath}`
                },
                getByTestId(name) {
                  assert.equal(name, 'operator-phase')
                  return {
                    filter({ hasText }) {
                      assert.equal(hasText, 'Backend ready')
                      return { waitFor: async () => {} }
                    },
                  }
                },
                getByText(text) {
                  if (text === 'Deterministic browser proof.') return { count: async () => 0 }
                  if (text === 'Live services') return { isVisible: async () => true }
                  throw new Error(`unexpected text locator ${text}`)
                },
                locator(selector) {
                  assert.equal(selector, '.tc-bar-proj')
                  return { innerText: async () => result.drawingId }
                },
                url() {
                  return currentUrl
                },
              }
            },
            async close() {
              state.contextCloses += 1
            },
          }
        },
        async close() {
          state.browserCloses += 1
        },
      }
    },
  }
  return { chromium, state }
}

describe('surface synthetic configuration', () => {
  it('accepts only the exact fixed staging and production origins', () => {
    assert.equal(validateSyntheticConfig(environment(), 'linux').targetUrl,
      'https://platform-staging.leafdesign.ai')
    assert.throws(
      () => validateSyntheticConfig(environment({
        LEAF_SYNTHETIC_TARGET_URL: 'https://platform-staging.leafdesign.ai.evil.example',
      }), 'linux'),
      /must be exactly/,
    )
    assert.throws(
      () => validateSyntheticConfig(environment({
        LEAF_SYNTHETIC_TARGET_ENV: 'prod',
        LEAF_SYNTHETIC_TARGET_URL: 'https://platform-staging.leafdesign.ai',
      }), 'linux'),
      /must be exactly/,
    )
  })

  it('requires distinct Auth0 account subjects, tenant ids, and tokens', () => {
    assert.throws(
      () => validateSyntheticConfig(environment({
        LEAF_ACCEPTANCE_TENANT_B_ID: 'tenant-a',
        LEAF_ACCEPTANCE_TENANT_B_JWT: jwt('auth0|account-a', 'tenant-a'),
      }), 'linux'),
      /must be distinct/,
    )
    assert.throws(
      () => validateSyntheticConfig(environment({
        LEAF_ACCEPTANCE_TENANT_B_JWT: jwt('github|account-b', 'tenant-b'),
      }), 'linux'),
      /not an Auth0 account principal/,
    )
  })

  it('requires a POSIX runner for the private mode-0600 artifact contract', () => {
    assert.throws(
      () => validateSyntheticConfig(environment(), 'win32'),
      /requires a POSIX runner/,
    )
  })
})

describe('surface synthetic conversation contract', () => {
  it('accepts only exact uploaded drawing/version and terminal event evidence', () => {
    const ids = IDS.A
    assert.equal(evaluateConversationEvidence({
      events: eventsFor('A'),
      drawingId: ids.drawing,
      confirmationId: ids.confirmation,
      firstTurnId: ids.first,
      resumedTurnId: ids.resumed,
      job: terminalJob('A'),
    }).jobId, ids.job)

    const wrongDrawing = structuredClone(eventsFor('A'))
    wrongDrawing[0].data.dwg = IDS.B.drawing
    assert.throws(() => evaluateConversationEvidence({
      events: wrongDrawing,
      drawingId: ids.drawing,
      confirmationId: ids.confirmation,
      firstTurnId: ids.first,
      resumedTurnId: ids.resumed,
      job: terminalJob('A'),
    }), /wrong tool, drawing, or confirmation id/)

    const wrongVersion = { ...terminalJob('A'), dwg_version: 2 }
    assert.throws(() => evaluateConversationEvidence({
      events: eventsFor('A'),
      drawingId: ids.drawing,
      confirmationId: ids.confirmation,
      firstTurnId: ids.first,
      resumedTurnId: ids.resumed,
      job: wrongVersion,
    }), /version 1/)
  })

  it('rejects missing job_linked, tool_result, turn_complete, and live job pins', () => {
    const ids = IDS.A
    for (const missing of ['job_linked', 'tool_result', 'turn_complete']) {
      const events = eventsFor('A').filter((event) =>
        !(event.type === missing && event.turn_id === ids.resumed))
      assert.throws(() => evaluateConversationEvidence({
        events,
        drawingId: ids.drawing,
        confirmationId: ids.confirmation,
        firstTurnId: ids.first,
        resumedTurnId: ids.resumed,
        job: terminalJob('A'),
      }))
    }
    assert.throws(() => evaluateConversationEvidence({
      events: eventsFor('A'),
      drawingId: ids.drawing,
      confirmationId: ids.confirmation,
      firstTurnId: ids.first,
      resumedTurnId: ids.resumed,
      job: { ...terminalJob('A'), dwg_version: null },
    }), /exact live uploaded-drawing version 1/)
  })

  it('requires one exact initial awaiting-approval terminal', () => {
    const ids = IDS.A
    const evaluate = (events) => evaluateConversationEvidence({
      events,
      drawingId: ids.drawing,
      confirmationId: ids.confirmation,
      firstTurnId: ids.first,
      resumedTurnId: ids.resumed,
      job: terminalJob('A'),
    })
    const duplicateTerminal = structuredClone(eventsFor('A'))
    duplicateTerminal.splice(2, 0, structuredClone(duplicateTerminal[1]))
    assert.throws(() => evaluate(duplicateTerminal), /end exactly once awaiting approval/)

    const wrongStop = structuredClone(eventsFor('A'))
    wrongStop[1].data.stop_reason = 'end_turn'
    assert.throws(() => evaluate(wrongStop), /end exactly once awaiting approval/)

    const duplicateProposal = structuredClone(eventsFor('A'))
    duplicateProposal.splice(1, 0, structuredClone(duplicateProposal[0]))
    assert.throws(() => evaluate(duplicateProposal), /exactly one approval proposal/)

    for (const conflictType of ['error', 'confirmation_required', 'job_linked', 'tool_result']) {
      const conflict = structuredClone(eventsFor('A'))
      conflict.splice(1, 0, {
        type: conflictType,
        turn_id: ids.first,
        data: conflictType === 'error'
          ? { stop_reason: 'error' }
          : conflictType === 'confirmation_required'
            ? { confirmation_id: 'second-confirmation' }
            : { job_id: IDS.A.job, tool: 'count-by-layer', ok: true },
      })
      assert.throws(() => evaluate(conflict), /error, execution, or conflicting approval event/)
    }
  })

  it('requires one ordered successful resumed sequence with no conflicting events', () => {
    const ids = IDS.A
    const evaluate = (events) => evaluateConversationEvidence({
      events,
      drawingId: ids.drawing,
      confirmationId: ids.confirmation,
      firstTurnId: ids.first,
      resumedTurnId: ids.resumed,
      job: terminalJob('A'),
    })
    const reorder = structuredClone(eventsFor('A'))
    ;[reorder[2], reorder[3]] = [reorder[3], reorder[2]]
    assert.throws(() => evaluate(reorder), /one ordered/)

    for (const type of ['job_linked', 'tool_result', 'turn_complete']) {
      const duplicate = structuredClone(eventsFor('A'))
      const event = duplicate.find((candidate) =>
        candidate.type === type && candidate.turn_id === ids.resumed)
      duplicate.push(structuredClone(event))
      assert.throws(() => evaluate(duplicate), /one ordered/)
    }

    const wrongLinkedTool = structuredClone(eventsFor('A'))
    wrongLinkedTool[2].data.tool = 'list-layers'
    assert.throws(() => evaluate(wrongLinkedTool), /job_linked names the wrong tool/)

    const wrongResultTool = structuredClone(eventsFor('A'))
    wrongResultTool[3].data.tool = 'count-by-layer'
    assert.throws(() => evaluate(wrongResultTool), /successful run_capability/)

    for (const stop of [
      'awaiting_approval',
      'error',
      'timeout',
      'interrupted',
      'llm_quota_exhausted',
      'llm_rate_limited',
      'cap_hit',
    ]) {
      const failed = structuredClone(eventsFor('A'))
      failed[4].data.stop_reason = stop
      assert.throws(() => evaluate(failed), /failure, second approval, or non-success stop/)
    }

    for (const conflictType of ['error', 'proposed_run', 'confirmation_required']) {
      const conflicting = structuredClone(eventsFor('A'))
      conflicting.splice(4, 0, {
        type: conflictType,
        turn_id: ids.resumed,
        data: conflictType === 'error'
          ? { stop_reason: 'error' }
          : { confirmation_id: 'second-confirmation', tool: 'count-by-layer' },
      })
      assert.throws(() => evaluate(conflicting), /failure, second approval, or non-success stop/)
    }
  })

  it('requires foreign drawing, job, and transcript probes to match unknown controls', () => {
    for (const status of [403, 404]) {
      const denied = { status, body: { error: { error_code: 'BAD_PARAMS' } } }
      requireUnknownEquivalent(denied, structuredClone(denied), 'job')
    }
    requireUnknownEquivalent(
      { status: 404, body: { error: `unknown job ${IDS.A.job}` } },
      { status: 404, body: { error: `unknown job ${IDS.B.job}` } },
      'job',
      [IDS.A.job, IDS.B.job],
    )
    assert.throws(
      () => requireUnknownEquivalent(
        { status: 403, body: {} },
        { status: 404, body: {} },
        'job',
      ),
      /different status/,
    )
    assert.throws(
      () => requireUnknownEquivalent(
        { status: 404, body: { error: 'foreign' } },
        { status: 404, body: { error: 'unknown' } },
        'job',
      ),
      /distinguishable bodies/,
    )
    assert.throws(
      () => requireUnknownEquivalent(
        { status: 200, body: { resource: null } },
        { status: 200, body: { resource: null } },
        'job',
      ),
      /foreign resource returned HTTP 200/,
    )
  })

  it('runs both tenants, requires replay 409, isolation controls, and stable identity', async () => {
    const config = validateSyntheticConfig(environment(), 'linux')
    const dependencies = fakeDependencies()
    const result = await runSynthetic(config, dependencies)
    assert.equal(result.proof.tenants.length, 2)
    assert.deepEqual(result.proof.tenants.map((tenant) => tenant.label), ['a', 'b'])
    assert.deepEqual(result.proof.browser.map((tenant) => tenant.label), ['a', 'b'])
    assert.deepEqual(result.correlation.tenants.map((tenant) => tenant.label), ['a', 'b'])
    assert.equal(result.proof.assertions[12].name, 'confirmation_replay_refused')
    assert.ok(dependencies.calls.filter((call) => call.path.includes('/messages')).length === 6)
    assert.equal(
      dependencies.calls.filter((call) => call.path === '/api/deployment-identity').length,
      4,
    )
    assert.equal(dependencies.calls.filter((call) => call.path === '/api/ready').length, 4)
    assert.ok(dependencies.calls.some((call) => call.path.includes(IDS.B.job)))
    assert.ok(dependencies.calls.some((call) => call.path.includes(IDS.A.session)))

    await runSynthetic(config, fakeDependencies({ isolationStatus: 403 }))
    await assert.rejects(
      () => runSynthetic(config, fakeDependencies({ isolationStatus: 200 })),
      /foreign resource returned HTTP 200/,
    )

    const timingOnly = await runSynthetic(
      config,
      fakeDependencies({ readinessTimingDrift: true }),
    )
    assert.equal(timingOnly.proof.assertions[3].name, 'readiness_green')

    await assert.rejects(
      () => runSynthetic(config, fakeDependencies({ replayStatus: 200 })),
      /unexpected HTTP 200/,
    )
    await assert.rejects(
      () => runSynthetic(config, fakeDependencies({ drift: true })),
      /wrong source revision|changed during/,
    )
    await assert.rejects(
      () => runSynthetic(config, fakeDependencies({ principalMismatch: true })),
      /differs between Auth0 principals/,
    )
    await assert.rejects(
      () => runSynthetic(config, fakeDependencies({ readinessPrincipalMismatch: true })),
      /readiness differs between Auth0 principals/,
    )
    await assert.rejects(
      () => runSynthetic(config, fakeDependencies({ readinessDrift: true })),
      /readiness changed during the synthetic/,
    )
    await assert.rejects(
      () => runSynthetic(config, fakeDependencies({ readinessDependencySemanticDrift: true })),
      /readiness changed during the synthetic/,
    )
  })
})

describe('surface synthetic browser contract', () => {
  function browserFixture() {
    const config = validateSyntheticConfig(environment(), 'linux')
    const results = ['A', 'B'].map((label) => ({
      label,
      drawingId: IDS[label].drawing,
    }))
    return { config, results }
  }

  it('primes each isolated browser with its Auth0 token and intended drawing', async () => {
    const { config, results } = browserFixture()
    const fake = fakeChromium(config, results)
    const observed = await runRealBrowser(config, results, fake.chromium)
    assert.deepEqual(fake.state.contextOptions, [
      { baseURL: config.targetUrl, serviceWorkers: 'block' },
      { baseURL: config.targetUrl, serviceWorkers: 'block' },
    ])
    assert.deepEqual(fake.state.initPayloads, [
      { token: config.tenants[0].jwt, drawingId: IDS.A.drawing },
      { token: config.tenants[1].jwt, drawingId: IDS.B.drawing },
    ])
    assert.deepEqual(fake.state.primedStorage.map(({ local, session }) => ({
      token: local.get('leaf.jwt'),
      drawing: session.get('leaf.cat.workbench.id.v1'),
    })), [
      { token: config.tenants[0].jwt, drawing: IDS.A.drawing },
      { token: config.tenants[1].jwt, drawing: IDS.B.drawing },
    ])
    assert.deepEqual(fake.state.gotos, ['/try', '/try'])
    assert.equal(fake.state.routeCalls, 0)
    assert.equal(fake.state.contextCloses, 2)
    assert.equal(fake.state.browserCloses, 1)
    assert.deepEqual(observed.map((entry) => entry.route_interceptions), [0, 0])
    assert.deepEqual(observed.map((entry) => entry.live_services), [true, true])
    assert.deepEqual(observed.map((entry) => entry.label), ['a', 'b'])
  })

  it('rejects any browser request outside the exact target origin', async () => {
    const { config, results } = browserFixture()
    const fake = fakeChromium(config, results, { extraOrigin: 'https://leafautomation.us.auth0.com' })
    await assert.rejects(
      () => runRealBrowser(config, results, fake.chromium),
      /unexpected origin/,
    )
    assert.equal(fake.state.routeCalls, 0)
    assert.equal(fake.state.contextCloses, 1)
    assert.equal(fake.state.browserCloses, 1)
  })

  it('requires the final navigation to remain on the exact target /try path', async () => {
    const { config, results } = browserFixture()
    for (const options of [
      { finalPath: '/app' },
      { finalOrigin: 'https://example.test' },
      { finalPath: '/try?proof=1' },
      { finalPath: '/try#proof' },
    ]) {
      const fake = fakeChromium(config, results, options)
      await assert.rejects(
        () => runRealBrowser(config, results, fake.chromium),
        /exact target \/try surface/,
      )
      assert.equal(fake.state.routeCalls, 0)
    }
  })
})

describe('surface synthetic artifacts', () => {
  it('publishes hashes only and keeps exact required assertion order', () => {
    const config = validateSyntheticConfig(environment(), 'linux')
    const before = { identity: identity(), ready: readiness() }
    const results = ['A', 'B'].map((label) => ({
      label,
      drawingId: IDS[label].drawing,
      sessionId: IDS[label].session,
      firstTurnId: IDS[label].first,
      resumedTurnId: IDS[label].resumed,
      confirmationId: IDS[label].confirmation,
      jobId: IDS[label].job,
    }))
    const proof = buildPublicProof(config, before, before, results, [
      { label: 'A', route_interceptions: 0 },
      { label: 'B', route_interceptions: 0 },
    ], {
      startedAt: '2026-08-08T00:00:00.000Z',
      stoppedAt: '2026-08-08T00:01:00.000Z',
    })
    const correlation = buildPrivateCorrelation(config, before, before, results, {
      startedAt: '2026-08-08T00:00:00.000Z',
      stoppedAt: '2026-08-08T00:01:00.000Z',
    })
    assert.equal(proof.schema, 'leaf.surface-reconciliation-proof-input.v1')
    assert.equal(
      correlation.schema,
      'leaf.surface-reconciliation-private-correlation.v1',
    )
    assertVerifierTenantLabelContract(proof, correlation)
    assert.deepEqual(proof.tenants.map((tenant) => tenant.label), ['a', 'b'])
    assert.deepEqual(correlation.tenants.map((tenant) => tenant.label), ['a', 'b'])
    assert.throws(
      () => assertVerifierTenantLabelContract(
        { ...proof, tenants: [{ label: 'A' }, { label: 'B' }] },
        correlation,
      ),
      /exactly lowercase a, b/,
    )
    assertPublicProofIsCredentialFree(proof, config, results)
    const encoded = JSON.stringify(proof)
    for (const value of [
      ...config.tenants.flatMap((tenant) => [tenant.jwt, tenant.id, tenant.subject]),
      ...Object.values(IDS).flatMap((ids) => Object.values(ids)),
    ]) assert.ok(!encoded.includes(value), value)
    assert.deepEqual(proof.assertions.map((item) => item.name), [
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
    ])
    assert.deepEqual(Object.keys(proof.resource_hashes), [
      'tenant_ids',
      'subjects',
      'drawing_ids',
      'session_ids',
      'turn_ids',
      'confirmation_ids',
      'job_ids',
    ])
    assert.deepEqual(proof.resource_hashes.turn_ids.map((turns) => turns.length), [2, 2])
  })

  it('writes the raw correlation through an exclusive private create', () => {
    const config = validateSyntheticConfig(environment(), 'linux')
    const results = ['A', 'B'].map((label) => ({
      label,
      drawingId: IDS[label].drawing,
      sessionId: IDS[label].session,
      firstTurnId: IDS[label].first,
      resumedTurnId: IDS[label].resumed,
      confirmationId: IDS[label].confirmation,
      jobId: IDS[label].job,
    }))
    const correlation = buildPrivateCorrelation(
      config,
      { identity: identity() },
      { identity: identity() },
      results,
      { startedAt: 'start', stoppedAt: 'stop' },
    )
    const dir = mkdtempSync(join(tmpdir(), 'leaf-synthetic-'))
    const path = join(dir, 'correlation.json')
    writePrivateJson(path, correlation)
    assert.equal(JSON.parse(readFileSync(path, 'utf8')).tenants[0].job_id, IDS.A.job)
    if (process.platform !== 'win32') assert.equal(statSync(path).mode & 0o777, 0o600)
    assert.throws(() => writePrivateJson(path, correlation), /EEXIST/)

    const proofPath = join(dir, 'proof.json')
    assert.throws(
      () => writeArtifactPair(proofPath, { ok: true }, path, correlation),
      /EEXIST/,
    )
    assert.equal(existsSync(proofPath), false)
  })

  it('scrubs tokens, subjects, UUIDs, and opaque secrets from safe failure output', () => {
    const token = jwt('auth0|secret-subject', 'tenant-secret')
    const error = new Error(`${token} auth0|secret-subject ${IDS.A.job} ${'z'.repeat(40)}`)
    error.name = 'Exploded'
    const safe = safeSyntheticFailure(error)
    assert.equal(safe.message, 'synthetic failed')

    const acceptance = new AcceptanceError('safe_failure', error.message)
    const scrubbed = JSON.stringify(safeSyntheticFailure(acceptance))
    assert.ok(!scrubbed.includes(token))
    assert.ok(!scrubbed.includes('auth0|secret-subject'))
    assert.ok(!scrubbed.includes(IDS.A.job))
    assert.ok(!scrubbed.includes('z'.repeat(40)))
    // This source-level negative test also prevents the live driver from adding
    // request interception.
    const source = readFileSync(
      new URL('./deployed_surface_synthetic_acceptance.mjs', import.meta.url),
      'utf8',
    )
    assert.ok(!source.includes('.route('))
  })
})
