import { test as base, expect } from '@playwright/test'
import { startStack } from '../../walk/stack.mjs'

export { expect }

export function unsupportedLocal(reason) {
  const message = String(reason).replace(/[\r\n]+/g, ' ').trim()
  throw new Error(`UNSUPPORTED_LOCAL: ${message || 'Feature is unavailable on the local stack'}`)
}

export const test = base.extend({
  stack: [async ({}, use, workerInfo) => {
    const firstSlot = Number(process.env.LEAF_REGRESSION_SLOT || process.env.LEAF_WALK_TEST_SLOT || 0)
    if (!Number.isInteger(firstSlot) || firstSlot < 0) throw new Error('LEAF_REGRESSION_SLOT must be a nonnegative integer')
    let stack
    try {
      // parallelIndex survives Playwright worker replacement after a failure.
      stack = await startStack({ slot: firstSlot + workerInfo.parallelIndex })
    } catch (error) {
      if (error.code === 'QUEUED' || error.code === 'STOPPED') {
        throw new Error(`${error.code}: ${String(error.message).replace(/[\r\n]+/g, ' ')}`, { cause: error })
      }
      throw error
    }
    try { await use(stack) } finally { await stack.stop() }
  }, { scope: 'worker', timeout: 240_000 }],
  // Playwright's automatic artifact fixture resolves baseURL even without a
  // page. Bind it at context creation so unsupportedLocal can fail before boot.
  context: async ({ stack, _contextFactory }, use) => {
    const { context, close } = await _contextFactory({ baseURL: stack.baseURL })
    try { await use(context) } finally { await close() }
  },
})
