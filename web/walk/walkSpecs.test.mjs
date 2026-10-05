import test from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync } from 'node:fs'
import { SWEEP_SPECS, walkTestMatch } from './walkSpecs.mjs'

const knownProofSpecs = [
  'u3-header-rail.spec.mjs',
  'u5-shell-hit-targets.spec.mjs',
  'u4-solar-ribbon.spec.mjs',
  'unsupported-cost.spec.mjs',
  'unsupported-fast.spec.mjs',
  'u6-drawer-escape.spec.mjs',
  'u7-catalog-error.spec.mjs',
  'ux-evidence.spec.mjs',
  'w1o-solar-unsupported.spec.mjs',
  'w1z-map.spec.mjs',
  'w1p-states.spec.mjs',
  'w1w-engine-recipes.spec.mjs',
  'w1x-recipes.spec.mjs',
  'w1z-oracles.spec.mjs',
  'w1z-states.spec.mjs',
  'w1z-faults.spec.mjs',
  'w1z-harness.spec.mjs',
  'w1y-recipes.spec.mjs',
]

function specFiles(directory, prefix = '') {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const name = `${prefix}${entry.name}`
    if (entry.isDirectory()) return specFiles(new URL(`${entry.name}/`, directory), `${name}/`)
    return entry.isFile() && entry.name.endsWith('.spec.mjs') ? [name] : []
  })
}

function matchesSweep(name) {
  return SWEEP_SPECS.some((pattern) => pattern === name
    || (pattern === 'journeys/**/*.spec.mjs' && /^journeys\/(?:.*\/)?[^/]+\.spec\.mjs$/.test(name)))
}

test('the default walk match is the frozen sweep list', () => {
  assert.deepEqual(SWEEP_SPECS, [
    'features.spec.mjs', 'control-inventory.spec.mjs', 'journeys/**/*.spec.mjs',
  ])
  assert.ok(Object.isFrozen(SWEEP_SPECS))
  assert.equal(walkTestMatch({}), SWEEP_SPECS)
})

test('only LEAF_WALK_PROOF=1 includes all specs', () => {
  assert.equal(walkTestMatch({ LEAF_WALK_PROOF: '1' }), '**/*.spec.mjs')
  for (const value of ['', '0', 'true', '2', '01', ' 1', '1 ', 1, true, null, undefined]) {
    assert.equal(walkTestMatch({ LEAF_WALK_PROOF: value }), SWEEP_SPECS)
  }
})

test('every walk spec is a sweep spec or an explicitly known proof', () => {
  const files = specFiles(new URL('../e2e/walk/', import.meta.url))
  const proofs = files.filter((name) => !matchesSweep(name)).sort()
  assert.deepEqual(proofs, [...knownProofSpecs].sort(),
    'Classify new walk specs as sweep specs or explicitly known slice proofs')
  assert.ok(knownProofSpecs.every((name) => !matchesSweep(name)))
})
