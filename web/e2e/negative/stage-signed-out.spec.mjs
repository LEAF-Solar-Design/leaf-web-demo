import { expect, test } from '@playwright/test'
import { installNegativeApi } from './negativeApiFixture.mjs'

// BLOCKER 1 (found and fixed 2026-09-04, standardization slice 5a): PromptBox
// mounted on the public, signed-out /try stage fired an unconditional mount
// effect calling the tenant-scoped, private /api/converse/mcp endpoint. This
// is the negative row scout section 3 calls for: a signed-out /try load must
// never make that call at all. installNegativeApi intercepts every request
// under http://leaf-proof.invalid/api/** (this config's VITE_API_BASE) and
// records each one in evidence.calls, so a call that never happened is
// provable, not merely unobserved.
test.describe('negative browser contracts: the stage', () => {
  test('a signed-out /try load makes no /api/converse/mcp request', async ({ page }) => {
    const { evidence } = await installNegativeApi(page)

    await page.goto('/try')
    await page.getByLabel('Command bar').waitFor()
    await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible()

    // Give the mount effect a full turn: if the fetch were unconditional it
    // would have fired and installNegativeApi would have logged it already.
    await page.waitForTimeout(500)

    expect(evidence.calls).not.toContain('GET /api/converse/mcp')
    expect(evidence.calls.some((entry) => entry.endsWith('/api/converse/mcp'))).toBe(false)
  })
})
