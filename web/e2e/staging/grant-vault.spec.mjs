import { randomBytes } from 'node:crypto'
import { writeFileSync } from 'node:fs'
import { expect, test } from '@playwright/test'
import {
  allowedStagingOrigins,
  assertAllowedStagingHost,
  assertResponseOnAllowedOrigin,
  assertStagingTokenIdentity,
  PRODUCTION_HOSTS,
  resolveStagingBaseURL,
} from './stagingConfig.mjs'

let token, origin

function stagingURL(path) {
  const url = new URL(path, origin)
  if (url.username || url.password || PRODUCTION_HOSTS.includes(url.hostname) ||
      !allowedStagingOrigins().has(url.origin.toLowerCase())) {
    throw new Error('Grant vault proof refuses credentials in URLs or a non-staging origin')
  }
  return url
}

// Node fetch keeps the real Authorization header out of Playwright traces.
async function call(path, options = {}) {
  const url = stagingURL(path)
  let response
  try {
    response = await fetch(url, {
      ...options,
      redirect: 'error',
      signal: AbortSignal.timeout(15_000),
      headers: {
        Authorization: `Bearer ${token}`,
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      },
    })
  } catch {
    throw new Error('Grant vault API request failed or timed out')
  }
  assertResponseOnAllowedOrigin({ url: () => response.url })
  return response
}

function accountIds(body) {
  return new Set((Array.isArray(body.accounts) ? body.accounts : [])
    .map((account) => account?.id)
    .filter((id) => typeof id === 'string' && id.length > 0))
}

test.describe('R10 GE staging grant vault round trip', () => {
  test.describe.configure({ mode: 'serial', timeout: 120_000 })

  // Environment checks and all network activity happen only during execution.
  test('canary round trip uses vault v3 without echoing the grant', async () => {
    token = process.env.LEAF_E2E_STAGING_JWT || ''
    if (!token.trim()) {
      test.skip(true, 'LEAF_E2E_STAGING_JWT is not set; the grant vault proof needs a staging tenant owner credential')
    }
    assertStagingTokenIdentity(token, process.env)
    const base = assertAllowedStagingHost(resolveStagingBaseURL())
    if (base.username || base.password || PRODUCTION_HOSTS.includes(base.hostname)) {
      throw new Error('Grant vault proof refuses production hosts or URL credentials')
    }
    origin = base.origin

    const canary = 'ge-vault-canary-' + randomBytes(24).toString('hex')
    let diagnostic_backend = null
    let diagnostic_record_format = null
    let token_echoed = false

    async function json(path, options) {
      const response = await call(path, options)
      let text
      try {
        text = await response.text()
      } catch {
        throw new Error('Grant vault API response body could not be read')
      }
      // Inspect error bodies too, before either parsing or asserting status.
      token_echoed = token_echoed || text.includes(canary)
      let body
      try {
        body = JSON.parse(text)
      } catch {
        throw new Error('Grant vault API did not return valid JSON')
      }
      expect(response.status, 'Grant vault API status').toBe(200)
      if (!body || typeof body !== 'object' || Array.isArray(body)) {
        throw new Error('Grant vault API did not return a JSON object')
      }
      return body
    }

    try {
      const prior = await json('/api/tenant/claude-grant')
      const prior_active = typeof prior.active_account_id === 'string' ? prior.active_account_id : null
      const priorIds = accountIds(prior)
      const linked = await json('/api/tenant/claude-grant', {
        method: 'POST',
        body: JSON.stringify({ token: canary, kind: 'oauth', label: 'ge-vault-proof' }),
      })
      const addedIds = [...accountIds(linked)].filter((id) => !priorIds.has(id))
      // Assert only the count, so a failure cannot print account data.
      expect(addedIds.length, 'Grant vault link adds exactly one account').toBe(1)
      const canaryId = addedIds[0]
      try {
        const diagnostic = await json('/api/tenant/claude-grant/diagnostic')
        diagnostic_backend = typeof diagnostic.backend === 'string' ? diagnostic.backend : null
        diagnostic_record_format = typeof diagnostic.record_format === 'string' ? diagnostic.record_format : null
      } finally {
        // Never issue an unscoped DELETE: only remove the account just added.
        const removed = await json(`/api/tenant/claude-grant?account_id=${encodeURIComponent(canaryId)}`, {
          method: 'DELETE',
        })
        if (prior_active && removed.active_account_id !== prior_active) {
          await json('/api/tenant/claude-grant', {
            method: 'PATCH', body: JSON.stringify({ account_id: prior_active }),
          })
        }
        const restored = await json('/api/tenant/claude-grant')
        expect(accountIds(restored).has(canaryId), 'Grant vault canary account was removed').toBe(false)
        if (prior_active) {
          expect(restored.active_account_id === prior_active, 'Grant vault prior active account was restored').toBe(true)
        }
      }
    } finally {
      const receiptPath = process.env.LEAF_GE_RECEIPT_PATH
      if (typeof receiptPath === 'string' && receiptPath.trim()) {
        const receipt = {
          writer: 'web/e2e/staging/grant-vault.spec.mjs',
          diagnostic_backend,
          diagnostic_record_format,
          token_echoed,
          staging_origin: origin,
          written_at: new Date().toISOString(),
        }
        writeFileSync(receiptPath, JSON.stringify(receipt, null, 2) + '\n', 'utf8')
      }
    }

    // Compare booleans so assertion output never prints response field values.
    expect(diagnostic_backend === 'vault', 'Grant vault diagnostic reports the vault backend').toBe(true)
    expect(diagnostic_record_format === 'v3', 'Grant vault diagnostic reports the v3 record format').toBe(true)
    expect(token_echoed, 'Grant vault responses never echo the canary').toBe(false)
  })
})
