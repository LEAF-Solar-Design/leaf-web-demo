import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const entries = buildFeatureMap().entries
const setupTimes = []
test.describe('W1p real state recipes', () => {
  test.describe.configure({ mode: 'serial' })
  test.afterAll(() => {
    expect(setupTimes).toHaveLength(4)
    expect(setupTimes.reduce((sum, elapsed) => sum + elapsed, 0)).toBeLessThan(420_000)
  })
  for (const [featureId, state] of [
    ['tool:count-by-layer', 'read-only'],
    ['action:undo', 'read-only'],
    ['tool:count-by-layer', 'job-running'],
    ['action:bar-escape', 'job-running'],
  ]) {
    test(`${featureId} [${state}] W1p @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
      const probe = resolveProbe(entries.find((entry) => entry.id === featureId), state)
      let failure
      try {
        const result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo })
        expect(result?.unsupported).not.toBe(true)
      } catch (error) { failure = error }
      const errors = [failure?.message, ...walkEvidence.pageErrors.map((error) => error.message),
        ...walkEvidence.consoleErrors.map((error) => error.text)].filter(Boolean)
      expect(errors.join('\n')).not.toContain('Route is already handled')
      expect(walkEvidence.setupCompleted, failure?.message).toBeTruthy()
      expect(walkEvidence.cleanupCompleted, failure?.message).toBe(true)
      setupTimes.push(walkEvidence.setupCompleted.elapsedMs)
      if (state === 'read-only') {
        expect(walkEvidence.versionPreview.head).toBeGreaterThan(walkEvidence.versionPreview.preview)
      } else {
        expect(walkEvidence.pendingRun.status).toBe(202)
        expect(walkEvidence.pendingRun.pendingVisible).toBe(true)
        if (featureId === 'action:bar-escape' && !failure) expect(walkEvidence.pendingRun.detached).toBe(true)
      }
      await testInfo.attach('w1p-state-proof', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
        featureId, state, setup: walkEvidence.setupCompleted, preview: walkEvidence.versionPreview,
        pendingRun: walkEvidence.pendingRun, cleanupCompleted: walkEvidence.cleanupCompleted,
        productVerdict: failure ? { message: failure.message } : null,
      })) })
      if (failure) {
        // Only a product expectation after successful setup can be recorded as
        // an expected failure. Runner exceptions and cleanup errors still fail.
        if (!failure.matcherResult || walkEvidence.failure?.message !== failure.message) throw failure
        testInfo.annotations.push({ type: 'product_verdict', description: failure.message })
        test.fail(true, failure.message)
        throw failure
      }
    })
  }
})
