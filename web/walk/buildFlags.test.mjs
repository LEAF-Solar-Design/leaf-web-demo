import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { shippedViteFlags, productionBundleCacheKey } from './buildFlags.mjs'

const dockerfile = await readFile(new URL('../../deploy/Dockerfile.web', import.meta.url), 'utf8')

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
  const flags = shippedViteFlags(dockerfile)
  const ready = productionBundleCacheKey(flags)
  assert.notEqual(ready, productionBundleCacheKey({ ...flags, VITE_CAD_EDIT: '0' }))
  assert.notEqual(ready, productionBundleCacheKey(flags, 'wasm-pack unavailable on this host'))
  assert.notEqual(productionBundleCacheKey(flags, 'first failure'), productionBundleCacheKey(flags, 'second failure'))
  assert.equal(ready, productionBundleCacheKey(Object.fromEntries(Object.entries(flags).reverse())))
})
