import assert from 'node:assert/strict'
import test from 'node:test'
import { PROD_HOSTS, assertProdResponse, requireFixtureProject, resolveExpectedSha, validateProdConfig } from './prodConfig.mjs'
import { requireProdTarget, resolveProdBaseUrl } from './prodConfig.mjs'

const TOKEN = 'SYNTHETIC_CONFIG_SECRET_42'
const ID = '11111111-1111-4111-8111-111111111111'
const valid = () => ({ LEAF_E2E_PROD_REQUIRED: '1', LEAF_E2E_PROD_BASE_URL: 'https://platform.leafdesign.ai', LEAF_E2E_PROD_ACCEPTANCE_TOKEN: TOKEN, LEAF_E2E_EXPECTED_SHA: 'a'.repeat(40), LEAF_E2E_PROD_PROJECT_ID: ID })
const inputs = Object.keys(valid()).filter((key) => key !== 'LEAF_E2E_PROD_REQUIRED')
const fixedError = (error) => { assert.match(error.message, /^PROD_CONFIG_INVALID LEAF_E2E_[A-Z_]+$/); return true }

test('required mode rejects every missing input before skip', () => {
  let skips = 0
  let transports = 0
  for (const name of inputs) for (const value of [undefined, '']) {
    assert.throws(() => {
      validateProdConfig({ ...valid(), [name]: value })
      skips += 1
      transports += 1
    }, fixedError)
  }
  assert.equal(skips, 0)
  assert.equal(transports, 0)
})

test('optional mode disables only wholly absent inputs', () => {
  for (const flag of [undefined, '0']) {
    assert.deepEqual(validateProdConfig({ LEAF_E2E_PROD_REQUIRED: flag }), { enabled: false, required: false })
    assert.ok(Object.isFrozen(validateProdConfig({})))
    for (const name of inputs) {
      assert.throws(() => validateProdConfig({ LEAF_E2E_PROD_REQUIRED: flag, [name]: valid()[name] }), fixedError)
      assert.throws(() => validateProdConfig({ ...valid(), LEAF_E2E_PROD_REQUIRED: flag, [name]: 'bad value' }), fixedError)
      assert.throws(() => validateProdConfig({ [name]: '' }), fixedError)
    }
  }
})

test('required flag accepts only absent zero or one', () => {
  for (const flag of [undefined, '0', '1']) assert.equal(validateProdConfig({ ...valid(), LEAF_E2E_PROD_REQUIRED: flag }).required, flag === '1')
  for (const flag of ['', ' ', 'true', 'false', '01', ' 1', '1 ', '\n', true, false, 0, 1, null]) assert.throws(() => validateProdConfig({ ...valid(), LEAF_E2E_PROD_REQUIRED: flag }), fixedError)
})

test('production config validates origin token sha and fixture uuid', () => {
  for (const host of PROD_HOSTS) {
    const config = validateProdConfig({ ...valid(), LEAF_E2E_PROD_BASE_URL: `https://${host}/` })
    assert.equal(config.origin, `https://${host}`)
    assert.ok(Object.isFrozen(config))
    assert.equal(assertProdResponse(`https://${host}/api/health`).host, host)
  }
  for (const url of ['http://platform.leafdesign.ai', 'https://platform-staging.leafdesign.ai', 'https://studio.leafautomation.ai.evil.example', 'https://studio-staging.leafautomation.ai', 'https://platform.leafdesign.ai:444', 'https://user:pass@platform.leafdesign.ai', 'https://platform.leafdesign.ai/app', 'https://platform.leafdesign.ai/?a=1', 'https://platform.leafdesign.ai/#hash', 'https://platform.leafdesign.ai/?', 'https://platform.leafdesign.ai/#', `https://platform.leafdesign.ai/${'a'.repeat(4096)}`, ' https://platform.leafdesign.ai']) assert.throws(() => validateProdConfig({ ...valid(), LEAF_E2E_PROD_BASE_URL: url }), fixedError)
  for (const url of ['http://platform.leafdesign.ai/api/health', 'https://platform-staging.leafdesign.ai/api/health']) assert.throws(() => assertProdResponse(url), fixedError)
  for (const url of ['https://@platform.leafdesign.ai', 'https://platform.leafdesign.ai/..']) assert.throws(() => validateProdConfig({ ...valid(), LEAF_E2E_PROD_BASE_URL: url }), fixedError)
  for (const token of ['', 'a b', ' a', 'a\n', 'a\t', 'a\x00', 'a\x7f', 'a'.repeat(16385)]) assert.throws(() => validateProdConfig({ ...valid(), LEAF_E2E_PROD_ACCEPTANCE_TOKEN: token }), fixedError)
  for (const sha of ['A'.repeat(40), 'a'.repeat(39), `${'a'.repeat(39)}g`, `${'a'.repeat(40)}\n`]) assert.throws(() => validateProdConfig({ ...valid(), LEAF_E2E_EXPECTED_SHA: sha }), fixedError)
  for (const id of ['bad', ID.toUpperCase().replace('11111111', 'ABCDEFAB'), '00000000-0000-0000-0000-000000000000', `${ID} `, `${ID}\n`, ID.replaceAll('-', '')]) assert.throws(() => validateProdConfig({ ...valid(), LEAF_E2E_PROD_PROJECT_ID: id }), fixedError)
  assert.equal(resolveExpectedSha({}), null)
  assert.equal(resolveExpectedSha({ LEAF_E2E_EXPECTED_SHA: '' }), null)
  assert.equal(resolveExpectedSha(valid()), 'a'.repeat(40))
})

