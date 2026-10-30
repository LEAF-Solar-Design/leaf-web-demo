import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { collectProbeCases, declareProbes } from './collection.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe, PROJECT_WORKSPACE_PROBES } from './probes.mjs'

test('probe cases partition exact PostgreSQL options and preserve declaration order', () => {
  const probes = [
    {}, { workerOptions: { walkPostgres: true } }, { workerOptions: {} },
    { workerOptions: { walkPostgres: true } }, { workerOptions: null }, {},
  ]
  const entries = probes.map((_probe, index) => ({ id: `action:${index}`, states: ['ready'] }))
  const cases = collectProbeCases(entries, (entry) => probes[entries.indexOf(entry)])
  const item = (index) => ({ entry: entries[index], state: 'ready', probe: probes[index] })
  assert.deepEqual(cases, {
    ordinary: [item(0), item(2), item(4), item(5)], postgres: [item(1), item(3)],
  })
  assert.deepEqual(collectProbeCases([], () => assert.fail('no probe to resolve')), { ordinary: [], postgres: [] })
})

test('unsupported worker option sets fail collection loudly', () => {
  for (const workerOptions of [
    { walkPostgres: false }, { walkPostgres: 'true' }, { walkPostgres: true, extra: true },
    { extra: true }, { walkPostgres: { enabled: true } },
  ]) {
    assert.throws(() => collectProbeCases([{ id: 'action:unit', states: ['ready'] }],
      () => ({ workerOptions })), /Unsupported worker options for action:unit \[ready\].*dedicated top-level spec/)
  }
})

test('project Solar live recipes receive enough time for extraction and publication', async () => {
  const entry = buildFeatureMap().entries.find((row) => row.id === 'surface:solar')
  const cases = entry.states.filter((state) => state.startsWith('project-solar-'))
    .map((state) => ({ entry, state, probe: resolveProbe(entry, state) }))
  const rows = []
  declareProbes((title, _options, callback) => rows.push({ title, callback }), async () => {}, cases)
  for (const row of rows) {
    const timeouts = []
    await row.callback({ page: {}, stack: {}, walkEvidence: {} }, { setTimeout: (value) => timeouts.push(value) })
    assert.deepEqual(timeouts, row.title.includes('project-solar-proposal-offline') ? [] : [300_000])
  }
})

