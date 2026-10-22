// Reviewed exact origins only. No environment override and no implicit sibling hosts.
export const READ_ONLY_EXTERNAL_ORIGINS = Object.freeze([])
const READS = new Set(['GET', 'HEAD', 'OPTIONS'])
const CODES = new Set(['METHOD', 'URL', 'ORIGIN', 'REDIRECT', 'WEBSOCKET', 'ESCAPE', 'TRANSPORT', 'ARTIFACT'])

export function denialLedger() {
  const counts = Object.create(null)
  return Object.freeze({
    record(code) { const safe = CODES.has(code) ? code : 'TRANSPORT'; counts[safe] = (counts[safe] || 0) + 1 },
    snapshot() { return { ...counts } },
    assertClean() { if (Object.keys(counts).length) throw new Error('PROD_POLICY_DENIED') },
  })
}

export function decideRequest({ url, method }, policy) {
  if (!READS.has(method)) return { allowed: false, code: 'METHOD' }
  let parsed
  try {
    if (typeof url !== 'string' || !/^https:\/\/[^/?#]+/iu.test(url) || /[\s\x00-\x1f\x7f]/u.test(url)) throw new Error()
    parsed = new URL(url)
    const decoded = decodeURIComponent(url)
    if (parsed.protocol !== 'https:' || parsed.username || parsed.password || /^https:\/\/[^/?#]*@/iu.test(url) || parsed.hash
        || /[\x00-\x1f\x7f]/u.test(decoded)
        || /(?:token|authorization|bearer|jwt|api[_-]?key|secret)/iu.test(decoded)
        || (policy.token && decoded.includes(policy.token))) throw new Error()
  } catch { return { allowed: false, code: 'URL' } }
  const external = policy.externalOrigins ?? READ_ONLY_EXTERNAL_ORIGINS
  const reviewed = external.filter((value) => {
    try { const origin = new URL(value); return origin.protocol === 'https:' && origin.origin === value && !origin.username && !origin.password } catch { return false }
  })
  if (parsed.origin !== policy.origin && !reviewed.includes(parsed.origin)) return { allowed: false, code: 'ORIGIN' }
  return { allowed: true, sameOrigin: parsed.origin === policy.origin }
}

function refuse(ledger, code) {
  ledger.record(code)
  throw new Error(`PROD_POLICY_${code}`)
}

function headersFor(headers, policy, sameOrigin, browser = false) {
  const clean = {}
  let authorization
  for (const [name, value] of Object.entries(headers || {})) {
    if (name.toLowerCase() === 'authorization') authorization = value
    else clean[name] = value
  }
  if (sameOrigin && browser && policy.authenticated) clean.authorization = `Bearer ${policy.token}`
  if (sameOrigin && !browser && authorization !== undefined) clean.authorization = authorization
  return clean
}

function responseFacade(raw, ledger) {
  const facade = Object.create(null)
  for (const name of ['status', 'statusText', 'ok', 'url', 'headers', 'headersArray', 'body', 'text', 'json', 'dispose']) {
    if (typeof raw[name] === 'function') facade[name] = raw[name].bind(raw)
  }
  return new Proxy(Object.freeze(facade), {
    get(target, key) {
      if (key === 'then' || typeof key === 'symbol') return undefined
      if (Object.hasOwn(target, key)) return target[key]
      return () => refuse(ledger, 'ESCAPE')
    },
  })
}

export function createRequestFacade(raw, policy, ledger) {
  const facade = Object.create(null)
  for (const verb of ['fetch', 'get', 'head', 'options', 'post', 'put', 'patch', 'delete']) facade[verb] = async (input, options = {}) => {
    const method = verb === 'fetch' ? (options.method ?? 'GET') : verb.toUpperCase()
    let url
    try {
      if (!READS.has(method) || (options.method !== undefined && !READS.has(options.method))) refuse(ledger, 'METHOD')
      if (typeof input !== 'string' || /[\s\x00-\x1f\x7f]/u.test(input) || /^https:\/\/[^/?#]*@/iu.test(input)) throw new Error()
      url = new URL(input, policy.origin)
      if (options.params) {
        for (const [key, value] of new URLSearchParams(options.params)) url.searchParams.append(key, value)
      }
    } catch (error) {
      if (error.message === 'PROD_POLICY_METHOD') throw error
      refuse(ledger, 'URL')
    }
    const decision = decideRequest({ url: url.href, method }, policy)
    if (!decision.allowed) refuse(ledger, decision.code)
    if (['data', 'form', 'multipart'].some((name) => options[name] !== undefined)) refuse(ledger, 'METHOD')
    let response
    try {
      response = await raw.fetch(url.href, {
        method, headers: headersFor(options.headers, policy, decision.sameOrigin),
        maxRedirects: 0, failOnStatusCode: false,
        ...(options.timeout === undefined ? {} : { timeout: options.timeout }),
      })
    } catch { refuse(ledger, 'TRANSPORT') }
    if (response.status() >= 300 && response.status() < 400) refuse(ledger, 'REDIRECT')
    return responseFacade(response, ledger)
  }
  return new Proxy(Object.freeze(facade), {
    get(target, key) {
      if (key === 'then' || typeof key === 'symbol') return undefined
      if (Object.hasOwn(target, key)) return target[key]
      return () => refuse(ledger, 'ESCAPE')
    },
  })
}

export async function installBrowserGuard(context, policy, ledger, { offline = false } = {}) {
  await context.route('**/*', async (route) => {
    const sent = route.request()
    const decision = decideRequest({ url: sent.url(), method: sent.method() }, policy)
    if (!decision.allowed) {
      ledger.record(decision.code)
      await route.abort('blockedbyclient')
      return
    }
    if (offline) { await route.fulfill({ status: 200, contentType: 'text/html', body: '<html><body>offline</body></html>' }); return }
    try {
      const response = await route.fetch({ headers: headersFor(sent.headers(), policy, decision.sameOrigin, true), maxRedirects: 0 })
      if (response.status() >= 300 && response.status() < 400) {
        ledger.record('REDIRECT')
        await route.abort('blockedbyclient')
      } else await route.fulfill({ response })
    } catch (error) {
      // A closed target cannot be an unreviewed transport; it is teardown.
      const isTargetClosed = (error) => error?.name === 'TargetClosedError' || (typeof error?.message === 'string'
        && /^(?:route\.(?:fetch|fulfill|abort): )?Target page, context or browser has been closed(?:$|\n)/u.test(error.message))
      if (isTargetClosed(error)) {
        try { await route.abort('blockedbyclient') } catch (abortError) {
          if (!isTargetClosed(abortError)) {
            ledger.record('TRANSPORT')
            throw abortError
          }
        }
        return
      }
      ledger.record('TRANSPORT')
      await route.abort('blockedbyclient')
    }
  })
  await context.routeWebSocket('**/*', (socket) => {
    ledger.record('WEBSOCKET')
    socket.close()
  })
}

// Raw contexts remain in this closure. All exposed API handles use one policy.
export async function createGuardedContext(browser, policy, ledger, options = {}) {
  const raw = await browser.newContext({
    serviceWorkers: 'block', storageState: { cookies: [], origins: [] },
    acceptDownloads: false, viewport: { width: 1600, height: 1000 },
  })
  try { await installBrowserGuard(raw, policy, ledger, options) } catch { await raw.close(); refuse(ledger, 'TRANSPORT') }
  const transport = options.offline ? { fetch: async () => { throw new Error('PROD_OFFLINE') } } : raw.request
  const request = createRequestFacade(transport, policy, ledger)
  const pages = new WeakMap()
  const handles = new WeakMap()
  const matcherOptions = Object.freeze({ baseURL: policy.origin })
  let context
  const forbidden = new Set(['route', 'unroute', 'unrouteAll', 'routeFromHAR', 'routeWebSocket', 'newCDPSession', 'browser', 'setExtraHTTPHeaders', 'storageState'])
  const privateEscape = (key) => typeof key === 'string' && key.startsWith('_') && !['_apiName', '_expect', '_selector'].includes(key)
  function protect(value) {
    if (value instanceof Promise) return value.then(protect)
    if (!value || typeof value !== 'object') return value
    if (Array.isArray(value)) return value.map(protect)
    if (value === raw) return context
    if (pages.has(value)) return pages.get(value)
    // Playwright 1.60.0 bundles client classes as _Page, _Frame, _Locator, etc.
    // Normalize those names so returned handles cannot expose a raw page.
    const type = value.constructor?.name.replace(/^_/u, '')
    if (type === 'Page') return wrapPage(value)
    if (['Browser', 'BrowserType', 'BrowserContext', 'APIRequestContext', 'CDPSession'].includes(type)) refuse(ledger, 'ESCAPE')
    if (!['Frame', 'Locator', 'FrameLocator', 'ElementHandle', 'JSHandle', 'Request', 'Response'].includes(type)) return value
    if (handles.has(value)) return handles.get(value)
    const wrapped = new Proxy(value, {
      get(target, key) {
        if (key === 'screenshot') return () => refuse(ledger, 'ARTIFACT')
        if (privateEscape(key)) return refuse(ledger, 'ESCAPE')
        if (forbidden.has(key)) return () => refuse(ledger, 'ESCAPE')
        const member = Reflect.get(target, key, target)
        return typeof member === 'function' && key !== 'constructor'
          ? (...args) => protect(member.apply(target, args)) : protect(member)
      },
    })
    handles.set(value, wrapped)
    return wrapped
  }
  function wrapPage(page) {
    if (pages.has(page)) return pages.get(page)
    const wrapped = new Proxy(page, {
      get(target, key) {
        if (key === 'request') return request
        if (key === 'context') return () => context
        if (key === 'screenshot' || key === 'video' || key === 'pdf') return () => refuse(ledger, 'ARTIFACT')
        if (privateEscape(key)) return refuse(ledger, 'ESCAPE')
        if (forbidden.has(key)) return () => refuse(ledger, 'ESCAPE')
        if (key === 'opener') return async () => { const opener = await target.opener(); return opener ? wrapPage(opener) : null }
        if (key === 'waitForEvent') return async (name, ...args) => { const result = await target.waitForEvent(name, ...args); return name === 'popup' ? wrapPage(result) : protect(result) }
        if (key === 'waitForResponse' || key === 'waitForRequest') return (predicate, ...args) => protect(target[key](
          typeof predicate === 'function' ? (...items) => predicate(...items.map(protect)) : predicate, ...args,
        ))
        if (key === 'on' || key === 'once') return (name, callback) => { target[key](name, (...args) => callback(...(name === 'popup' ? args.map(wrapPage) : args.map(protect)))); return wrapped }
        const value = Reflect.get(target, key, target)
        return typeof value === 'function' && key !== 'constructor' ? (...args) => protect(value.apply(target, args)) : protect(value)
      },
    })
    pages.set(page, wrapped)
    return wrapped
  }
  context = new Proxy(raw, {
    get(target, key) {
      if (key === 'request') return request
      // toHaveURL reads this field. Expose only the immutable, admitted base URL.
      if (key === '_options') return matcherOptions
      if (key === 'tracing') return new Proxy({}, { get: () => () => refuse(ledger, 'ARTIFACT') })
      if (privateEscape(key)) return refuse(ledger, 'ESCAPE')
      if (forbidden.has(key)) return () => refuse(ledger, 'ESCAPE')
      if (key === 'newPage') return async () => wrapPage(await target.newPage())
      if (key === 'pages') return () => target.pages().map(wrapPage)
      if (key === 'on' || key === 'once') return (name, callback) => { target[key](name, (...args) => callback(...(name === 'page' ? args.map(wrapPage) : args.map(protect)))); return context }
      const value = Reflect.get(target, key, target)
      return typeof value === 'function' && key !== 'constructor' ? (...args) => protect(value.apply(target, args)) : protect(value)
    },
  })
  return Object.freeze({ context, request, close: () => raw.close() })
}
