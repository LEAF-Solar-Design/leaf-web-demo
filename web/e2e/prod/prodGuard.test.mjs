import assert from 'node:assert/strict'
import test from 'node:test'
import { READ_ONLY_EXTERNAL_ORIGINS, createGuardedContext, createRequestFacade, decideRequest, denialLedger, installBrowserGuard } from './prodGuard.mjs'

const origin = 'https://platform.leafdesign.ai'
const external = 'https://static.example.test'
const policy = { origin, token: 'SYNTHETIC_GUARD_SECRET_42', authenticated: true, externalOrigins: [external] }
const response = (status = 200) => ({ status: () => status })

function double() {
  const order = []
  const handlers = {}
  const calls = []
  // Use the bundled class names from the locked Playwright client as well.
  class _Page { _apiName = 'Page'; async close() {} on() {} once() {} }
  const page = new _Page()
  class _Frame { page() { return page } }
  class _Response { frame() { return new _Frame() } }
  class _Locator { _apiName = 'Locator'; _selector = 'body'; page() { return page } async _expect() { return { matches: true } } }
  page.mainFrame = () => new _Frame()
  page.locator = () => new _Locator()
  page.waitForResponse = async (predicate) => { const result = new _Response(); predicate(result); return result }
  const context = {
    request: { fetch: async (url, options) => { calls.push({ url, options }); return response() } },
    route: async (pattern, handler) => { assert.equal(pattern, '**/*'); order.push('http'); handlers.http = handler },
    routeWebSocket: async (pattern, handler) => { assert.equal(pattern, '**/*'); order.push('websocket'); handlers.websocket = handler },
    newPage: async () => { order.push('page'); return page },
    pages: () => [page],
    close: async () => { order.push('close') },
  }
  const browser = { newContext: async (options) => {
    assert.equal(options.serviceWorkers, 'block')
    assert.deepEqual(options.storageState, { cookies: [], origins: [] })
    assert.equal(options.acceptDownloads, false)
    assert.equal(options.extraHTTPHeaders, undefined)
    assert.equal(options.recordVideo, undefined)
    order.push('sw-block')
    return context
  } }
  return { browser, context, page, order, handlers, calls }
}

function routeDouble(url, method = 'GET', headers = {}, status = 200) {
  const sent = []
  const route = {
    request: () => ({ url: () => url, method: () => method, headers: () => headers }),
    fetch: async (options) => { sent.push({ kind: 'fetch', options }); return response(status) },
    abort: async () => { sent.push({ kind: 'abort' }) },
    fulfill: async () => { sent.push({ kind: 'fulfill' }) },
  }
  return { route, sent }
}

test('guard denies every write before transport', async () => {
  const ledger = denialLedger()
  let calls = 0
  const api = createRequestFacade({ fetch: async () => { calls += 1 } }, policy, ledger)
  const fake = double()
  await installBrowserGuard(fake.context, policy, ledger)
  const methods = ['POST', 'PUT', 'PATCH', 'DELETE', 'CONNECT', 'TRACE', 'CUSTOM', 'get']
  for (const method of methods) for (const destination of [origin, 'https://evil.example.test', external]) {
    assert.equal(decideRequest({ url: destination, method }, policy).code, 'METHOD')
    await assert.rejects(api.fetch(destination, { method }), /^Error: PROD_POLICY_METHOD$/)
    const { route, sent } = routeDouble(destination, method)
    await fake.handlers.http(route)
    assert.deepEqual(sent, [{ kind: 'abort' }])
  }
  for (const method of ['post', 'put', 'patch', 'delete']) await assert.rejects(api[method](origin, { method: 'GET' }), /^Error: PROD_POLICY_METHOD$/)
  assert.equal(calls, 0)
  assert.equal(ledger.snapshot().METHOD, methods.length * 3 * 2 + 4)
  assert.throws(() => ledger.assertClean(), /^Error: PROD_POLICY_DENIED$/)
})