test('config errors never echo supplied secrets', () => {
  for (const name of Object.keys(valid())) {
    assert.throws(() => validateProdConfig({ ...valid(), [name]: `${TOKEN} bad` }), (error) => {
      fixedError(error)
      assert.ok(!String(error).includes(TOKEN))
      assert.ok(!error.stack.includes(TOKEN))
      return true
    })
  }
})

test('fixture selection requires the exact unique project', () => {
  const fixture = { project_id: ID, name: 'Manifest fixture' }
  const other = { project_id: '22222222-2222-4222-8222-222222222222', name: 'Other' }
  assert.equal(requireFixtureProject([other, fixture], ID), fixture)
  assert.equal(requireFixtureProject([fixture, other], ID), fixture)
  for (const projects of [[], [other], [fixture, fixture], null, {}, [null], [false], [[fixture]], [{ ...fixture, name: null }], [{ ...fixture, name: '' }], [{ ...fixture, name: ' ' }], [{ ...fixture, name: 'bad\nname' }]]) assert.throws(() => requireFixtureProject(projects, ID), /^Error: PROD_FIXTURE_/)
})

const SHA = 'ae5dc8470123456789abcdef0123456789abcdef'
const baseUrlError = (host) => (error) => {
  assert.equal(error.message, 'PROD_CONFIG_INVALID LEAF_E2E_PROD_BASE_URL')
  if (host !== undefined) assert.ok(!error.message.includes(host))
  return true
}

test('accepts the production HTTPS base URL', () => {
  const url = 'https://platform.leafdesign.ai'
  assert.equal(resolveProdBaseUrl({ LEAF_E2E_PROD_BASE_URL: url }), url)
})

test('refuses a staging base URL and names its host', () => {
  assert.throws(() => resolveProdBaseUrl({ LEAF_E2E_PROD_BASE_URL: 'https://platform-staging.leafdesign.ai' }), baseUrlError('platform-staging.leafdesign.ai'))
})

test('refuses HTTP even for a production host', () => {
  assert.throws(() => resolveProdBaseUrl({ LEAF_E2E_PROD_BASE_URL: 'http://platform.leafdesign.ai' }), baseUrlError('platform.leafdesign.ai'))
})

test('refuses an unset base URL', () => {
  assert.throws(() => resolveProdBaseUrl({}), baseUrlError())
})

test('refuses a response redirected to staging', () => {
  assert.throws(() => assertProdResponse('https://platform-staging.leafdesign.ai/api/health'), baseUrlError('platform-staging.leafdesign.ai'))
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
    assert.throws(() => resolveProdBaseUrl({ LEAF_E2E_PROD_BASE_URL: `https://${host}` }), baseUrlError(host))
    assert.throws(() => assertProdResponse(`https://${host}/api/health`), baseUrlError(host))
  }
  assert.throws(() => resolveProdBaseUrl({ LEAF_E2E_PROD_BASE_URL: 'http://studio.leafautomation.ai' }), baseUrlError('studio.leafautomation.ai'))
})

test('E06 row3 resolveExpectedSha returns null unset and the sha when valid', () => {
  assert.equal(resolveExpectedSha({}), null)
  assert.equal(resolveExpectedSha({ LEAF_E2E_EXPECTED_SHA: '' }), null)
  assert.equal(resolveExpectedSha({ LEAF_E2E_EXPECTED_SHA: SHA }), SHA)
})

test('E06 row4 resolveExpectedSha throws on uppercase, 39 characters, and a non-hex character', () => {
  for (const value of [SHA.toUpperCase(), SHA.slice(0, 39), `${SHA.slice(0, 39)}g`]) {
    assert.throws(() => resolveExpectedSha({ LEAF_E2E_EXPECTED_SHA: value }), (error) => {
      assert.equal(error.message, 'PROD_CONFIG_INVALID LEAF_E2E_EXPECTED_SHA')
      assert.ok(!error.message.includes(value))
      return true
    })
  }
})

test('E06 row5 requireProdTarget throws when required and the base URL is missing', () => {
  const message = 'PROD_CONFIG_INVALID LEAF_E2E_PROD_BASE_URL'
  assert.throws(() => requireProdTarget({ LEAF_E2E_PROD_REQUIRED: '1', LEAF_E2E_EXPECTED_SHA: SHA }), { message })
  assert.throws(
    () => requireProdTarget({ LEAF_E2E_PROD_REQUIRED: '1', LEAF_E2E_PROD_BASE_URL: '', LEAF_E2E_EXPECTED_SHA: SHA }),
    { message },
  )
})

test('E06 row6 requireProdTarget throws when required and the expected sha is missing', () => {
  const base = 'https://platform.leafdesign.ai'
  const env = { LEAF_E2E_PROD_REQUIRED: '1', LEAF_E2E_PROD_BASE_URL: base, LEAF_E2E_PROD_ACCEPTANCE_TOKEN: 'synthetic-acceptance-token', LEAF_E2E_PROD_PROJECT_ID: '11111111-1111-4111-8111-111111111111' }
  const message = 'PROD_CONFIG_INVALID LEAF_E2E_EXPECTED_SHA'
  assert.throws(() => requireProdTarget(env), { message })
  assert.throws(
    () => requireProdTarget({ ...env, LEAF_E2E_EXPECTED_SHA: '' }),
    { message },
  )
  assert.equal(
    requireProdTarget({ ...env, LEAF_E2E_EXPECTED_SHA: SHA }),
    undefined,
  )
})

test('E06 row7 requireProdTarget is silent when not required', () => {
  assert.equal(requireProdTarget({}), undefined)
  assert.equal(requireProdTarget({ LEAF_E2E_PROD_REQUIRED: '0' }), undefined)
  assert.throws(() => requireProdTarget({ LEAF_E2E_PROD_REQUIRED: 'true' }), { message: 'PROD_CONFIG_INVALID LEAF_E2E_PROD_REQUIRED' })
})
