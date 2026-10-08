import { test, expect, setupStep, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

if (process.env.LEAF_WALK_PROOF === '1') {
  test('tool:solar-unit-sync [ready] recovers its failed browser catalog once @desktop',
    async ({ page, stack, walkEvidence }, testInfo) => {
      const entry = buildFeatureMap().entries.find((entry) => entry.id === 'tool:solar-unit-sync')
      expect(entry).toBeTruthy()
      const probe = resolveProbe(entry, 'ready')
      const pattern = '**/api/capabilities*'
      let failures = 0, retryClicks = 0, faultRemoved = false
      const failCatalog = async (route) => {
        failures += 1
        await route.abort('failed')
      }
      await page.route(pattern, failCatalog)
      // Observe real pointer clicks without changing the product's Retry handler.
      await page.exposeFunction('recordCatalogRetry', () => { retryClicks += 1 })
      await page.addInitScript(() => {
        document.addEventListener('click', (event) => {
          const button = event.target.closest?.('button')
          const status = button?.closest('[role="status"]')
          if (button?.textContent.trim() === 'Retry' && status?.textContent.startsWith("Couldn't load tools:")) {
            window.recordCatalogRetry()
          }
        }, true)
      })
      const runtime = { page, stack, evidence: walkEvidence, testInfo,
        runStep: (name, callback) => test.step(name, async () => {
          if (name === 'Setup: catalog-tool' && !faultRemoved) {
            await setupStep(probe, runtime, { kind: 'ribbon-tab', name: 'Solar' })
            const status = page.getByRole('toolbar', { name: 'Drafting tools', exact: true })
              .getByRole('status').filter({ hasText: /^Couldn't load tools:/ })
            await expect(status).toBeVisible({ timeout: 15_000 })
            await expect(status.getByRole('button', { name: 'Retry', exact: true })).toBeVisible()
            expect(failures).toBeGreaterThan(0)
            await page.unroute(pattern, failCatalog)
            faultRemoved = true
          }
          await callback()
        }) }
      let result, failure
      try { result = await runProbe(probe, runtime) }
      catch (error) { failure = error }
      finally {
        await page.unroute(pattern, failCatalog)
        await testInfo.attach('w1z-catalog-recovery-proof', { contentType: 'application/json',
          body: Buffer.from(JSON.stringify({ featureId: probe.featureId, state: probe.state,
            failures, faultRemoved, retryClicks, catalogRecovery: walkEvidence.catalogRecovery,
            oracleReached: walkEvidence.oracleReached, cleanupCompleted: walkEvidence.cleanupCompleted,
            result, failure: failure ? { message: failure.message } : null })) })
      }
      if (failure) throw failure
      expect(result?.unsupported, result?.reason).not.toBe(true)
      expect(faultRemoved).toBe(true)
      expect(walkEvidence.catalogRecovery.retried).toBe(true)
      expect(walkEvidence.catalogRecovery.reason).toMatch(/^Couldn't load tools:/)
      await expect.poll(() => retryClicks).toBe(1)
      expect(walkEvidence.oracleReached).toBe(probe.assertion.assertionId)
      expect(walkEvidence.result.result).toBe('passed')
      expect(walkEvidence.cleanupCompleted).toBe(true)
    })
}
