import { afterEach, expect, it, vi } from 'vitest'
import { INVESTOR_ISSUER } from '../investorDemo.js'
import { createIntegrationsClient } from './client.js'

afterEach(() => localStorage.removeItem('leaf.jwt'))

it('refuses an investor session before sending a request to the integrations origin', async () => {
  const fetchImpl = vi.fn()
  const client = createIntegrationsClient({ origin: 'https://ops.example', fetchImpl })
  const payload = btoa(JSON.stringify({ iss: INVESTOR_ISSUER })).replace(/=+$/, '')
  localStorage.setItem('leaf.jwt', 'e30.' + payload + '.c2ln')
  await expect(client.getScope('12345678-1234-1234-1234-123456789abc', { signal: new AbortController().signal }))
    .rejects.toMatchObject({ status: 403, dispatched: false })
  expect(fetchImpl).not.toHaveBeenCalled()
})
