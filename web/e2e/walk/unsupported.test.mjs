import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { HANDLED_SETUP_KINDS, ENGINE_SETUP_KINDS, unsupportedBeforeSetup,
  UnsupportedLocalError, setupStep } from './fixtures.mjs'

const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const body = (start, end) => source.slice(source.indexOf(start) + start.length,
  source.indexOf(end)).trim().slice(0, -1)
const unavailable = new AsyncFunction('probe', 'runtime', 'reason', 'UnsupportedLocalError',
  body('async function unsupported(probe, runtime, reason) {', '\nasync function engineReady'))
const runner = new AsyncFunction('probe', 'runtime', 'test', 'setupStep', 'UnsupportedLocalError',
  'unsupportedBeforeSetup', 'unsupported', 'control', 'expect', 'collectProbeUxEvidence',
  'captureBefore', 'activate', 'assertEffect',
  body('export async function runProbe(probe, runtime) {', '\n// This reporter'))
const evidenceFixture = new AsyncFunction('workerFacts', 'use', 'testInfo', source.slice(
  source.indexOf('walkEvidence: [async ({ workerFacts }, use, testInfo) => {')
    + 'walkEvidence: [async ({ workerFacts }, use, testInfo) => {'.length,
  source.indexOf('}, { auto: true }]')))
const probeFor = (steps) => ({ featureId: 'action:unit', kind: 'action', state: 'unit-state',
  certify: 'local', setup: { context: { required: true }, steps }, locator: {},
  assertion: { assertionId: 'unit/effect', kind: 'renders' } })
const engineReason = 'The production bundle has no mounted browser editing engine'

async function run(probe, facts, { precheck = unsupportedBeforeSetup, fallbackReason } = {}) {
  const log = [], attachments = []
  const testInfo = { title: 'unit', project: { name: 'desktop' }, annotations: [],
    attach: async (name, attachment) => attachments.push({ name, ...attachment }) }
  let runtime, result
  await evidenceFixture(facts, async (evidence) => {
    evidence.stack = { ready: true, instance: 'one-worker' }
    runtime = { page: {}, evidence, testInfo }
    result = await runner(probe, runtime, { step: async (_name, fn) => fn() },
      async (_probe, current, recipe) => {
        log.push(recipe.kind)
        if (fallbackReason) await unavailable(probe, current, fallbackReason, UnsupportedLocalError)
      }, UnsupportedLocalError, precheck,
      (probe, current, reason) => unavailable(probe, current, reason, UnsupportedLocalError),
      () => ({}), () => ({ toBeVisible: async () => {} }),
      async () => { log.push('ux'); return [] }, async () => ({}),
      async () => { log.push('activate') }, async () => { log.push('assert') })
  }, testInfo)
  return { result, runtime, log, attachments }
}

test('missing recipes are declared before any page work and retain the fallback reason', async () => {
  const recipe = { kind: 'require-local-state', state: 'unit-state', context: { required: true } }
  const probe = probeFor([{ kind: 'navigate', url: '/must-not-open' }, recipe])
  const expected = 'The isolated stack has no public fixture recipe for unit-state; required context {"required":true}'
  assert.equal(unsupportedBeforeSetup(probe, {}), expected)
  let fallback
  await assert.rejects(setupStep(probe, {
    page: {}, evidence: {}, testInfo: { attach: async (_name, data) => { fallback = JSON.parse(data.body) } },
  }, recipe), UnsupportedLocalError)
  assert.equal(fallback.reason, expected)
  const early = await run(probe, {})
  assert.deepEqual(early.log, [])
  assert.deepEqual(early.result, { unsupported: true, reason: expected })
  assert.equal(early.runtime.evidence.steps.length, 0)
  assert.equal(early.runtime.evidence.stack.instance, 'one-worker')
  const unknown = await run(probeFor([{ kind: 'future-unhandled-kind' }]), {})
  assert.deepEqual(unknown.log, [])
  assert.equal(unknown.result.reason, expected)
})

