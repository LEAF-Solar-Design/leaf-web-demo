import assert from 'node:assert/strict'
import test from 'node:test'
import { assertProdResponse, resolveProdBaseUrl } from './prodConfig.mjs'

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
