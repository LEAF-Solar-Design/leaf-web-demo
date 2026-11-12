import { test as base, expect } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import { startStack } from '../../walk/stack.mjs'
import { installIdentity } from '../project/projectIdentity.mjs'

export { expect }

export const test = base.extend({
  stack: [async ({}, use, workerInfo) => {
    const firstSlot = Number(process.env.LEAF_REGRESSION_SLOT || process.env.LEAF_WALK_TEST_SLOT || 0)
    if (!Number.isInteger(firstSlot) || firstSlot < 0) throw new Error('LEAF_REGRESSION_SLOT must be a nonnegative integer')
    let stack
    try {
      stack = await startStack({ slot: firstSlot + workerInfo.parallelIndex, identity: true, postgres: 'ephemeral' })
    } catch (error) {
      if (error.code === 'QUEUED' || error.code === 'STOPPED' || error.code === 'postgres_binaries_missing') {
        const marker = error.code === 'postgres_binaries_missing' ? 'UNSUPPORTED_LOCAL' : error.code
        throw new Error(`${marker}: ${String(error.message).replace(/[\r\n]+/g, ' ')}`, { cause: error })
      }
      throw error
    }
    try { await use(stack) } finally { await stack.stop() }
  }, { scope: 'worker', timeout: 300_000 }],
  context: async ({ stack, _contextFactory }, use) => {
    const { context, close } = await _contextFactory({ baseURL: stack.baseURL, serviceWorkers: 'block' })
    try { await use(context) } finally { await close() }
  },
  signedInUser: async ({ stack, page }, use) => {
    const subject = `test-identity|${randomUUID()}`
    const bootstrap = stack.identity.mint({ subject })
    const created = await page.request.post(`${stack.baseURL}/api/orgs`, {
      headers: { Authorization: `Bearer ${bootstrap}` }, data: { name: `Signed-in test ${randomUUID()}` },
    })
    expect(created.ok()).toBe(true)
    const { org } = await created.json()
    expect(org.org_id).toBeTruthy()
    const bearer = stack.identity.mint({ subject, tenantId: org.org_id, orgId: org.org_id, tier: 'hosted_free' })
    await page.addInitScript(installIdentity, { origin: stack.baseURL, bearer, organizationId: org.org_id })
    await use({ subject, orgId: org.org_id, bearer,
      headers: { Authorization: `Bearer ${bearer}`, 'X-Org-Id': org.org_id } })
  },
})
