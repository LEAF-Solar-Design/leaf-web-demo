import assert from 'node:assert/strict'
import test from 'node:test'
import { RIBBON_TABS, PROFILE_RIBBON_TABS, profileRibbonTabData } from '../src/lib/ribbonTabs.data.js'
import { STUDIO_DRAWERS } from '../src/lib/studioDrawers.js'

// Captured from CockpitTopBand and profileRibbonTabs before extraction.
const EXPECTED_TABS = Object.freeze({
  drafting: Object.freeze(['draw', 'model', 'insert', 'annotate', 'view', 'manage']),
  solar: Object.freeze(['draw', 'solar', 'model', 'insert', 'annotate', 'view', 'manage']),
  project: Object.freeze(['project', 'tools', 'activity']),
  ship: Object.freeze(['ship']),
})
const EXPECTED_LABELS = Object.freeze({
  drafting: Object.freeze(['Draw', 'Model', 'Insert', 'Annotate', 'View', 'Manage']),
  solar: Object.freeze(['Draw', 'Solar', 'Model', 'Insert', 'Annotate', 'View', 'Manage']),
  project: Object.freeze(['Project', 'Tools', 'Activity']),
  ship: Object.freeze(['Ship']),
})
const EXPECTED_DRAWERS = Object.freeze(['nav', 'jobs', 'result', 'plan', 'none'])

function assertIds(ids, expected) {
  assert.ok(ids.length > 0)
  assert.ok(ids.every((id) => typeof id === 'string' && id.length > 0))
  assert.equal(new Set(ids).size, ids.length)
  assert.deepEqual(ids, expected)
}

for (const [profile, expected] of Object.entries(EXPECTED_TABS)) {
  test(`${profile} tab registry imports under plain Node and preserves metadata`, () => {
    const tabs = profileRibbonTabData(profile)
    assert.equal(tabs, PROFILE_RIBBON_TABS[profile])
    assertIds(tabs.map((tab) => tab.id), expected)
    assert.deepEqual(tabs.map((tab) => tab.label), EXPECTED_LABELS[profile])
    assert.deepEqual(tabs.filter((tab) => tab.reason),
      profile === 'drafting' || profile === 'solar'
        ? [{ id: 'model', label: 'Model', reason: '3D modelling is not in this engine yet' }]
        : [])
  })
}

test('other profile values retain the drafting fallback', () => {
  for (const profile of [undefined, null, '', 'ios', 'unknown', 'toString']) {
    assert.equal(profileRibbonTabData(profile), RIBBON_TABS)
  }
})

test('studio drawers import under plain Node and preserve their order', () => {
  assertIds(STUDIO_DRAWERS, EXPECTED_DRAWERS)
  assert.ok(Object.isFrozen(STUDIO_DRAWERS))
})
