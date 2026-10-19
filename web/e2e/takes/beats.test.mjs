import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile, readdir } from 'node:fs/promises'
import { runBeat, STEP_VERBS, validateBeat } from './runBeat.mjs'
import { verifiedFixture } from './fixtures.mjs'

const beatDir = new URL('./beats/', import.meta.url)
const beatFiles = (await readdir(beatDir)).filter((name) => name.endsWith('.json')).sort()
const sw1 = JSON.parse(await readFile(new URL('SW1.json', beatDir), 'utf8'))

test('the beat vocabulary is exactly the ten portable verbs', () => {
  assert.deepEqual([...STEP_VERBS].sort(), [
    'goto', 'click', 'fill', 'upload', 'press', 'wait_text', 'assert_text', 'scroll_to', 'hover', 'pause',
  ].sort())
  assert.ok(beatFiles.length > 0, 'at least one beat must be present')
})

const beatIds = new Set()
for (const name of beatFiles) {
  const beat = JSON.parse(await readFile(new URL(name, beatDir), 'utf8'))
  test(`${name}: validates leaf.studio-beats.v1 and declared fixture bytes`, async () => {
    validateBeat(beat)
    assert.ok(!beatIds.has(beat.beat_id), `duplicate beat_id ${beat.beat_id}`)
    beatIds.add(beat.beat_id)
    for (const fixture of beat.fixtures) {
      await verifiedFixture(fixture)
    }
  })
}

test('schema rejects executable steps, selector targets, malformed payloads and undeclared fixtures', () => {
  const invalidSteps = [
    { verb: 'evaluate', script: 'window.run()' },
    { verb: 'click', selector: '#open' },
    { verb: 'click', testid: 'open', text: 'Open' },
    { verb: 'click', role: 'button' },
    { verb: 'hover', target: { css: '.open' } },
    { verb: 'scroll_to', target: { text: 'Open', script: 'run()' } },
    { verb: 'fill', target: { text: 'Name' }, value: 123 },
    { verb: 'upload', target: { testid: 'file' }, fixture: '../outside.dxf' },
    { verb: 'press', key: '' },
    { verb: 'pause', ms: -1 },
    { verb: 'pause', ms: 1.5 },
    { verb: 'wait_text', text: 'Ready', timeout_ms: 0 },
    { verb: 'assert_text', text: '' },
    { verb: 'goto', path: '//another-origin.invalid' },
    { verb: 'goto', path: '/\\another-origin.invalid' },
    { verb: 'goto', path: 'https://another-origin.invalid' },
    null,
  ]
  for (const step of invalidSteps) {
    assert.throws(() => validateBeat({ ...sw1, steps: [step] }), undefined, JSON.stringify(step))
  }
  for (const change of [
    { schema: 'leaf.studio-beats.v2' }, { env: 'production' }, { origin: 'https://other.invalid' },
    { requires_signed_in: 'false' }, { steps: [] }, { expect_text: ['unasserted'] }, { script: 'run()' },
    { fixtures: [{ ...sw1.fixtures[0], path: '/absolute.dxf' }] },
    { fixtures: [{ ...sw1.fixtures[0], path: 'web/../sample.dxf' }] },
    { fixtures: [{ ...sw1.fixtures[0], sha256: 'bad' }] },
    { fixtures: [{ ...sw1.fixtures[0], bytes: -1 }] },
  ]) assert.throws(() => validateBeat({ ...sw1, ...change }))
})

test('all target forms and all verbs have valid portable representations', () => {
  const steps = [
    { verb: 'goto', path: '/try?demo=1' },
    ...[{ testid: 'open' }, { role: 'button', name: 'Open' }, { text: 'Open' }]
      .map((target) => ({ verb: 'click', ...target })),
    { verb: 'fill', target: { role: 'textbox', name: 'Name' }, value: '' },
    { verb: 'upload', target: { text: 'DXF file' }, fixture: sw1.fixtures[0].path },
    { verb: 'press', key: 'Control+Enter' },
    { verb: 'wait_text', text: 'Ready', timeout_ms: 1000 },
    { verb: 'assert_text', text: 'Ready' },
    { verb: 'scroll_to', target: { testid: 'properties-dock' } },
    { verb: 'hover', target: { text: 'Open' } },
    { verb: 'pause', ms: 0 },
  ]
  validateBeat({ ...sw1, steps, expect_text: ['Ready'] })
})