test('feature spec files apply PostgreSQL options at top level and share declarations', async () => {
  const entries = [
    { id: 'action:fit', states: ['ready', 'project-open'], viewports: ['desktop'],
      state_viewports: { 'project-open': ['phone'] } },
    { id: 'surface:sheets', states: ['ready', 'project-open'], viewports: ['desktop', 'phone'] },
    { id: 'action:other', states: ['ready', 'project-open'], viewports: ['desktop'] },
  ]
  const certification = { result: 'excluded', reason: 'declared exclusion' }
  const probeFor = (entry, state) => ({ featureId: entry.id, state,
    ...(state === 'project-open' ? { workerOptions: { walkPostgres: true } } : {}),
    ...(entry.id === 'surface:sheets' ? { certification } : {}) })
  const rows = [], runs = [], uses = []
  for (const file of ['features.spec.mjs', 'features-postgres.spec.mjs']) {
    let workerOptions
    const declare = (title, options, callback) => rows.push({ title, options, callback, file, workerOptions })
    declare.describe = () => assert.fail('feature probes must be declared at top level')
    declare.use = (options) => {
      assert.equal(rows.some((row) => row.file === file), false, 'apply options before declaring tests')
      workerOptions = options
      uses.push({ file, options })
    }
    declare.fail = (condition, reason) => assert.deepEqual([condition, reason], [true, certification.reason])
    const source = readFileSync(new URL(`./${file}`, import.meta.url), 'utf8')
    const collect = new Function('test', 'runProbe', 'buildFeatureMap', 'resolveProbe', 'collectProbeCases', 'declareProbes',
      source.replace(/^import .*\n/gm, ''))
    collect(declare, async (...args) => runs.push(args), () => ({ entries }), probeFor, collectProbeCases, declareProbes)
  }
  assert.deepEqual(uses, [{ file: 'features-postgres.spec.mjs', options: { walkPostgres: true } }])
  assert.deepEqual(rows.map((row) => row.title), [
    'action:fit [ready] @desktop', 'surface:sheets [ready] @desktop', 'surface:sheets [ready] @phone',
    'action:other [ready] @desktop', 'action:fit [project-open] @phone',
    'surface:sheets [project-open] @desktop', 'surface:sheets [project-open] @phone',
    'action:other [project-open] @desktop',
  ])
  for (const row of rows) {
    const postgres = row.title.includes('[project-open]')
    assert.equal(row.file, postgres ? 'features-postgres.spec.mjs' : 'features.spec.mjs')
    assert.deepEqual(row.workerOptions, postgres ? { walkPostgres: true } : undefined)
    assert.deepEqual(row.options, { tag: row.title.startsWith('action:other') ? [] : ['@smoke'] })
  }
  const runtime = { page: {}, stack: {}, walkEvidence: {} }
  const testInfo = { annotations: [], attach: async () => {} }
  for (const row of rows.filter((row) => row.title.startsWith('action:'))) {
    await row.callback(runtime, testInfo)
    const entry = entries.find((entry) => row.title.startsWith(`${entry.id} `))
    const state = row.title.includes('[project-open]') ? 'project-open' : 'ready'
    assert.deepEqual(runs.at(-1), [probeFor(entry, state), {
      page: runtime.page, stack: runtime.stack, evidence: runtime.walkEvidence, testInfo,
    }])
  }
  assert.equal(runs.length, 4)
  for (const state of ['ready', 'project-open']) {
    const walkEvidence = {}, attachments = []
    const info = { annotations: [], attach: async (name, attachment) => attachments.push({ name, ...attachment }) }
    await assert.rejects(rows.find((row) => row.title === `surface:sheets [${state}] @desktop`)
      .callback({ walkEvidence }, info), /CERTIFICATION_CLASS: excluded: declared exclusion/)
    const result = { featureId: 'surface:sheets', state, ...certification }
    assert.deepEqual(walkEvidence.certification, result)
    assert.deepEqual(info.annotations, [{ type: 'certification', description: certification.result }])
    assert.equal(attachments.length, 1)
    assert.equal(attachments[0].name, 'walk-certification')
    assert.equal(attachments[0].contentType, 'application/json')
    assert.deepEqual(JSON.parse(attachments[0].body.toString()), result)
  }
  assert.equal(runs.length, 4, 'certifications must not run a probe')
})

test('the feature map assigns every project probe to the PostgreSQL spec exactly once', () => {
  const entries = buildFeatureMap().entries
  const cases = collectProbeCases(entries, resolveProbe)
  const solar = ['project-solar-open', 'project-solar-published', 'project-solar-form-switch', 'project-solar-proposal-offline']
  assert.deepEqual(cases.postgres.filter(({ entry }) => entry.id === 'surface:solar').map(({ state }) => state).sort(), solar.sort())
  assert.ok(cases.ordinary.some(({ entry, state }) => entry.id === 'surface:solar' && state === 'ready'))
  assert.ok(!cases.ordinary.some(({ state }) => solar.includes(state)))
  assert.ok(cases.postgres.length > 0)
  assert.equal(cases.postgres.filter(({ entry, state }) => entry.id === 'tab:project:tools' && state === 'ready').length, 1)
  for (const tab of ['activity', 'project']) {
    assert.ok(cases.ordinary.some(({ entry, state }) => entry.id === `tab:project:${tab}` && state === 'ready'))
  }
  const all = [...cases.ordinary, ...cases.postgres]
  assert.equal(all.length, entries.reduce((count, entry) => count + entry.states.length, 0))
  assert.equal(new Set(all.map(({ entry, state }) => JSON.stringify([entry.id, state]))).size, all.length)
  for (const { entry, state, probe } of all) {
    const postgres = cases.postgres.some((item) => item.probe === probe)
    assert.equal(postgres, Boolean(entry.state_contexts?.[state]?.openProjectId)
      || PROJECT_WORKSPACE_PROBES.includes(`${entry.id}/${state}`))
    assert.deepEqual(probe.workerOptions, postgres ? { walkPostgres: true } : undefined)
  }
})