test('guard admits only explicit read destinations', () => {
  assert.deepEqual(READ_ONLY_EXTERNAL_ORIGINS, [])
  assert.ok(Object.isFrozen(READ_ONLY_EXTERNAL_ORIGINS))
  for (const method of ['GET', 'HEAD', 'OPTIONS']) for (const destination of [origin, external]) assert.equal(decideRequest({ url: `${destination}/read?q=1`, method }, policy).allowed, true)
  for (const url of [external, 'https://app.leafdesign.ai', 'https://studio.leafautomation.ai']) assert.equal(decideRequest({ url, method: 'GET' }, { origin }).allowed, false)
  for (const url of ['https://platform.leafdesign.ai.evil.example', 'https://static.example.test.evil.example', 'https://sub.static.example.test', 'http://platform.leafdesign.ai', 'file:///tmp/file', 'data:text/plain,bad', 'wss://platform.leafdesign.ai', 'about:blank', 'bad', '', 'https://user:pass@platform.leafdesign.ai', `${origin}/?token=value`, `${origin}/?%61uthorization=value`, `${origin}/?q=${policy.token}`, `${origin}/#fragment`, `${origin}/%zz`, `${origin}/\n`]) assert.equal(decideRequest({ url, method: 'GET' }, policy).allowed, false)
  for (const entry of ['*.example.test', 'http://static.example.test', `${external}/`, `${external}/path`, `https://user@static.example.test`]) assert.equal(decideRequest({ url: external, method: 'GET' }, { origin, externalOrigins: [entry] }).allowed, false)
  for (const url of ['https://@platform.leafdesign.ai', `${origin}/%0A`]) assert.equal(decideRequest({ url, method: 'GET' }, policy).allowed, false)
})

test('browser guard is installed before page creation', async () => {
  const fake = double()
  const ledger = denialLedger()
  const session = await createGuardedContext(fake.browser, policy, ledger)
  await session.context.newPage()
  assert.deepEqual(fake.order, ['sw-block', 'http', 'websocket', 'page'])
  // Context-wide handlers also see popup, iframe and unload beacon requests.
  for (const kind of ['popup', 'frame', 'beacon']) {
    const { route, sent } = routeDouble(`${origin}/${kind}`, 'POST')
    await fake.handlers.http(route)
    assert.deepEqual(sent, [{ kind: 'abort' }])
  }
  let connected = 0
  let closed = 0
  fake.handlers.websocket({ connectToServer() { connected += 1 }, close() { closed += 1 } })
  assert.equal(connected, 0)
  assert.equal(closed, 1)
  fake.context.close = async () => {
    const { route } = routeDouble(`${origin}/unload`, 'POST')
    await fake.handlers.http(route)
    fake.order.push('close')
  }
  await session.close()
  assert.equal(fake.order.at(-1), 'close')
  assert.equal(ledger.snapshot().METHOD, 4)
  assert.equal(ledger.snapshot().WEBSOCKET, 1)
  assert.throws(() => ledger.assertClean())
})

