import assert from 'node:assert/strict'
import test from 'node:test'
import { resolve } from 'node:path'
import { requireSolarFlags, installSpendGuard, assertRunRoot } from './solarProofGuard.mjs'

const flags = ['VITE_CAD_EDIT', 'VITE_SOLAR_FLOW_RAIL', 'VITE_SOLAR_SETTINGS_FORM']

test('only all three Solar flags set to string 1 are supported', () => {
  for (let mask = 0; mask < 8; mask++) {
    const env = Object.fromEntries(flags.map((flag, index) => [flag, mask & (1 << index) ? '1' : '0']))
    if (mask === 7) assert.doesNotThrow(() => requireSolarFlags(env))
    else assert.throws(() => requireSolarFlags(env), {
      message: 'UNSUPPORTED_LOCAL: solar proof needs VITE_CAD_EDIT, VITE_SOLAR_FLOW_RAIL and VITE_SOLAR_SETTINGS_FORM = 1',
    })
  }
  for (const flag of flags) {
    for (const value of [undefined, '', 1, true, 'true', ' 1']) {
      assert.throws(() => requireSolarFlags({ ...Object.fromEntries(flags.map(key => [key, '1'])), [flag]: value }), /^Error: UNSUPPORTED_LOCAL:/)
    }
  }
  assert.throws(() => requireSolarFlags({}), /^Error: UNSUPPORTED_LOCAL:/)
})

test('both managed run-root shapes return the parent directory', () => {
  for (const name of ['leaf-unified-local-e2e-20261105-123456', '.e2e-run']) {
    assert.equal(assertRunRoot(resolve(name, 'store')), resolve(name))
  }
})

test('unrelated, malformed and suffixed run roots are refused', () => {
  for (const name of ['shared-store', 'leaf-unified-local-e2e-20261105', '.e2e-run-extra']) {
    assert.throws(() => assertRunRoot(resolve(name, 'store')), /Invalid solar proof run root:/)
  }
  for (const value of [undefined, null, '']) assert.throws(() => assertRunRoot(value), /Invalid solar proof store directory/)
})

test('spend guard blocks and records every matching request, including repeats', async () => {
  let matches, handler
  const assertNoSpend = await installSpendGuard({
    async route(matcher, callback) { matches = matcher; handler = callback },
  })
  assert.doesNotThrow(assertNoSpend)
  for (const url of ['http://127.0.0.1:8230/api/run', 'https://api.leafdesign.ai.example.com/', 'https://example.com/developer.api.autodesk.com']) {
    assert.equal(matches(new URL(url)), false)
  }
  const blocked = []
  for (const host of ['developer.api.autodesk.com', 'api.leafdesign.ai', 'api.leafdesign.ai']) {
    const url = `https://${host}:443/any/path?proof=1`
    assert.equal(matches(new URL(url)), true)
    await handler({ request: () => ({ url: () => url }), abort: async () => { blocked.push(host) } })
  }
  assert.deepEqual(blocked, ['developer.api.autodesk.com', 'api.leafdesign.ai', 'api.leafdesign.ai'])
  assert.throws(assertNoSpend, { message: `SPEND_GUARD: blocked requests to ${blocked.join(', ')}` })
  assert.throws(assertNoSpend, /^Error: SPEND_GUARD:/)
})
