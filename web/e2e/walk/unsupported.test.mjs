import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { HANDLED_SETUP_KINDS, ENGINE_SETUP_KINDS, unsupportedBeforeSetup,
  UnsupportedLocalError, setupStep, TOOL_ARM_EFFECT_KINDS, workerCatalog,
  toolAvailabilityEvidence, catalogSolarNeedsDrawing, readWalkBuildFlags, CATALOG_SOLAR_FLAG_REASON } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const body = (start, end) => source.slice(source.indexOf(start) + start.length,
  source.indexOf(end)).trim().slice(0, -1)
const unavailable = new AsyncFunction('probe', 'runtime', 'reason', 'UnsupportedLocalError',
  body('async function unsupported(probe, runtime, reason) {', '\nasync function engineReady'))
const runner = new AsyncFunction('probe', 'runtime', 'test', 'setupStep', 'UnsupportedLocalError',
  'unsupportedBeforeSetup', 'unsupported', 'control', 'expect', 'collectProbeUxEvidence',
  'captureBefore', 'activate', 'assertEffect', 'workerCatalog', 'toolAvailabilityEvidence', 'readWalkBuildFlags',
  body('export async function runProbe(probe, runtime) {', '\n// This reporter'))
const evidenceFixture = new AsyncFunction('workerFacts', 'use', 'testInfo', source.slice(
  source.indexOf('walkEvidence: [async ({ workerFacts }, use, testInfo) => {')
    + 'walkEvidence: [async ({ workerFacts }, use, testInfo) => {'.length,
  source.indexOf('}, { auto: true }]')))
const probeFor = (steps) => ({ featureId: 'action:unit', kind: 'action', state: 'unit-state',
  certify: 'local', setup: { context: { required: true }, steps }, locator: {},
  assertion: { assertionId: 'unit/effect', kind: 'renders' } })
const engineReason = 'The production bundle has no mounted browser editing engine'

