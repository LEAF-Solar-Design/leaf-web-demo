import assert from 'node:assert/strict'
import test from 'node:test'
import {
  assertProdResponse,
  oneShellOn,
  requireProdTarget,
  resolveExpectedSha,
  resolveProdBaseUrl,
} from './prodConfig.mjs'

const SHA = 'ae5dc8470123456789abcdef0123456789abcdef'

test('accepts the production HTTPS base URL', () => {
  const url = 'https://platform.leafdesign.ai'
  assert.equal(resolveProdBaseUrl({ LEAF_E2E_PROD_BASE_URL: url }), url)
})

test('refuses a staging base URL and names its host', () => {
  assert.throws(() => resolveProdBaseUrl({ LEAF_E2E_PROD_BASE_URL: 'https://platform-staging.leafdesign.ai' }), {
    message: 'production driver refuses platform-staging.leafdesign.ai: not a production host',
  })
})

test('refuses HTTP even for a production host', () => {
  assert.throws(() => resolveProdBaseUrl({ LEAF_E2E_PROD_BASE_URL: 'http://platform.leafdesign.ai' }), {
    message: 'production driver refuses platform.leafdesign.ai: not a production host',
  })
})

test('refuses an unset base URL', () => {
  assert.throws(() => resolveProdBaseUrl({}), { message: 'LEAF_E2E_PROD_BASE_URL is not set' })
})

test('refuses a response redirected to staging', () => {
  assert.throws(() => assertProdResponse('https://platform-staging.leafdesign.ai/api/health'), {
    message: 'production driver refuses platform-staging.leafdesign.ai: not a production host',
  })
})

test('refuses URLs over the 4 KB bound', () => {
  assert.throws(() => resolveProdBaseUrl({ LEAF_E2E_PROD_BASE_URL: `https://platform.leafdesign.ai/${'a'.repeat(4096)}` }))
})

test('E06 row1 the studio door origin is accepted', () => {
  const url = 'https://studio.leafautomation.ai'
  assert.equal(resolveProdBaseUrl({ LEAF_E2E_PROD_BASE_URL: url }), url)
  assert.equal(assertProdResponse('https://studio.leafautomation.ai/api/health').host, 'studio.leafautomation.ai')
})

test('E06 row2 a studio staging-looking or look-alike host is refused', () => {
  for (const host of ['studio.leafautomation.ai.evil.example', 'studio-staging.leafautomation.ai']) {
    assert.throws(() => resolveProdBaseUrl({ LEAF_E2E_PROD_BASE_URL: `https://${host}` }), {
      message: `production driver refuses ${host}: not a production host`,
    })
    assert.throws(() => assertProdResponse(`https://${host}/api/health`), {
      message: `production driver refuses ${host}: not a production host`,
    })
  }
  assert.throws(() => resolveProdBaseUrl({ LEAF_E2E_PROD_BASE_URL: 'http://studio.leafautomation.ai' }), {
    message: 'production driver refuses studio.leafautomation.ai: not a production host',
  })
})

test('E06 row3 resolveExpectedSha returns null unset and the sha when valid', () => {
  assert.equal(resolveExpectedSha({}), null)
  assert.equal(resolveExpectedSha({ LEAF_E2E_EXPECTED_SHA: '' }), null)
  assert.equal(resolveExpectedSha({ LEAF_E2E_EXPECTED_SHA: SHA }), SHA)
})

test('E06 row4 resolveExpectedSha throws on uppercase, 39 characters, and a non-hex character', () => {
  const message = 'LEAF_E2E_EXPECTED_SHA must be 40 lowercase hex characters'
  assert.throws(() => resolveExpectedSha({ LEAF_E2E_EXPECTED_SHA: SHA.toUpperCase() }), { message })
  assert.throws(() => resolveExpectedSha({ LEAF_E2E_EXPECTED_SHA: SHA.slice(0, 39) }), { message })
  assert.throws(() => resolveExpectedSha({ LEAF_E2E_EXPECTED_SHA: `${SHA.slice(0, 39)}g` }), { message })
})

test('E06 row5 requireProdTarget throws when required and the base URL is missing', () => {
  const message = 'LEAF_E2E_PROD_REQUIRED=1 but LEAF_E2E_PROD_BASE_URL is not set'
  assert.throws(() => requireProdTarget({ LEAF_E2E_PROD_REQUIRED: '1', LEAF_E2E_EXPECTED_SHA: SHA }), { message })
  assert.throws(
    () => requireProdTarget({ LEAF_E2E_PROD_REQUIRED: '1', LEAF_E2E_PROD_BASE_URL: '', LEAF_E2E_EXPECTED_SHA: SHA }),
    { message },
  )
})

test('E06 row6 requireProdTarget throws when required and the expected sha is missing', () => {
  const base = 'https://platform.leafdesign.ai'
  const message = 'LEAF_E2E_PROD_REQUIRED=1 but LEAF_E2E_EXPECTED_SHA is not set'
  assert.throws(() => requireProdTarget({ LEAF_E2E_PROD_REQUIRED: '1', LEAF_E2E_PROD_BASE_URL: base }), { message })
  assert.throws(
    () => requireProdTarget({ LEAF_E2E_PROD_REQUIRED: '1', LEAF_E2E_PROD_BASE_URL: base, LEAF_E2E_EXPECTED_SHA: '' }),
    { message },
  )
  assert.equal(
    requireProdTarget({ LEAF_E2E_PROD_REQUIRED: '1', LEAF_E2E_PROD_BASE_URL: base, LEAF_E2E_EXPECTED_SHA: SHA }),
    undefined,
  )
})

test('E06 row7 requireProdTarget is silent when not required', () => {
  assert.equal(requireProdTarget({}), undefined)
  assert.equal(requireProdTarget({ LEAF_E2E_PROD_REQUIRED: '0' }), undefined)
  assert.equal(requireProdTarget({ LEAF_E2E_PROD_REQUIRED: 'true' }), undefined)
})

test('E06 row8 oneShellOn is true for the measured production text', () => {
  assert.equal(oneShellOn('window.__LEAF_FLAGS = { oneShell: "1" }'), true)
  assert.equal(oneShellOn('window.__LEAF_FLAGS = { oneShell: "1" }\n'), true)
})

test('E06 row9 oneShellOn is false for "0", a missing flag, an empty file, and a 65 KB file', () => {
  assert.equal(oneShellOn('window.__LEAF_FLAGS = { oneShell: "0" }\n'), false)
  assert.equal(oneShellOn("window.__LEAF_FLAGS = { oneShell: '0' }\n"), false)
  assert.equal(oneShellOn('window.__LEAF_FLAGS = { oneShell: "false" }\n'), false)
  assert.equal(oneShellOn('window.__LEAF_FLAGS = { oneShell: 1 }\n'), false)
  assert.equal(oneShellOn('window.__LEAF_FLAGS = {}\n'), false)
  assert.equal(oneShellOn('// oneShell: "1"\n'), false)
  assert.equal(oneShellOn('// window.__LEAF_FLAGS = { oneShell: "1" }\n'), false)
  assert.equal(oneShellOn(''), false)
  assert.equal(oneShellOn(undefined), false)
  assert.equal(oneShellOn(`${'/'.repeat(65 * 1024)}\nwindow.__LEAF_FLAGS = { oneShell: "1" }\n`), false)
})
