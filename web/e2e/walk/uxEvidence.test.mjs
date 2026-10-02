import test from 'node:test'
import assert from 'node:assert/strict'
import { ID, assertionAlias, coveredAtRest, scrollNeeded, uxObservation, packUxEvidence,
  MAX_UX_OBSERVATIONS, collectControlSample } from './uxEvidence.mjs'

const sample = () => ({ rect: { x: 10, y: 20, width: 30, height: 40 },
  viewport: { width: 100, height: 100 }, hit: 'other', enabled: true, visible: true })
const input = () => ({ lensId: 'end-user', metricId: 'control_covered_at_rest', viewport: 'desktop', state: 'ready', observed: 1 })

test('coveredAtRest distinguishes obstruction, self, descendants and unknown hits', () => {
  assert.equal(coveredAtRest(sample()), 1)
  for (const hit of ['self', 'descendant']) assert.equal(coveredAtRest({ ...sample(), hit }), 0)
  for (const hit of ['none', undefined, 'invalid', null]) assert.equal(coveredAtRest({ ...sample(), hit }), null)
  for (const rect of [{ x: -40 }, { x: 100 }, { y: -50 }, { y: 100 }, { x: 85 }, { y: 80 }]) {
    assert.equal(coveredAtRest({ ...sample(), rect: { ...sample().rect, ...rect } }), null)
  }
  assert.equal(coveredAtRest({ ...sample(), rect: { x: -15, y: -20, width: 30, height: 40 } }), 1)
})

test('both metrics refuse missing, nonfinite, disabled, invisible and invalid geometry', () => {
  const unknowns = [undefined, null, {}, { ...sample(), rect: null }, { ...sample(), viewport: null }]
  for (const field of ['enabled', 'visible']) {
    for (const value of [false, undefined, null, 1, 'true']) unknowns.push({ ...sample(), [field]: value })
  }
  for (const [group, fields] of [['rect', ['x', 'y', 'width', 'height']], ['viewport', ['width', 'height']]]) {
    for (const field of fields) {
      for (const value of [undefined, null, NaN, Infinity, -Infinity, '20', true]) {
        unknowns.push({ ...sample(), [group]: { ...sample()[group], [field]: value } })
      }
      if (['width', 'height'].includes(field)) {
        for (const value of [0, -1]) unknowns.push({ ...sample(), [group]: { ...sample()[group], [field]: value } })
      }
    }
  }
  unknowns.push({ ...sample(), rect: { x: Number.MAX_VALUE, y: 0, width: Number.MAX_VALUE, height: 10 } })
  for (const candidate of unknowns) {
    assert.equal(coveredAtRest(candidate), null)
    assert.equal(scrollNeeded(candidate), null)
  }
})

test('scrollNeeded measures every viewport edge before any action and does not require a hit', () => {
  assert.equal(scrollNeeded(sample()), 0)
  assert.equal(scrollNeeded({ ...sample(), hit: undefined }), 0)
  assert.equal(scrollNeeded({ ...sample(), rect: { x: 0, y: 0, width: 100, height: 100 } }), 0)
  for (const rect of [{ x: -1 }, { y: -1 }, { x: 71 }, { y: 61 }, { x: 110 }, { y: 110 }, { x: -50 }, { y: -50 }]) {
    assert.equal(scrollNeeded({ ...sample(), rect: { ...sample().rect, ...rect } }), 1)
  }
})