test('an unknown late verb fails before even reading the browser width', async () => {
  const page = new Proxy({}, { get() { assert.fail('browser must not be touched before preflight completes') } })
  await assert.rejects(runBeat(page, { ...sw1, steps: [...sw1.steps, { verb: 'evaluate' }] }), /unknown verb/)
})

test('runner refuses wrong fixture hashes, byte counts and parent paths before upload', async () => {
  for (const [change, reason] of [
    [{ sha256: '0'.repeat(64) }, /steps\[1\] \(upload\) failed: .*SHA-256/],
    [{ bytes: sw1.fixtures[0].bytes + 1 }, /steps\[1\] \(upload\) failed: .*byte count/],
    [{ path: '../outside.dxf' }, /steps\[1\] fixture.path/],
  ]) {
    const fixture = { ...sw1.fixtures[0], ...change }
    const beat = { ...sw1, fixtures: [fixture], steps: [
      { verb: 'pause', ms: 0 },
      { verb: 'upload', target: { text: 'DXF file' }, fixture: fixture.path },
    ], expect_text: [] }
    const page = {
      evaluate: async () => 1600,
      waitForTimeout: async () => {},
      getByLabel: () => assert.fail('unverified fixture must not reach the file input'),
    }
    await assert.rejects(runBeat(page, beat), reason)
  }
})

test('runner uploads the verified fixture bytes with the declared filename', async () => {
  const fixture = sw1.fixtures[0]
  const expected = await verifiedFixture(fixture)
  let uploads = 0
  const page = {
    evaluate: async () => 1600,
    getByLabel: (text) => {
      assert.equal(text, 'DXF file')
      return { setInputFiles: async (payload) => {
        assert.deepEqual(payload, expected)
        uploads += 1
      } }
    },
  }
  await runBeat(page, { ...sw1, steps: [
    { verb: 'upload', target: { text: 'DXF file' }, fixture: fixture.path },
  ], expect_text: [] })
  assert.equal(uploads, 1)
})

test('a failed step reports its index and verb, preserves its cause and stops playback', async () => {
  const failure = new Error('control unavailable')
  const beat = { ...sw1, steps: [
    { verb: 'pause', ms: 0 },
    { verb: 'click', role: 'button', name: 'Open a DXF in the browser engine' },
    { verb: 'press', key: 'Enter' },
  ], expect_text: [] }
  const page = {
    evaluate: async () => 1600,
    waitForTimeout: async () => {},
    getByRole: () => ({ click: async () => { throw failure } }),
    keyboard: { press: async () => assert.fail('playback must stop at the failed step') },
  }
  await assert.rejects(runBeat(page, beat), (error) => {
    assert.match(error.message, /Beat SW1 steps\[1\] \(click\) failed: control unavailable/)
    assert.equal(error.cause, failure)
    return true
  })
})

test('runner refuses narrow viewports and records measured width, monotonic times and sync ID', async () => {
  const beat = { ...sw1, steps: [{ verb: 'pause', ms: 0 }], expect_text: [] }
  await assert.rejects(runBeat({ evaluate: async () => 980 }, beat), /innerWidth >= 981/)
  const widths = [1600, 1200]
  const page = { evaluate: async () => widths.shift(), waitForTimeout: async () => {} }
  const sync_id = 'split-plan-42'
  const record = await runBeat(page, beat, { sync_id })
  assert.deepEqual(Object.keys(record).sort(), [
    'beat_id', 'sync_id', 't_start_ms', 't_end_ms', 'expect_text', 'inner_width',
  ].sort())
  assert.equal(record.beat_id, 'SW1')
  assert.equal(record.sync_id, sync_id)
  assert.equal(record.inner_width, 1200)
  assert.ok(Number.isFinite(record.t_start_ms) && record.t_start_ms > 0)
  assert.ok(record.t_end_ms >= record.t_start_ms)
  assert.deepEqual(record.expect_text, [])
  const defaultRecord = await runBeat({ evaluate: async () => 981, waitForTimeout: async () => {} }, beat)
  assert.equal(defaultRecord.sync_id, null)
  let reads = 0
  await assert.rejects(runBeat({ evaluate: async () => ++reads === 1 ? 1600 : 980,
    waitForTimeout: async () => {} }, beat), /innerWidth >= 981/)
})
