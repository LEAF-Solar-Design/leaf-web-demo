import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { releaseProjectSolarCheckout, takeProjectSolarCheckout } from '../walk/fixtures.mjs'
import { withProjectSolarCleanup } from './projectSolarSteps.mjs'

const apiBase = 'https://platform-staging.leafdesign.ai'
const path = '/api/projects/project/drawings/drawing/checkout'
const capability = 'lco1.private-capability'
const token = 'private-bearer-token'
const response = (body, status = 200) => ({ status: () => status, json: async () => body })
function runtime() {
  return { page: { isClosed: () => true }, cleanup: [], projectSolarToken: token,
    projectSolarCheckout: { apiBase, path, capability, released: false },
    evidence: { projectSolar: { steps: [] } } }
}
function requestFake(reply = async () => response({ released: true })) {
  const calls = []
  let disposed = false
  return { calls, get disposed() { return disposed }, newContext: async () => ({
    delete: async (url, options) => { calls.push({ url, options }); return reply() },
    dispose: async () => { disposed = true },
  }) }
}

test('absent or already released checkout does nothing', async () => {
  const request = { newContext: () => assert.fail('no request context') }
  for (const value of [{}, { projectSolarCheckout: { released: true } }]) {
    assert.equal(await releaseProjectSolarCheckout(value, { request }), undefined)
  }
})

test('acquisition is recorded before assertions and normal cleanup clicks Release', async () => {
  const events = []
  const value = runtime()
  delete value.projectSolarCheckout
  value.projectSolar = { apiBase, binding: { project_id: 'project', drawing_id: 'drawing', organization_id: 'org' } }
  value.page = { isClosed: () => false, getByRole: () => ({ getByRole: (_role, { name }) => ({
    click: async () => events.push(name), isEnabled: async () => true,
  }) }) }
  const action = async (_page, base, method, actualPath, click) => {
    assert.equal(base, apiBase)
    assert.equal(actualPath, path)
    await click()
    return response(method === 'POST'
      ? { acquired: true, checkout_capability: capability, project_id: 'wrong-project' }
      : { released: true, checkout: null })
  }
  const assertions = actual => ({ toBe: expected => assert.equal(actual, expected) })
  await assert.rejects(takeProjectSolarCheckout(value, assertions, action))
  assert.deepEqual(value.projectSolarCheckout, { apiBase, path, capability, released: false })
  assert.equal(value.cleanup.length, 1)
  assert.equal(await releaseProjectSolarCheckout(value, {
    request: { newContext: () => assert.fail('normal UI release must not use API') },
  }), undefined)
  await value.cleanup[0]()
  assert.deepEqual(events, ['Take', 'Release'])
  assert.equal(value.projectSolarCheckout.released, true)
})

test('closed page releases via an independent context with bearer and capability', async () => {
  const value = runtime(), request = requestFake()
  assert.equal(await releaseProjectSolarCheckout(value, { request }), undefined)
  assert.equal(request.calls.length, 1)
  assert.equal(request.calls[0].url, `${apiBase}${path}`)
  assert.deepEqual(request.calls[0].options.headers, {
    Authorization: `Bearer ${token}`, 'X-Checkout-Capability': capability,
  })
  assert.ok(request.calls[0].options.timeout > 0 && request.calls[0].options.timeout <= 10_000)
  assert.equal(value.projectSolarCheckout.released, true)
  assert.equal(request.disposed, true)
})

test('UI failure falls back to API DELETE', async () => {
  const value = runtime(), request = requestFake()
  value.page.isClosed = () => false
  let attempted = false
  value.projectSolarReleaseUI = async () => { attempted = true; throw new Error('page crashed') }
  assert.equal(await releaseProjectSolarCheckout(value, { request }), undefined)
  assert.equal(attempted, true)
  assert.equal(request.calls.length, 1)
  assert.equal(value.projectSolarCheckout.released, true)
  assert.equal(value.projectSolarCheckout.uiReleaseFailed, true)
  assert.equal(value.evidence.projectSolar.releaseUiError, 'Project checkout UI release failed')
  assert.equal(value.evidence.projectSolar.releaseError, undefined)
})

test('registered cleanup fails after a UI failure even when API fallback released the checkout', async () => {
  const value = runtime(), request = requestFake()
  delete value.projectSolarCheckout
  value.projectSolar = { apiBase, binding: { project_id: 'project', drawing_id: 'drawing', organization_id: 'org' } }
  value.page = { isClosed: () => false, getByRole: () => ({ getByRole: () => ({}) }) }
  const assertions = actual => ({ toBe: expected => assert.equal(actual, expected) })
  await assert.rejects(takeProjectSolarCheckout(value, assertions, async () => response({
    acquired: true, checkout_capability: capability, project_id: 'wrong-project',
  })))
  assert.equal(value.cleanup.length, 1)
  value.projectSolarReleaseUI = async () => { throw new Error(`${token} ${capability}`) }
  assert.equal(await releaseProjectSolarCheckout(value, { request }), undefined)
  assert.equal(request.calls.length, 1)
  assert.equal(value.projectSolarCheckout.released, true)
  assert.equal(value.projectSolarCheckout.uiReleaseFailed, true)
  assert.equal(value.evidence.projectSolar.releaseUiError, 'Project checkout UI release failed')
  await assert.rejects(value.cleanup[0](), { message: 'Project checkout UI release failed' })
  for (const secret of [token, capability]) assert.equal(JSON.stringify(value.evidence).includes(secret), false)
})

