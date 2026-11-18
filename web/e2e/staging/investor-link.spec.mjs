import { test, expect } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { isInvestorToken } from '../../src/investorDemo.js'
import { INVESTOR_LINK_PATTERN } from '../../src/lib/investorLinkCapture.js'
import { assertAllowedStagingHost, DEFAULT_STAGING_BASE_URL } from './stagingConfig.mjs'

// Traces contain request bodies and response headers. The only evidence this
// walk retains is the set of forbidden API paths.
test.use({ trace: 'off', screenshot: 'off', video: 'off', storageState: { cookies: [], origins: [] } })
const file = process.env.LEAF_E2E_INVESTOR_LINK_FILE
test.skip(!file, 'Set LEAF_E2E_INVESTOR_LINK_FILE to a private staging link file.')

const prefix = DEFAULT_STAGING_BASE_URL + '/app#investor='
const exchangePath = '/api/site/investor-demo/session'
let token
test.beforeEach(async ({ baseURL }) => {
  if (assertAllowedStagingHost(baseURL).origin !== DEFAULT_STAGING_BASE_URL) {
    throw new Error('Investor links require the fixed staging origin.')
  }
  const value = (await readFile(file, 'utf8')).replace(/\r?\n$/, '')
  if (!value.startsWith(prefix)) throw new Error('Invalid staging investor link file.')
  token = value.slice(prefix.length)
  if (!INVESTOR_LINK_PATTERN.test(token)) {
    throw new Error('Invalid staging investor link file.')
  }
})
test.afterEach(() => { token = undefined })

// A goto step embeds its URL in the HTML report and navigation errors. Start
// the navigation from the blank document. Only its main frame can retrieve
// the URL, exactly once, without putting it in any reported step arguments.
async function openLink(page, value) {
  let taken = false
  await page.exposeBinding('takeWalkAddress', ({ frame }) => {
    if (taken || frame !== page.mainFrame() || frame.url() !== 'about:blank') throw new Error('Unexpected link request.')
    taken = true
    return prefix + value
  })
  await page.evaluate(async () => {
    const address = await window.takeWalkAddress()
    setTimeout(() => location.replace(address), 0)
  })
  value = undefined
}

test('a fresh visitor reaches the signed-in console within five seconds', async ({ page }, info) => {
  const refused = new Set()
  const unauthorized = new Set()
  page.on('response', response => {
    const { pathname } = new URL(response.url())
    if (!pathname.startsWith('/api/')) return
    const safePath = pathname.includes(token) ? '[redacted]' : pathname
    if (response.status() === 401) unauthorized.add(safePath)
    if (response.status() === 403) refused.add(safePath)
  })
  const identity = page.waitForResponse(response => {
    const url = new URL(response.url())
    return url.origin === DEFAULT_STAGING_BASE_URL && url.pathname === '/api/session/identity' && response.status() === 200
  }, { timeout: 5000 })
  try {
    await Promise.all([identity, openLink(page, token),
      expect(page.getByText('View-only demo', { exact: true })).toBeVisible({ timeout: 5000 })])
    const address = new URL(page.url())
    expect(address.origin === DEFAULT_STAGING_BASE_URL && address.pathname === '/app').toBe(true)
    expect(address.href.includes('#investor') || address.href.includes(token)).toBe(false)
    expect(await page.evaluate(() => {
      try {
        const parts = localStorage.getItem('leaf.jwt').split('.')
        const claims = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')))
        return parts.length === 3 && claims.iss === 'https://investor-demo.leaf.invalid/staging/'
      } catch { return false }
    })).toBe(true)
    expect([...unauthorized]).toEqual([])
  } finally {
    await info.attach('403 paths during boot', {
      body: Buffer.from(JSON.stringify([...refused].sort())), contentType: 'application/json',
    })
  }
})

test('a modified link opens the signed-out gate', async ({ page }) => {
  const modified = token.slice(0, -1) + (token.endsWith('A') ? 'Q' : 'A')
  await openLink(page, modified)
  await expect(page.getByText('This demo link has expired or is no longer valid. Sign in to continue.', {
    exact: true,
  })).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem('leaf.jwt') === null)).toBe(true)
})

async function request(path, init) {
  return fetch(DEFAULT_STAGING_BASE_URL + path, { ...init, credentials: 'omit', redirect: 'error', cache: 'no-store' })
}

test('the session refuses unrelated objects, writes and other drawings', async () => {
  const exchange = await request(exchangePath, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }),
  })
  expect(exchange.status).toBe(200)
  const { session } = await exchange.json()
  expect(isInvestorToken(session)).toBe(true)
  const headers = { Authorization: `Bearer ${session}` }
  for (const [method, path] of [
    ['GET', '/api/projects/' + randomUUID()], ['GET', '/api/jobs/' + randomUUID()],
    ['POST', '/api/run'], ['POST', '/api/orgs'], ['GET', '/api/session?dwg=not-the-sample'],
  ]) {
    const result = await request(path, { method, headers })
    expect([403, 404], method + ' ' + path).toContain(result.status)
  }
})

test('the exchange token cannot serve as an API bearer', async () => {
  const result = await request('/api/session/identity', { headers: { Authorization: `Bearer ${token}` } })
  expect(result.status).toBe(401)
})
