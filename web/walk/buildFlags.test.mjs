import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { shippedViteFlags, productionBundleCacheKey, PRODUCTION_BUNDLE_INPUTS, productionBundleMarker } from './buildFlags.mjs'

const dockerfile = await readFile(new URL('../../deploy/Dockerfile.web', import.meta.url), 'utf8')

test('production bundle inputs retain the previous inputs and track Next build inputs', () => {
  for (const path of ['src', 'public', 'vite-plugins', 'vite.config.js', 'index.html', 'package.json', 'package-lock.json', 'next.config.mjs', 'webpack', 'scripts']) {
    assert.ok(PRODUCTION_BUNDLE_INPUTS.includes(path), path)
  }
  assert.ok(Object.isFrozen(PRODUCTION_BUNDLE_INPUTS))
})

test('production bundle markers preserve existing fields and record build and reuse times', () => {
  const flags = shippedViteFlags(dockerfile)
  for (const engine_unavailable_reason of [null, 'wasm-pack unavailable on this host']) {
    const cacheKey = productionBundleCacheKey(flags, engine_unavailable_reason)
    const existing = Object.freeze({ sourceMtime: 123456.75, apiBase: '', mock: false, authLive: false, flags, engine_unavailable_reason, cacheKey })
    const at = '2026-11-01T10:00:00.000Z'
    const built = Object.freeze(productionBundleMarker({ ...existing, provenance: 'built', at }))
    const reused = productionBundleMarker({ ...built, provenance: 'reused', at: '2026-11-01T11:00:00.000Z' })
    const reusedAgain = productionBundleMarker({ ...reused, provenance: 'reused', at: '2026-11-01T12:00:00.000Z' })
    for (const marker of [built, reused, reusedAgain]) {
      assert.equal(JSON.stringify(Object.fromEntries(Object.keys(existing).map((key) => [key, marker[key]]))), JSON.stringify(existing))
      assert.equal(marker.builtAt, at)
    }
    assert.equal(built.provenance, 'built')
    assert.equal(built.builtAt, built.lastUsedAt)
    assert.equal(reused.provenance, 'reused')
    assert.equal(reused.lastUsedAt, '2026-11-01T11:00:00.000Z')
    assert.equal(reusedAgain.lastUsedAt, '2026-11-01T12:00:00.000Z')
    assert.equal(productionBundleMarker({ ...existing, provenance: 'reused', at }).builtAt, null)
    assert.equal(productionBundleMarker({ ...reused, provenance: 'built', at: reused.lastUsedAt }).builtAt, reused.lastUsedAt)
  }
})

test('the deployed defaults enable all five shipped feature surfaces and exclude walk overrides', () => {
  const flags = shippedViteFlags(dockerfile)
  for (const name of ['VITE_LIFECYCLE_UI', 'VITE_IOS_SURFACE', 'VITE_CAD_EDIT', 'VITE_SOLAR_FLOW_RAIL', 'VITE_SOLAR_SETTINGS_FORM']) assert.equal(flags[name], '1')
  for (const name of Object.keys(flags)) {
    assert.ok(!['VITE_API_BASE', 'VITE_MOCK', 'VITE_TENANT_ID'].includes(name))
    assert.ok(!name.startsWith('VITE_AUTH0_'))
  }
  assert.deepEqual(shippedViteFlags('ARG VITE_TENANT_ID=live\nARG VITE_AUTH0_NEW=secret'), {})
})

test('new Dockerfile flags are discovered without a code change', () => {
  assert.equal(shippedViteFlags(dockerfile + '\nARG VITE_NEW_SURFACE=1\n').VITE_NEW_SURFACE, '1')
  assert.deepEqual(shippedViteFlags('# ARG VITE_COMMENT=1\nARG VITE_QUOTED="on" # enabled\nARG VITE_EMPTY='), { VITE_QUOTED: 'on', VITE_EMPTY: '' })
})

test('bundle cache identity changes with effective flags and engine availability', () => {
  assert.equal(productionBundleCacheKey({ VITE_Z: '0', VITE_A: '1' }), '{"flags":{"VITE_A":"1","VITE_Z":"0"},"engine_unavailable_reason":null}')
  const flags = shippedViteFlags(dockerfile)
  const ready = productionBundleCacheKey(flags)
  assert.notEqual(ready, productionBundleCacheKey({ ...flags, VITE_CAD_EDIT: '0' }))
  assert.notEqual(ready, productionBundleCacheKey(flags, 'wasm-pack unavailable on this host'))
  assert.notEqual(productionBundleCacheKey(flags, 'first failure'), productionBundleCacheKey(flags, 'second failure'))
  assert.equal(ready, productionBundleCacheKey(Object.fromEntries(Object.entries(flags).reverse())))
})