test('catalog Solar flag declarations require an effect and exact drawing-context evidence', async () => {
  const entries = buildFeatureMap().entries.filter((entry) => entry.kind === 'tool' && entry.source_id.startsWith('solar-'))
  const drawingAvailability = { entitled: true, engine_ready: true, input_ready: false, implemented: true,
    refusal_reasons: ['drawing_context_required'] }
  const catalog = { families: [{ capabilities: [
    { name: 'solar-settings', availability: drawingAvailability },
    { name: 'solar-solve-proposal', availability: { ...drawingAvailability, input_ready: true, refusal_reasons: [] } },
  ] }] }
  for (const entry of entries) {
    for (const state of entry.states) {
      const probe = resolveProbe(entry, state)
      const refusal = probe.assertion.kind === 'disabled_with_reason'
      assert.equal(catalogSolarNeedsDrawing(probe), !refusal)
      if (refusal) assert.equal(unsupportedBeforeSetup(probe, { catalog, buildFlags: {} }), null)
    }
  }
  const probe = resolveProbe(entries.find((entry) => entry.source_id === 'solar-settings'), 'ready')
  const solve = resolveProbe(entries.find((entry) => entry.source_id === 'solar-solve-proposal'), 'ready')
  assert.equal(unsupportedBeforeSetup(solve, { catalog, buildFlags: {} }), null)
  for (const unknown of [undefined, { families: [] }, { families: [{ capabilities: [{ name: 'solar-settings' }] }] }]) {
    assert.equal(unsupportedBeforeSetup(probe, { catalog: unknown, buildFlags: {} }), null)
  }
  for (const flag of [undefined, '0', '1']) {
    const flags = flag === undefined ? {} : { VITE_SOLAR_SETTINGS_FORM: flag }
    let fetches = 0
    const runtime = { workerFacts: {}, evidence: { steps: [] }, testInfo: { attach: async () => {} },
      page: { request: { get: async (path) => {
        fetches++
        assert.ok(path === '/.leaf-walk-build.json' || path === '/api/capabilities')
        return { ok: () => true, json: async () => path === '/.leaf-walk-build.json' ? { flags } : catalog }
      } } } }
    await Promise.all([readWalkBuildFlags(probe, runtime), readWalkBuildFlags(probe, runtime)])
    await readWalkBuildFlags(probe, { ...runtime, evidence: {} })
    assert.equal(fetches, 2)
    assert.deepEqual(runtime.workerFacts.buildFlags, flags)
    assert.deepEqual(runtime.workerFacts.catalog, catalog)
    assert.equal(runtime.evidence.result, undefined)
    assert.equal(unsupportedBeforeSetup(probe, runtime.workerFacts, false), flag === '1' ? null : CATALOG_SOLAR_FLAG_REASON)
    assert.notEqual(unsupportedBeforeSetup(probe, { catalog, buildFlags: { VITE_SOLAR_SETTINGS_FORM: '1' } }), CATALOG_SOLAR_FLAG_REASON)
    assert.deepEqual(runtime.evidence.buildFlags, flags)
    assert.deepEqual(runtime.evidence.steps, [])
    if (flag !== '1') {
      const early = await run(probe, {}, { readBuild: true, request: runtime.page.request })
      assert.deepEqual(early.log, [])
      assert.deepEqual(early.result, { unsupported: true, reason: CATALOG_SOLAR_FLAG_REASON })
      assert.deepEqual(early.runtime.evidence.result.flags, flags)
      assert.deepEqual(early.runtime.evidence.result.refusal_codes, ['drawing_context_required'])
      assert.equal(early.runtime.evidence.oracleReached, undefined)
      assert.equal(early.runtime.evidence.cleanupCompleted, true)
    } else {
      let catalogs = 0
      const normal = await run(probe, {}, { readBuild: true, request: { get: async (path) => {
        assert.ok(path === '/.leaf-walk-build.json' || path === '/api/capabilities')
        return { ok: () => true, json: async () => path === '/.leaf-walk-build.json'
          ? { flags } : ++catalogs === 1 ? catalog : { families: [] } }
      } } })
      assert.deepEqual(normal.log, [...probe.setup.steps.map((step) => step.kind), 'ux', 'activate', 'assert'])
      assert.equal(normal.runtime.evidence.result.result, 'passed')
      assert.equal(normal.runtime.evidence.oracleReached, probe.assertion.assertionId)
    }
  }
  const refusal = { ...probe, assertion: { kind: 'disabled_with_reason', reason_code: 'drawing_context_required' } }
  assert.equal(catalogSolarNeedsDrawing(refusal), false)
  await readWalkBuildFlags(refusal, { page: {} })
  await readWalkBuildFlags(probeFor([]), { page: {} })
  assert.ok(source.indexOf('export async function readWalkBuildFlags') < source.indexOf('async function baselineThreeState'))
  assert.ok(source.indexOf('await readWalkBuildFlags(probe, runtime)') < source.indexOf('for (const recipe of probe.setup.steps)', source.indexOf('export async function runProbe')))
})
async function run(probe, facts, { precheck = unsupportedBeforeSetup, fallbackReason, request, readBuild = false } = {}) {
  const log = [], attachments = []
  const testInfo = { title: 'unit', project: { name: 'desktop' }, annotations: [],
    attach: async (name, attachment) => attachments.push({ name, ...attachment }) }
  let runtime, result
  await evidenceFixture(facts, async (evidence) => {
    evidence.stack = { ready: true, instance: 'one-worker' }
    runtime = { page: { request }, evidence, testInfo }
    result = await runner(probe, runtime, { step: async (_name, fn) => fn() },
      async (_probe, current, recipe) => {
        log.push(recipe.kind)
        if (fallbackReason) await unavailable(probe, current, fallbackReason, UnsupportedLocalError)
      }, UnsupportedLocalError, precheck,
      (probe, current, reason) => unavailable(probe, current, reason, UnsupportedLocalError),
      () => ({}), () => ({ toBeVisible: async () => {} }),
      async () => { log.push('ux'); return [] }, async () => ({}),
      async () => { log.push('activate') }, async () => { log.push('assert') },
      workerCatalog, toolAvailabilityEvidence, readBuild ? readWalkBuildFlags : undefined)
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

test('Repeat refusal rows still activate Enter before reaching their keyboard oracle', async () => {
  const entry = buildFeatureMap().entries.find((row) => row.id === 'action:engine-repeat')
  for (const state of entry.states) {
    const probe = resolveProbe(entry, state)
    const normal = await run(probe, {})
    assert.deepEqual(normal.log, [...probe.setup.steps.map((step) => step.kind), 'ux', 'activate', 'assert'])
    assert.equal(normal.runtime.evidence.oracleReached, probe.assertion.assertionId)
    assert.equal(normal.runtime.evidence.result.result, 'passed')
    assert.equal(normal.runtime.evidence.cleanupCompleted, true)
  }
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
  for (const kind of ['saved-version-history', 'require-versionless-drawing', 'seed-solar-graph']) {
    assert.equal(HANDLED_SETUP_KINDS.has(kind), true)
    assert.equal(ENGINE_SETUP_KINDS.has(kind), false)
  }
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
  // Solar surface readiness checks its engine during setup; browser and CAD
  // contexts must not acquire an unconditional engine pre-check from it.
  assert.equal(ENGINE_SETUP_KINDS.has('require-surface-context'), false)
  const engineKinds = cases.filter((match, index) => {
    const text = dispatch.slice(match.index, cases[index + 1]?.index ?? dispatch.length)
      .replace(/await selectEntity\([^\n]*viewerOnly: true[^\n]*/g, '')
      .replace(/if \(recipe\.surface === 'solar'[\s\S]*?\n      }/g, '')
    return [...needsEngine].some((name) => text.includes(`await ${name}(`))
      || text.includes("getByTestId('cad-edit-workbench')") || text.includes('page.workers()')
  }).map((match) => match[1])
  assert.deepEqual([...ENGINE_SETUP_KINDS].sort(), engineKinds.sort())
})

const solarEntry = buildFeatureMap().entries.find((entry) => entry.id === 'tool:solar-correct-string')
const solarProbe = (state = 'ready') => resolveProbe(solarEntry, state)
const readyAvailability = { entitled: true, engine_ready: true, input_ready: true, implemented: true }
const catalogFor = (availability) => ({ families: [{ capabilities: [
  { name: 'solar-correct-string', ...(availability === undefined ? {} : { availability }) },
] }] })
const requestFor = (catalog) => ({ get: async (path) => {
  assert.equal(path, '/api/capabilities')
  return { ok: () => true, json: async () => catalog }
} })

test('catalog input readiness declares a run decision after drawing and graph setup with availability evidence', async () => {
  const catalog = catalogFor({ ...readyAvailability, input_ready: false,
    refusal_reasons: ['drawing_context_required', 'solar_profile_required'] })
  const probe = solarProbe()
  const log = [], attachments = []
  const testInfo = { title: 'unit', project: { name: 'desktop' }, annotations: [],
    attach: async (name, attachment) => attachments.push({ name, ...attachment }) }
  let runtime, result
  await evidenceFixture({ catalog: catalogFor({ ...readyAvailability, input_ready: false }) }, async (evidence) => {
    runtime = { page: { request: { get: async (path) => {
      assert.deepEqual(log, probe.setup.steps.map((step) => step.kind))
      assert.equal(path, '/api/capabilities?drawing_id=unit.drawing&drawing_version=2')
      log.push('catalog')
      return { ok: () => true, json: async () => catalog }
    } } }, evidence, testInfo }
    result = await runner(probe, runtime, { step: async (_name, fn) => fn() },
      async (_probe, current, recipe) => {
        log.push(recipe.kind)
        if (recipe.kind === 'open-private-drawing') current.drawingId = 'unit.drawing'
        if (recipe.kind === 'seed-solar-graph') {
          assert.equal(current.drawingId, 'unit.drawing')
          current.drawingVersion = 2
        }
      }, UnsupportedLocalError, unsupportedBeforeSetup,
      (probe, current, reason) => unavailable(probe, current, reason, UnsupportedLocalError),
      () => { throw new Error('control reached before refusal') }, () => ({}),
      async () => [], async () => ({}), async () => {}, async () => {},
      workerCatalog, toolAvailabilityEvidence)
  }, testInfo)
  const early = { result, runtime, log, attachments }
  const reason = 'The isolated stack cannot run solar-correct-string: input_ready is false (drawing_context_required,solar_profile_required)'
  assert.equal(probe.setup.steps.some((step) => step.kind === 'open-private-drawing'), true)
  assert.equal(probe.setup.steps.some((step) => step.kind === 'seed-solar-graph'), true)
  assert.deepEqual(early.log, [...probe.setup.steps.map((step) => step.kind), 'catalog'])
  assert.deepEqual(early.result, { unsupported: true, reason })
  assert.deepEqual(early.runtime.testInfo.annotations, [{ type: 'unsupported_local', description: reason }])
  assert.deepEqual(early.runtime.evidence.result, { featureId: solarEntry.id, state: 'ready',
    result: 'unsupported_local', declaredCertify: solarProbe().certify, reason,
    tool: 'solar-correct-string', availability_fields_false: ['input_ready'],
    refusal_codes: ['drawing_context_required', 'solar_profile_required'] })
  assert.deepEqual(JSON.parse(early.attachments[0].body).availability_fields_false, ['input_ready'])
  assert.deepEqual(early.runtime.evidence.steps, probe.setup.steps.map((step) => ({ phase: 'setup', ...step })))
  assert.equal(typeof early.runtime.evidence.setupCompleted.elapsedMs, 'number')
})

test('catalog availability leaves gate probes, ready tools and unknown facts on the normal path', async () => {
  for (const [probe, catalog] of [
    [solarProbe('write-locked'), catalogFor({ ...readyAvailability, input_ready: false })],
    [solarProbe(), catalogFor(readyAvailability)],
    [solarProbe(), catalogFor(undefined)],
    [solarProbe(), { families: [] }],
  ]) {
    const normal = await run(probe, {}, { request: requestFor(catalog) })
    assert.deepEqual(normal.log, [...probe.setup.steps.map((step) => step.kind), 'ux',
      ...(probe.assertion.kind === 'disabled_with_reason' ? [] : ['activate']), 'assert'])
    assert.equal(normal.runtime.evidence.result.result, 'passed')
    assert.deepEqual(normal.runtime.testInfo.annotations, [])
  }
})

test('catalog fetch shares concurrent drawing/version callers, refreshes readiness and isolates failures', async () => {
  for (const failure of [false, true]) {
    const facts = {}, calls = [], request = { get: async (path) => {
      calls.push(path)
      if (failure) throw new Error('catalog offline')
      return { ok: () => true, json: async () => catalogFor(readyAvailability) }
    } }
    for (const scope of [{ drawingId: 'first.drawing', version: 1 },
      { drawingId: 'first.drawing', version: 2 }, { drawingId: 'second.drawing', version: 2 }]) {
      const before = calls.length
      const answers = await Promise.all([workerCatalog(facts, request, scope), workerCatalog(facts, request, scope)])
      assert.equal(calls.length, before + 1)
      assert.equal(calls.at(-1), `/api/capabilities?drawing_id=${scope.drawingId}&drawing_version=${scope.version}`)
      assert.deepEqual(answers, failure ? [undefined, undefined]
        : [catalogFor(readyAvailability), catalogFor(readyAvailability)])
      if (failure) {
        assert.equal(facts.catalog, undefined)
        assert.equal(facts.catalogError.message, 'catalog offline')
      } else {
        assert.equal(unsupportedBeforeSetup(solarProbe(), facts), null)
        assert.equal(facts.catalogError, undefined)
      }
    }
    await workerCatalog(facts, request, { drawingId: 'first.drawing', version: 1 })
    assert.equal(calls.length, 4)
    if (failure) await assert.rejects(run(solarProbe(), {}, { request }), /catalog offline/)
    else {
      const normal = await run(solarProbe(), {}, { request })
      assert.equal(normal.runtime.evidence.result.result, 'passed')
    }
  }
  const facts = {}, scope = { drawingId: 'changing.drawing', version: 'head' }
  let inputReady = false
  const request = { get: async () => ({ ok: () => true,
    json: async () => catalogFor({ ...readyAvailability, input_ready: inputReady }) }) }
  await workerCatalog(facts, request, scope)
  assert.deepEqual(toolAvailabilityEvidence(solarProbe(), facts).availability_fields_false, ['input_ready'])
  inputReady = true
  await workerCatalog(facts, request, scope)
  assert.equal(toolAvailabilityEvidence(solarProbe(), facts), null)
  await workerCatalog(facts, { get: async (path) => {
    assert.equal(path, '/api/capabilities?drawing_id=other.drawing&drawing_version=head')
    return { ok: () => true, json: async () => catalogFor({ ...readyAvailability, input_ready: false }) }
  } }, { drawingId: 'other.drawing' })
  assert.deepEqual(toolAvailabilityEvidence(solarProbe(), facts).availability_fields_false, ['input_ready'])
  await workerCatalog(facts, request, scope)
  assert.equal(toolAvailabilityEvidence(solarProbe(), facts), null)
})

test('the frozen tool arm effect split follows the registry and excludes every refusal gate', () => {
  assert.equal(Object.isFrozen(TOOL_ARM_EFFECT_KINDS), true)
  assert.deepEqual([...TOOL_ARM_EFFECT_KINDS], ['opens'])
  const map = buildFeatureMap()
  const kinds = new Set()
  const armTargets = new Set(['catalog-run-decision', 'solar-step-editor'])
  for (const entry of map.entries.filter((entry) => entry.kind === 'tool')) {
    for (const state of entry.states) {
      const probe = resolveProbe(entry, state)
      if (armTargets.has(probe.assertion.target)) kinds.add(probe.assertion.kind)
      else assert.equal(TOOL_ARM_EFFECT_KINDS.has(probe.assertion.kind), false)
    }
  }
  assert.deepEqual([...kinds].sort(), [...TOOL_ARM_EFFECT_KINDS].sort())
  const facts = { catalog: catalogFor({ entitled: false, engine_ready: false,
    input_ready: false, implemented: false }) }
  assert.deepEqual(toolAvailabilityEvidence(solarProbe(), facts).availability_fields_false,
    ['entitled', 'engine_ready', 'input_ready', 'implemented'])
  assert.equal(unsupportedBeforeSetup(solarProbe(), facts),
    'The isolated stack cannot run solar-correct-string: entitled is false')
  for (const state of ['write-locked', 'write-unentitled', 'read-only', 'job-running', 'unsaved-engine-edits']) {
    assert.equal(toolAvailabilityEvidence(solarProbe(state), facts), null)
  }
})