test('only an observed false suppresses engine setup, including helpers and boot/crash states', async () => {
  for (const kind of ENGINE_SETUP_KINDS) {
    const probe = probeFor([{ kind }])
    for (const engineMounted of [undefined, true]) {
      const facts = { engineMounted }
      assert.equal(unsupportedBeforeSetup(probe, facts), null)
      const normal = await run(probe, facts)
      assert.deepEqual(normal.log, [kind, 'ux', 'activate', 'assert'])
      assert.equal(normal.runtime.evidence.result.result, 'passed')
      assert.equal(facts.engineMounted, engineMounted)
    }
    const early = await run(probe, { engineMounted: false })
    assert.deepEqual(early.log, [])
    assert.deepEqual(early.result, { unsupported: true, reason: engineReason })
  }
  assert.equal(unsupportedBeforeSetup(probeFor([{ kind: 'select-entity', viewerOnly: true }]),
    { engineMounted: false }), null)
})

test('engineReady records its observation before declaring unavailable or awaiting parsed entities', async () => {
  const ready = new AsyncFunction('probe', 'runtime', 'unsupported', 'expect',
    body('async function engineReady(probe, runtime) {', '\nasync function createLine'))
  for (const mounted of [false, true]) {
    const facts = { engineMounted: undefined }, log = []
    const observation = ready(probeFor([]), {
      workerFacts: facts, page: { getByTestId: (id) => ({ count: async () => {
        assert.equal(id, 'cad-edit-workbench'); return mounted ? 1 : 0
      } }) },
    }, async (_probe, runtime, reason) => {
      assert.equal(runtime.workerFacts.engineMounted, false)
      assert.equal(reason, engineReason)
      log.push('unsupported')
      throw new UnsupportedLocalError(probeFor([]), reason)
    }, () => ({ toHaveText: async () => {
      assert.equal(facts.engineMounted, true); log.push('parsed')
    } }))
    if (mounted) await observation
    else await assert.rejects(observation, UnsupportedLocalError)
    assert.equal(facts.engineMounted, mounted)
    assert.equal(log[0], mounted ? 'parsed' : 'unsupported')
  }
})

test('pre-check preserves W1l result, attachments and annotation for the same probe', async () => {
  const probe = probeFor([{ kind: 'engine-ready' }])
  const early = await run(probe, { engineMounted: false })
  const old = await run(probe, { engineMounted: false }, { precheck: () => null, fallbackReason: engineReason })
  assert.deepEqual(early.result, old.result)
  assert.deepEqual(early.runtime.evidence.result, old.runtime.evidence.result)
  assert.deepEqual(early.runtime.testInfo.annotations, old.runtime.testInfo.annotations)
  const payloads = (row) => row.attachments.map(({ name, contentType, body }) => {
    const payload = JSON.parse(body)
    delete payload.startedAt
    delete payload.finishedAt
    return { name, contentType, payload }
  })
  assert.deepEqual(payloads(early), payloads(old))
  assert.deepEqual(early.attachments.map(({ name }) => name), ['walk-result', 'walk-evidence'])
  assert.equal(Object.hasOwn(early.runtime.evidence, 'failure'), false)
})

test('setup kind sets match dispatch and all transitive engine requirements', () => {
  const dispatch = source.slice(source.indexOf('export async function setupStep'), source.indexOf('async function captureBefore'))
  const cases = [...dispatch.matchAll(/case '([^']+)':/g)]
  assert.deepEqual([...HANDLED_SETUP_KINDS].sort(), cases.map((match) => match[1]).sort())
  assert.equal(Object.isFrozen(HANDLED_SETUP_KINDS), true)
  assert.equal(Object.isFrozen(ENGINE_SETUP_KINDS), true)
  const declarations = [...source.slice(0, source.indexOf('export async function setupStep'))
    .matchAll(/async function (\w+)\(/g)]
  const needsEngine = new Set(['engineReady'])
  let changed
  do {
    changed = false
    for (const [index, declaration] of declarations.entries()) {
      const text = source.slice(declaration.index, declarations[index + 1]?.index ?? source.indexOf('export const HANDLED_SETUP_KINDS'))
      if (!needsEngine.has(declaration[1]) && [...needsEngine].some((name) => text.includes(`await ${name}(`))) {
        needsEngine.add(declaration[1]); changed = true
      }
    }
  } while (changed)
  const engineKinds = cases.filter((match, index) => {
    const text = dispatch.slice(match.index, cases[index + 1]?.index ?? dispatch.length)
      .replace(/await selectEntity\([^\n]*viewerOnly: true[^\n]*/g, '')
    return [...needsEngine].some((name) => text.includes(`await ${name}(`))
      || text.includes("getByTestId('cad-edit-workbench')") || text.includes('page.workers()')
  }).map((match) => match[1])
  assert.deepEqual([...ENGINE_SETUP_KINDS].sort(), engineKinds.sort())
})