test('api facades cannot bypass policy', async () => {
  const fake = double()
  const ledger = denialLedger()
  const session = await createGuardedContext(fake.browser, policy, ledger)
  const page = await session.context.newPage()
  assert.equal(session.request, session.context.request)
  assert.equal(session.request, page.request)
  assert.equal(page.context(), session.context)
  assert.equal(page._apiName, 'Page')
  assert.deepEqual(session.context._options, { baseURL: origin })
  assert.ok(Object.isFrozen(session.context._options))
  assert.equal(page.mainFrame().page(), page)
  assert.equal(page.locator('body').page(), page)
  assert.equal(page.locator('body')._apiName, 'Locator')
  assert.deepEqual(await page.locator('body')._expect(), { matches: true })
  assert.throws(() => page.locator('body').page().route(), /^Error: PROD_POLICY_ESCAPE$/)
  assert.throws(() => page.locator('body')._frame, /^Error: PROD_POLICY_ESCAPE$/)
  assert.equal(page.mainFrame().page().request, session.request)
  assert.throws(() => page.mainFrame().page().route(), /^Error: PROD_POLICY_ESCAPE$/)
  await page.waitForResponse((result) => {
    assert.throws(() => result.frame().page().context().browser(), /^Error: PROD_POLICY_ESCAPE$/)
    return true
  })
  assert.throws(() => page._browserContext, /^Error: PROD_POLICY_ESCAPE$/)
  for (const api of [session.request, session.context.request, page.request]) {
    for (const verb of ['post', 'put', 'patch', 'delete']) await assert.rejects(api[verb]('/write'), /^Error: PROD_POLICY_METHOD$/)
    await assert.rejects(api.fetch('/write', { method: 'POST', maxRedirects: 999 }), /^Error: PROD_POLICY_METHOD$/)
    await assert.rejects(api.get('/write', { method: 'DELETE' }), /^Error: PROD_POLICY_METHOD$/)
    await assert.rejects(api.get('/read', { data: 'body' }), /^Error: PROD_POLICY_METHOD$/)
    assert.throws(() => api.connect('/write'), /^Error: PROD_POLICY_ESCAPE$/)
    assert.throws(() => api.newContext(), /^Error: PROD_POLICY_ESCAPE$/)
    assert.throws(() => api.dispose(), /^Error: PROD_POLICY_ESCAPE$/)
  }
  for (const object of [page, page.context()]) for (const name of ['route', 'routeFromHAR', 'routeWebSocket', 'unroute', 'unrouteAll', 'newCDPSession', 'browser']) assert.throws(() => object[name](), /^Error: PROD_POLICY_ESCAPE$/)
  assert.throws(() => page.screenshot(), /^Error: PROD_POLICY_ARTIFACT$/)
  assert.throws(() => page.video(), /^Error: PROD_POLICY_ARTIFACT$/)
  assert.throws(() => page.pdf(), /^Error: PROD_POLICY_ARTIFACT$/)
  assert.throws(() => session.context.tracing.start(), /^Error: PROD_POLICY_ARTIFACT$/)
  assert.equal(fake.calls.length, 0)
  await session.close()
  assert.throws(() => ledger.assertClean())
  const offline = double()
  const offlineLedger = denialLedger()
  const offlineSession = await createGuardedContext(offline.browser, policy, offlineLedger, { offline: true })
  await assert.rejects(offlineSession.request.get('/read'), /^Error: PROD_POLICY_TRANSPORT$/)
  assert.equal(offline.calls.length, 0)
  await offlineSession.close()
})

test('redirects and external authorization fail closed', async () => {
  const calls = []
  const ledger = denialLedger()
  const api = createRequestFacade({ fetch: async (url, options) => { calls.push({ url, options }); return response(url.endsWith('/redirect') ? 302 : 200) } }, policy, ledger)
  await assert.rejects(api.get(`${origin}/redirect`, { maxRedirects: 12 }), /^Error: PROD_POLICY_REDIRECT$/)
  await assert.rejects(api.get('https://evil.example.test/target'), /^Error: PROD_POLICY_ORIGIN$/)
  for (const auth of ['', 'Bearer invalid']) await api.get(`${origin}/public`, { headers: { Authorization: auth }, maxRedirects: 99 })
  assert.equal(calls[1].options.headers.authorization, '')
  assert.equal(calls[2].options.headers.authorization, 'Bearer invalid')
  await api.get(`${external}/read`, { headers: { AuThOrIzAtIoN: `Bearer ${policy.token}` } })
  assert.ok(!Object.keys(calls.at(-1).options.headers).some((key) => key.toLowerCase() === 'authorization'))
  assert.ok(calls.every(({ options }) => options.maxRedirects === 0))
  assert.equal(calls.filter(({ url }) => url.includes('evil.example.test')).length, 0)
  const fake = double()
  await installBrowserGuard(fake.context, policy, ledger)
  for (const destination of [origin, external]) {
    const { route, sent } = routeDouble(`${destination}/redirect`, 'GET', { AUTHORIZATION: `Bearer ${policy.token}`, Authorization: 'Bearer invalid' }, 307)
    await fake.handlers.http(route)
    assert.deepEqual(sent.map(({ kind }) => kind), ['fetch', 'abort'])
    assert.equal(sent[0].options.maxRedirects, 0)
    if (destination === external) assert.ok(!Object.keys(sent[0].options.headers).some((key) => key.toLowerCase() === 'authorization'))
    else assert.ok(Object.hasOwn(sent[0].options.headers, 'authorization'))
  }
  assert.equal(ledger.snapshot().REDIRECT, 3)
  assert.throws(() => ledger.assertClean())
})
