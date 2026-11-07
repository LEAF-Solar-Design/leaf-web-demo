import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { deliveryRepo, stagingDelivery } from './stagingDelivery.mjs'
import { readHarnessCommit, walkProvenance } from './stagingProvenance.mjs'
import { stagingEvidenceStep } from '../walk-staging/projectSolarSteps.mjs'

const baseURL = 'https://platform-staging.leafdesign.ai'
const appSha = 'a'.repeat(40), webSha = 'b'.repeat(40), harnessSha = 'c'.repeat(40)
const readAt = '2026-10-18T12:00:00.000Z'
const now = () => new Date(readAt)

function healthResponse(url, body, status = 200) {
  let read = false
  return { url, status, redirected: false, body: { getReader: () => ({
    read: async () => {
      if (read) return { done: true }
      read = true
      return { done: false, value: Buffer.from(JSON.stringify(body)) }
    },
    cancel: async () => {},
  }) } }
}

const fetchHealth = async url => healthResponse(url,
  { source_sha: new URL(url).pathname === '/api/health' ? appSha : webSha })

test('harness commit reads HEAD with a bounded shell-free Git call', async () => {
  let calls = 0
  const harness = await readHarnessCommit({ now, run: async (args, options) => {
    calls++
    assert.deepEqual(args, ['rev-parse', 'HEAD'])
    assert.deepEqual(options, { cwd: deliveryRepo, shell: false, timeout: 2000, maxBuffer: 4096 })
    return { code: 0, stdout: `${harnessSha}\n` }
  } })
  assert.equal(calls, 1)
  assert.deepEqual(harness, { sha: harnessSha, read_at: readAt, reason: 'ok' })
})

test('harness commit failures and malformed results remain unavailable', async () => {
  for (const result of [
    { code: 128, stdout: harnessSha }, { code: 'ETIMEDOUT', stdout: harnessSha },
    { code: 0, stdout: 'abc' }, { code: 0, stdout: 'z'.repeat(40) },
    { code: 0, stdout: '' }, { code: 0, stdout: null }, undefined,
  ]) {
    assert.deepEqual(await readHarnessCommit({ now, run: async () => result }),
      { sha: null, read_at: readAt, reason: 'unavailable' })
  }
  assert.deepEqual(await readHarnessCommit({ now, run: () => { throw new Error('missing Git') } }),
    { sha: null, read_at: readAt, reason: 'unavailable' })
  const badClock = await readHarnessCommit({ now: () => { throw new Error('bad clock') },
    run: () => assert.fail('clock failure must remain diagnostic') })
  assert.equal(badClock.sha, null)
  assert.equal(badClock.reason, 'unavailable')
  assert.equal(new Date(badClock.read_at).toISOString(), badClock.read_at)
})

test('provenance collects both deployments without configuring a delivery verdict or ancestry', async () => {
  const calls = []
  const delivery = await stagingDelivery(baseURL, { env: {}, now, collectProvenance: true,
    ancestry: () => assert.fail('no expected SHA means no ancestry'),
    fetchImpl: async (url, options) => {
      calls.push(url)
      assert.equal(options.credentials, 'omit')
      return fetchHealth(url)
    } })
  assert.deepEqual(calls, [`${baseURL}/api/health`, `${baseURL}/health.json`])
  assert.equal(delivery.decision, 'not_configured')
  assert.equal(delivery.expected_sha, null)
  assert.equal(delivery.app.relation, 'unknown')
  assert.equal(delivery.web.relation, 'unknown')
  assert.equal(delivery.app.http_status, 200)
  assert.equal(delivery.web.http_status, 200)
  const harness = { sha: harnessSha, read_at: readAt, reason: 'ok' }
  const input = structuredClone(delivery)
  assert.deepEqual(walkProvenance(delivery, harness), {
    schema: 'leaf.walk-provenance.v1', harness_commit: harness,
    deployed_app_sha: { sha: appSha, read_at: readAt, reason: 'ok' },
    deployed_web_sha: { sha: webSha, read_at: readAt, reason: 'ok' },
  })
  assert.deepEqual(delivery, input)
})

