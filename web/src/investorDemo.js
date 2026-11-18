import { INVESTOR_LINK_PATTERN, takeInvestorLink } from './lib/investorLinkCapture.js'
import { serviceState } from './lib/serviceAuthSession.js'

export const INVESTOR_ISSUER = 'https://investor-demo.leaf.invalid/staging/'
export const INVESTOR_NOTICE_KEY = 'leaf.investorLink.notice'
export const INVESTOR_ORG_KEY = 'leaf.investorDemo.org'
const JWT_KEY = 'leaf.jwt'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

// Presentation only. The staging API verifies the signature and the binding.
export function isInvestorToken(jwt) {
  if (typeof jwt !== 'string' || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(jwt)) return false
  try {
    const decoded = atob(jwt.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))
    const bytes = Uint8Array.from(decoded, char => char.charCodeAt(0))
    const claims = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes))
    return claims !== null && typeof claims === 'object' && !Array.isArray(claims)
      && Object.hasOwn(claims, 'iss') && claims.iss === INVESTOR_ISSUER
  } catch { return false }
}

// The ribbon asks once per tool. For browser storage an unchanged token costs
// one getItem and one comparison, with no decoding or allocation.
let lastToken = null
let lastInvestor = false
export function isInvestorSession(storage) {
  try {
    const token = (storage === undefined ? globalThis.localStorage : storage).getItem(JWT_KEY)
    if (token !== lastToken) {
      lastInvestor = isInvestorToken(token)
      lastToken = token
    }
    return lastInvestor
  } catch { return false }
}

export function consumeInvestorNotice(storage) {
  try {
    const tab = storage === undefined ? globalThis.sessionStorage : storage
    if (tab.getItem(INVESTOR_NOTICE_KEY) !== '1') return false
    tab.removeItem(INVESTOR_NOTICE_KEY)
    return true
  } catch { return false }
}

export function clearInvestorWorkspace(storage) {
  const org = storage.getItem(INVESTOR_ORG_KEY)
  if (org !== null && storage.getItem('leaf.org_id') === org) storage.removeItem('leaf.org_id')
  storage.removeItem(INVESTOR_ORG_KEY)
}

export async function bootstrapInvestorLink(options = {}) {
  const { win = globalThis.window, fetchImpl = globalThis.fetch, apiBase = '' } = options
  let timer
  let initialJwt
  let writtenJwt
  try {
    if (!win) return 'none'
    const token = Object.hasOwn(options, 'link') ? options.link : takeInvestorLink(win)
    if (token === undefined) return 'none'
    if (serviceState().mode) return 'kept-existing'
    initialJwt = win.localStorage.getItem(JWT_KEY)
    if (initialJwt && !isInvestorToken(initialJwt)) return 'kept-existing'
    if (!INVESTOR_LINK_PATTERN.test(token) || (apiBase !== '' && apiBase !== win.location.origin)) throw new Error()
    const controller = new AbortController()
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => { controller.abort(); reject(new Error()) }, 4000)
    })
    const exchange = async () => {
      const response = await fetchImpl(`${apiBase}/api/site/investor-demo/session`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }),
        credentials: 'omit', cache: 'no-store', redirect: 'error', signal: controller.signal,
      })
      if (response.status !== 200) throw new Error()
      return response.json()
    }
    const body = await Promise.race([exchange(), deadline])
    if (serviceState().mode || win.localStorage.getItem(JWT_KEY) !== initialJwt) return 'kept-existing'
    if (!isInvestorToken(body?.session) || typeof body.org_id !== 'string' || !UUID.test(body.org_id)
      || typeof body.expires_at !== 'string' || !(Date.parse(body.expires_at) > Date.now())) throw new Error()
    win.sessionStorage.removeItem(INVESTOR_NOTICE_KEY)
    win.localStorage.setItem(JWT_KEY, body.session)
    writtenJwt = body.session
    win.localStorage.setItem(INVESTOR_ORG_KEY, body.org_id)
    win.localStorage.setItem('leaf.org_id', body.org_id)
    return 'signed-in'
  } catch {
    try {
      if (serviceState().mode) return 'kept-existing'
      const current = win.localStorage.getItem(JWT_KEY)
      if (current && current !== initialJwt && current !== writtenJwt) return 'kept-existing'
      if (isInvestorToken(current)) {
        win.localStorage.removeItem(JWT_KEY)
        clearInvestorWorkspace(win.localStorage)
      }
    } catch { /* storage may be blocked */ }
    try { win.sessionStorage.setItem(INVESTOR_NOTICE_KEY, '1') } catch { /* storage may be blocked */ }
    return 'failed'
  } finally { clearTimeout(timer) }
}
