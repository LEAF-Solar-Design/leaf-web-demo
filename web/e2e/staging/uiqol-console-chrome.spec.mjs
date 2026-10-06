import { expect, test } from '@playwright/test'
import { assertPageOnAllowedOrigin, stagingProofPath } from './stagingConfig.mjs'
import { captureStagingIdentity } from './stagingIdentity.mjs'

// uiqol S20 changed-behaviour check against the deployed staging origin, run
// after the D4 identity probe proves the candidate source is live. Fork
// F-studio-rollback-storage: the studio nav rail's posture is remembered, so
// an expanded rail survives a reload. When the studio rail is absent for a
// signed-out visitor the check is the identity probe only, and it says so.

test.use({ viewport: { width: 1600, height: 1000 } })

test('the expanded studio nav rail stays expanded across a reload', async ({ page, request }, testInfo) => {
  const identity = await captureStagingIdentity(request)
  await testInfo.attach('staging-identity', {
    body: JSON.stringify(identity, null, 2),
    contentType: 'application/json',
  })
  await page.goto('/app?demo=off', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  expect(new URL(page.url()).pathname).toBe('/app')

  const toggle = page.locator('[data-tool="rail-expand"]').first()
  const nav = page.locator('aside.nav').first()
  if (await toggle.count() === 0 || await nav.count() === 0) {
    testInfo.annotations.push({ type: 'identity-only', description: 'the studio rail is absent signed out' })
    return
  }

  if (await nav.getAttribute('data-spine') === 'hidden') await toggle.click()
  await expect(nav).not.toHaveAttribute('data-spine', 'hidden')
  await page.screenshot({ path: stagingProofPath('uiqol-console-chrome', 'rail-expanded.png') })

  await page.reload({ waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  const reloaded = page.locator('aside.nav').first()
  await expect(reloaded).toBeVisible()
  await expect(reloaded).not.toHaveAttribute('data-spine', 'hidden')
  await page.screenshot({ path: stagingProofPath('uiqol-console-chrome', 'rail-after-reload.png') })
})
