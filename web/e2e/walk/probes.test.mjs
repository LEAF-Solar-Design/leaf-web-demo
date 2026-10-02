import assert from 'node:assert/strict'
import test from 'node:test'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { ACTIONS, accessibleName } from '../../src/lib/actionRegistry.js'
import { effectAssertion, resolveProbe } from './probes.mjs'

const map = buildFeatureMap()

for (const entry of map.entries) {
  for (const state of entry.states) {
    test(`${entry.id} [${state}] resolves a registry locator, state recipe and effect assertion`, () => {
      const before = JSON.stringify(entry)
      const probe = resolveProbe(entry, state)
      assert.equal(probe.featureId, entry.id)
      assert.equal(probe.state, state)
      assert.ok(probe.locator.role)
      assert.ok(typeof probe.locator.name === 'string' || probe.locator.name instanceof RegExp)
      assert.ok(probe.locator.trigger)
      assert.ok(probe.setup.steps.length)
      assert.ok(probe.setup.steps.every((step) => typeof step.kind === 'string' && step.kind.length))
      assert.equal(probe.assertion.kind, entry.expected_effect[state].kind)
      assert.equal(probe.assertion.assertionId, `${entry.id}/${state}/${entry.expected_effect[state].kind}`)
      assert.equal(JSON.stringify(entry), before, 'mapping must not mutate its registry input')
      if (probe.assertion.kind === 'disabled_with_reason') {
        assert.equal(probe.assertion.reason, entry.expected_effect[state].reason)
        assert.equal(probe.assertion.reason_code, entry.expected_effect[state].reason_code)
        if (entry.kind === 'action') {
          const action = ACTIONS.find((action) => action.id === entry.source_id)
          if (!['bar', 'slash'].includes(action.surface)) {
            assert.equal(probe.locator.name, accessibleName(probe.locator.role === 'combobox' ? action.text : action.label, probe.assertion.reason))
          }
        }
      }
      if (['unsupported_local', 'staging'].includes(entry.certify)) {
        assert.deepEqual(probe.certification, { result: entry.certify, reason: entry.certify_reason })
      } else assert.equal(probe.certification, null)
    })
  }
}

test('unknown effect kinds and missing effects throw instead of becoming a visibility check', () => {
  const entry = structuredClone(map.entries[0])
  const state = entry.states[0]
  for (const effect of [{ kind: 'unknown', target: 'somewhere' }, undefined]) {
    entry.expected_effect[state] = effect
    assert.throws(() => resolveProbe(entry, state), /Unknown effect kind/)
  }
})

test('unknown states and incomplete effect contracts fail closed', () => {
  const entry = structuredClone(map.entries[0])
  assert.throws(() => resolveProbe(entry, 'not-a-state'), /Unknown state/)
  const state = entry.states[0]
  entry.expected_effect[state] = { kind: 'disabled_with_reason', reason: 'why' }
  assert.throws(() => effectAssertion(entry, state), /user-facing reason/)
  entry.expected_effect[state] = { kind: 'opens' }
  assert.throws(() => effectAssertion(entry, state), /needs a target/)
})

test('nav uses the visible desktop rail disclosure and its phone toggle', () => {
  const entry = map.entries.find((entry) => entry.id === 'drawer:nav')
  for (const state of ['closed', 'open']) {
    const probe = resolveProbe(entry, state)
    assert.equal(probe.locator.role, 'button')
    assert.equal(probe.locator.name, state === 'open' ? 'Collapse the tool rail to a spine' : 'Tool rail')
    assert.equal(probe.locator.phone.name, 'Tool rail')
    assert.equal(probe.locator.trigger, 'click')
    assert.deepEqual(probe.setup.steps.at(-1), { kind: 'tool-rail-state', name: 'Catalog', open: state === 'open' })
  }
})

test('the feature map excludes Fit no-drawing and keeps Fit ready', () => {
  const entry = map.entries.find((entry) => entry.id === 'action:fit')
  assert.ok(entry)
  assert.equal(entry.states.includes('no-drawing'), false)
  assert.equal(Object.hasOwn(entry.expected_effect, 'no-drawing'), false)
  assert.equal(entry.states.includes('ready'), true)
  assert.equal(resolveProbe(entry, 'ready').state, 'ready')
  assert.throws(() => resolveProbe(entry, 'no-drawing'), /Unknown state no-drawing for action:fit/)
})

test('Fit ready changes the viewport before asserting its return home', () => {
  const probe = resolveProbe(map.entries.find((entry) => entry.id === 'action:fit'), 'ready')
  assert.equal(probe.setup.steps.at(-1).kind, 'zoom-before-fit')
  assert.deepEqual(probe.setup.steps.at(-1).control, {
    role: 'button', name: 'Zoom in', exact: true,
    scope: { role: 'toolbar', name: 'View', exact: true },
  })
  assert.equal(probe.assertion.target, 'viewer-home')
})

test('the refreshed read tool has ready, read-only and running probes', () => {
  const entry = map.entries.find((entry) => entry.id === 'tool:count-by-layer')
  assert.ok(entry)
  assert.equal(map.entries.some((entry) => entry.id === 'tool:count-panels'), false)
  for (const state of ['ready', 'read-only', 'job-running']) {
    const probe = resolveProbe(entry, state)
    assert.ok(probe.setup.steps.some((step) => step.kind === 'catalog-tool' && step.name === 'count-by-layer'))
    assert.equal(probe.assertion.kind, state === 'ready' ? 'opens' : 'disabled_with_reason')
  }
})
