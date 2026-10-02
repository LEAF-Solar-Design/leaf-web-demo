import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { collectProbeUxEvidence, packUxEvidence, ID } from './uxEvidence.mjs'

const sample = { rect: { x: 10, y: 10, width: 50, height: 30 },
  viewport: { width: 1280, height: 800 }, visible: true, enabled: true, hit: 'self' }
const probe = { featureId: 'action:test', kind: 'action', state: 'ready',
  setup: { steps: [{ kind: 'settle' }] }, locator: { role: 'button', name: 'Test' },
  assertion: { assertionId: 'test-effect', kind: 'opens' } }

// Exercise the actual runProbe body with its Playwright dependencies replaced;
// importing fixtures would require a running Playwright test for test.step.
const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
const body = source.slice(source.indexOf('export async function runProbe(probe, runtime) {')
  + 'export async function runProbe(probe, runtime) {'.length,
source.indexOf('\n// This reporter')).trim().slice(0, -1)
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor

async function run({ matches = 1, throwing = false, state = 'ready' } = {}) {
  const log = []
  const locator = {
    waitFor: async () => { log.push('attached') },
    count: async () => matches,
    evaluate: async () => { log.push('sample'); if (throwing) throw new Error('read failed'); return sample },
  }
  const runtime = { page: {}, evidence: { steps: [] }, testInfo: { project: { name: 'desktop' } } }
  const runner = new AsyncFunction('probe', 'runtime', 'test', 'expect', 'control', 'setupStep',
    'captureBefore', 'activate', 'assertEffect', 'normalizedControlKey', 'groupNames',
    'collectProbeUxEvidence', 'packUxEvidence', body)
  await runner({ ...probe, state }, runtime, { step: async (_name, fn) => fn() },
    () => ({ toBeVisible: async () => { log.push('visible') } }), () => locator,
    async () => { log.push('setup') }, async () => { log.push('before') },
    async () => { log.push('activate') }, async () => { log.push('assert') },
    () => {}, {}, collectProbeUxEvidence, packUxEvidence)
  return { log, evidence: runtime.evidence }
}

test('runner samples after settled setup and before any activation', async () => {
  const { log, evidence } = await run()
  assert.deepEqual(log, ['setup', 'attached', 'sample', 'visible', 'before', 'activate', 'assert'])
  assert.deepEqual(evidence.ux_observations.map(row => [row.metric_id, row.observed]),
    [['control_covered_at_rest', 0], ['scroll_needed_steps', 0]])
  assert.ok(evidence.ux_observations.every(row => row.viewport === 'desktop' && row.lens_id === 'reachability'))
})

test('throwing evaluate preserves result and attaches no UX observations', async () => {
  const baseline = await run()
  const failedRead = await run({ throwing: true })
  assert.deepEqual(failedRead.evidence.result, baseline.evidence.result)
  assert.equal(Object.hasOwn(failedRead.evidence, 'ux_observations'), false)
  assert.deepEqual(failedRead.log.slice(-3), ['before', 'activate', 'assert'])
})

test('zero or two matches attach nothing and preserve the result', async () => {
  const baseline = await run()
  for (const matches of [0, 2]) {
    const result = await run({ matches })
    assert.deepEqual(result.evidence.result, baseline.evidence.result)
    assert.equal(Object.hasOwn(result.evidence, 'ux_observations'), false)
    assert.equal(result.log.includes('sample'), false)
  }
})

test('state ids replace unsafe characters and satisfy the ID pattern', async () => {
  const { evidence } = await run({ state: 'ready:with spaces/child' })
  assert.equal(evidence.ux_observations[0].state, 'ready-with-spaces-child')
  for (const state of ['-ready', '', 'x'.repeat(100)]) {
    const result = await run({ state })
    assert.match(result.evidence.ux_observations[0].state, ID)
  }
})

test('fake timer bounds attachment wait and evaluate together to two seconds', async () => {
  for (const pendingPhase of ['attached', 'evaluate']) {
    let fire, delay, cleared = false, release
    const pending = new Promise(resolve => { release = resolve })
    let entered
    const started = new Promise(resolve => { entered = resolve })
    const locator = {
      waitFor: async () => { if (pendingPhase === 'attached') { entered(); await pending } },
      count: async () => 1,
      evaluate: async () => { entered(); await pending; return sample },
    }
    const collection = collectProbeUxEvidence(probe, locator, 'desktop', {
      setTimer: (callback, ms) => { fire = callback; delay = ms; return 42 },
      clearTimer: id => { assert.equal(id, 42); cleared = true },
    })
    await started
    assert.equal(delay, 2000)
    fire()
    assert.deepEqual(await collection, [])
    assert.equal(cleared, true)
    release()
  }
})

test('unknown geometry and surface, journey or census probes supply no observations', async () => {
  const locator = { waitFor: async () => {}, count: async () => 1, evaluate: async () => null }
  assert.deepEqual(await collectProbeUxEvidence(probe, locator, 'phone'), [])
  for (const kind of ['surface', 'journey', 'census']) {
    assert.deepEqual(await collectProbeUxEvidence({ ...probe, kind }, null, 'desktop'), [])
  }
})