test('afterEach does not throw when UI release fails and API fallback succeeds', async () => {
  const source = readFileSync(new URL('./project-solar.spec.mjs', import.meta.url), 'utf8').replace(/^import .*\n/gm, '')
  let hook
  const declare = () => {}
  declare.skip = declare.beforeEach = () => {}
  declare.afterEach = callback => { hook = callback }
  const value = runtime(), request = requestFake()
  value.page.isClosed = () => false
  value.projectSolarReleaseUI = async () => { throw new Error(`${token} ${capability}`) }
  new Function('test', 'process', 'request', 'releaseProjectSolarCheckout', source)(
    declare, { env: {} }, request, releaseProjectSolarCheckout)
  const testInfo = { projectSolarRuntime: value, status: 'passed' }
  await assert.doesNotReject(hook({}, testInfo))
  assert.equal(testInfo.status, 'passed')
  assert.equal(request.calls.length, 1)
  assert.equal(value.projectSolarCheckout.released, true)
  assert.equal(value.projectSolarCheckout.uiReleaseFailed, true)
  assert.equal(value.evidence.projectSolar.releaseUiError, 'Project checkout UI release failed')
})

test('hanging DELETE is bounded overall and its context is disposed', async () => {
  const value = runtime(), request = requestFake(() => new Promise(() => {}))
  const start = Date.now()
  const error = await releaseProjectSolarCheckout(value, { request, timeoutMs: 25 })
  assert.ok(error instanceof Error)
  assert.ok(Date.now() - start < 1000)
  assert.equal(value.projectSolarCheckout.released, false)
  assert.equal(request.disposed, true)
  assert.equal(value.evidence.projectSolar.releaseError, error.message)
})

test('failed release returns a redacted error without throwing', async () => {
  const value = runtime(), request = requestFake(async () => { throw new Error(`${token} ${capability}`) })
  const error = await releaseProjectSolarCheckout(value, { request })
  assert.ok(error instanceof Error)
  assert.equal(value.evidence.projectSolar.releaseError, error.message)
  for (const secret of [token, capability]) {
    assert.equal(JSON.stringify(value.evidence).includes(secret), false)
    assert.equal(String(error).includes(secret), false)
  }
  assert.equal(value.projectSolarCheckout.released, false)
})

test('nothing-held responses release without a capability or response body', async () => {
  for (const status of [404, 409]) {
    const value = runtime(), request = requestFake(async () => response(null, status))
    delete value.projectSolarCheckout.capability
    assert.equal(await releaseProjectSolarCheckout(value, { request }), undefined)
    assert.equal(value.projectSolarCheckout.released, true)
    assert.deepEqual(request.calls[0].options.headers, { Authorization: `Bearer ${token}` })
  }
})

test('unconfirmed release is recorded as a failure', async () => {
  for (const reply of [response({ released: false }), response({ released: true }, 500)]) {
    const value = runtime()
    assert.ok(await releaseProjectSolarCheckout(value, { request: requestFake(async () => reply) }) instanceof Error)
    assert.equal(value.projectSolarCheckout.released, false)
  }
})

test('wrapper preserves the original action error and runs all cleanups in reverse', async () => {
  const value = runtime(), events = [], original = new Error('original action failure')
  value.cleanup.push(async () => events.push('first'), async () => { events.push('second'); throw new Error(token) })
  await assert.rejects(withProjectSolarCleanup(value, async () => { throw original }, async () => {
    events.push('finish'); throw new Error(capability)
  }), error => error === original)
  assert.deepEqual(events, ['second', 'first', 'finish'])
  assert.ok(value.evidence.projectSolar.cleanupError)
  assert.equal(JSON.stringify(value.evidence).includes(token), false)
  assert.equal(JSON.stringify(value.evidence).includes(capability), false)
})

test('cleanup failure still fails a successful action', async () => {
  const value = runtime(), original = new Error('cleanup failed')
  value.cleanup.push(async () => { throw original })
  await assert.rejects(withProjectSolarCleanup(value, async () => {}), error => error === original)
})

test('afterEach releases the test runtime and tolerates release failures', async () => {
  const source = readFileSync(new URL('./project-solar.spec.mjs', import.meta.url), 'utf8').replace(/^import .*\n/gm, '')
  let hook
  const declare = () => {}
  declare.skip = declare.beforeEach = () => {}
  declare.afterEach = callback => { hook = callback }
  const request = {}, seen = [], value = runtime()
  new Function('test', 'process', 'request', 'releaseProjectSolarCheckout', source)(
    declare, { env: {} }, request, async (actual, options) => {
      seen.push(actual)
      assert.equal(options.request, request)
      throw new Error('teardown release failure')
    })
  for (const status of ['passed', 'failed', 'timedOut']) await hook({}, { projectSolarRuntime: value, status })
  await hook({}, {})
  assert.deepEqual(seen, [value, value, value])
})