test('unconfigured delivery remains identical with provenance disabled or omitted', async () => {
  const options = { env: {}, now, fetchImpl: () => assert.fail('no optional fetch'),
    ancestry: () => assert.fail('no optional Git') }
  const delivery = await stagingDelivery(baseURL, { ...options, collectProvenance: false })
  assert.deepEqual(delivery, await stagingDelivery(baseURL, options))
  assert.deepEqual(delivery, {
    schema: 'leaf.staging-delivery.v1', checkedAt: readAt, expected_sha: null,
    app: { source_sha: null, release_source_sha: null, source_identity: 'unknown',
      observation: 'not_configured', http_status: null, relation: 'unknown' },
    web: { source_sha: null, observation: 'not_configured', http_status: null, relation: 'unknown' },
    decision: 'not_configured',
  })
  const provenance = walkProvenance(delivery, { sha: null, read_at: readAt, reason: 'unavailable' })
  for (const entry of [provenance.deployed_app_sha, provenance.deployed_web_sha]) {
    assert.deepEqual(entry, { sha: null, read_at: null, reason: 'not_configured' })
  }
})

test('attempted invalid and unavailable health reads have times and no deployed SHA', async () => {
  const delivery = await stagingDelivery(baseURL, { env: {}, now, collectProvenance: true,
    fetchImpl: async url => healthResponse(url, { source_sha: 'invalid' },
      new URL(url).pathname === '/api/health' ? 200 : 503) })
  const provenance = walkProvenance(delivery, {})
  assert.deepEqual(provenance.deployed_app_sha, { sha: null, read_at: readAt, reason: 'invalid' })
  assert.deepEqual(provenance.deployed_web_sha, { sha: null, read_at: readAt, reason: 'unavailable' })
})

test('unverified app identity is preserved in provenance', async () => {
  const delivery = await stagingDelivery(baseURL, { env: {}, now, collectProvenance: true,
    fetchImpl: async url => healthResponse(url, { source_sha: appSha,
      source_identity: 'adopted', release_source_sha: webSha }) })
  assert.deepEqual(walkProvenance(delivery, {}).deployed_app_sha,
    { sha: appSha, read_at: readAt, reason: 'identity_unverified' })
})

test('provenance collection still refuses URL credentials and disallowed hosts before fetching', async () => {
  for (const url of ['https://user:password@platform-staging.leafdesign.ai',
    'https://app.leafdesign.ai', 'https://platform-staging.leafdesign.ai:444']) {
    await assert.rejects(stagingDelivery(url, { env: {}, collectProvenance: true,
      fetchImpl: () => assert.fail('unsafe fetch') }))
  }
})

test('configured delivery is unchanged by provenance collection', async () => {
  const options = { env: { LEAF_E2E_EXPECTED_SHA: harnessSha }, now, fetchImpl: fetchHealth,
    ancestry: async () => 'ancestor' }
  assert.deepEqual(await stagingDelivery(baseURL, { ...options, collectProvenance: true }),
    await stagingDelivery(baseURL, options))
})

test('evidence step persists provenance on normal and stale paths for either opt-in', async () => {
  for (const stale of [false, true]) {
    for (const envOptIn of [false, true]) {
      const runtime = { page: new EventEmitter(), stack: { baseURL }, evidence: { projectSolar: {} } }
      const env = stale ? { LEAF_E2E_EXPECTED_SHA: harnessSha } : {}
      if (envOptIn) env.LEAF_E2E_WALK_PROVENANCE = '1'
      let persisted, gitCalls = 0, actions = 0
      const skip = new Error('Test is skipped: infra_stale_delivery')
      const testInfo = { title: 'provenance', annotations: [], skip: () => { throw skip } }
      const work = () => stagingEvidenceStep(runtime, testInfo, async () => { actions++; return 7 },
        async evidence => { persisted = structuredClone(evidence) }, {
          env, now, collectProvenance: !envOptIn, fetchImpl: fetchHealth, ancestry: async () => 'ancestor',
          run: async () => { gitCalls++; return { code: 0, stdout: harnessSha } },
        })
      if (stale) await assert.rejects(work(), error => error === skip)
      else assert.equal(await work(), 7)
      assert.equal(actions, stale ? 0 : 1)
      assert.equal(gitCalls, 1)
      assert.deepEqual(persisted.projectSolar.provenance,
        walkProvenance(persisted.projectSolar.delivery, { sha: harnessSha, read_at: readAt, reason: 'ok' }))
    }
  }
})

test('evidence step without opt-in adds no provenance and reads no Git', async () => {
  const runtime = { page: new EventEmitter(), stack: { baseURL }, evidence: { projectSolar: {} } }
  let persisted
  await stagingEvidenceStep(runtime, { title: 'no-provenance' }, async () => {},
    async evidence => { persisted = structuredClone(evidence) }, {
      env: {}, now, fetchImpl: () => assert.fail('no optional fetch'),
      run: () => assert.fail('no optional Git'),
    })
  assert.equal(Object.hasOwn(persisted.projectSolar, 'provenance'), false)
})