test('typed observations are frozen and use safe aliases and identifiers', () => {
  const row = uxObservation(input())
  assert.deepEqual(row, { ux_version: 1, lens_id: 'end-user', metric_id: 'control_covered_at_rest',
    viewport: 'desktop', state: 'ready', observed: 1 })
  assert.ok(Object.isFrozen(row))
  assert.equal(assertionAlias(row.metric_id), 'ux-control-covered-at-rest')
  assert.equal(ID.test('1' + 'a'.repeat(63)), true)
  for (const observed of [0, null, -1, 1.5]) assert.equal(uxObservation({ ...input(), observed }).observed, observed)
  for (const field of ['lensId', 'metricId', 'viewport', 'state']) {
    for (const value of ['', 'a'.repeat(65), '.abc', '_abc', 'a.b', 'a:b', 'a b', null, undefined, 1]) {
      assert.throws(() => uxObservation({ ...input(), [field]: value }), new RegExp(field))
    }
  }
  for (const observed of [true, false, NaN, Infinity, -Infinity, undefined, '1', {}]) {
    assert.throws(() => uxObservation({ ...input(), observed }), /observed/)
  }
  for (const key of ['threshold', 'severity', 'kind', 'ux_kind', 'baseline_id', 'unexpected']) {
    assert.throws(() => uxObservation({ ...input(), [key]: null }), new RegExp(key))
  }
  assert.throws(() => uxObservation({ ...input(), [Symbol('extra')]: 1 }), /extra/)
  for (const value of [null, undefined, [], 1]) assert.throws(() => uxObservation(value), TypeError)
})

test('packing validates the exact wire shape and rejects overflow without truncation', () => {
  const row = uxObservation(input())
  const rows = Array(MAX_UX_OBSERVATIONS).fill(row)
  assert.deepEqual(packUxEvidence(rows), { ux_observations: rows })
  assert.deepEqual(packUxEvidence([]), { ux_observations: [] })
  assert.throws(() => packUxEvidence([...rows, row]), /64/)
  assert.equal(rows.length, 64)
  for (const bad of [null, {}, input(), { ...row, ux_version: 2 }, { ...row, observed: true },
    { ...row, state: 'not.safe' }, { ...row, threshold: 0 }, { ...row, [Symbol('extra')]: 1 }]) {
    assert.throws(() => packUxEvidence([bad]), TypeError)
  }
  for (const field of Object.keys(row)) {
    const bad = { ...row }; delete bad[field]
    assert.throws(() => packUxEvidence([bad]), new RegExp(field))
  }
  assert.throws(() => packUxEvidence(Array(1)), TypeError)
  assert.throws(() => packUxEvidence({}), /array/)
})

test('collector evaluates once, reads geometry and hit testing, and never acts on the control', async () => {
  for (const kind of ['self', 'descendant', 'other', 'none']) {
    const child = {}
    const other = {}
    let calls = 0
    let actions = 0
    const act = () => { actions++; throw new Error('Unexpected action') }
    const control = {
      getBoundingClientRect: () => sample().rect,
      contains: (node) => node === child,
      matches: (selector) => { assert.equal(selector, ':disabled'); return false },
      getAttribute: (name) => { assert.equal(name, 'aria-disabled'); return null },
      scrollIntoView: act, click: act, focus: act, hover: act,
      ownerDocument: {
        defaultView: { innerWidth: 100, innerHeight: 100,
          getComputedStyle: (node) => { assert.equal(node, control); return { display: 'block', visibility: 'visible' } } },
        elementFromPoint: (x, y) => {
          assert.deepEqual([x, y], [25, 40])
          return { self: control, descendant: child, other, none: null }[kind]
        },
      },
    }
    const evaluate = async (fn, handle) => { calls++; assert.equal(handle, control); return fn(handle) }
    const result = await collectControlSample(evaluate, control)
    assert.deepEqual(result, { ...sample(), hit: kind })
    assert.equal(calls, 1)
    assert.equal(actions, 0)
    control.matches = () => true
    assert.equal((await collectControlSample(evaluate, control)).enabled, false)
    control.matches = () => false
    control.getAttribute = () => 'true'
    assert.equal((await collectControlSample(evaluate, control)).enabled, false)
    for (const style of [{ display: 'none', visibility: 'visible' }, { display: 'block', visibility: 'hidden' },
      { display: 'block', visibility: 'collapse' }]) {
      control.ownerDocument.defaultView.getComputedStyle = () => style
      assert.equal((await collectControlSample(evaluate, control)).visible, false)
    }
    assert.equal(actions, 0)
  }
})
