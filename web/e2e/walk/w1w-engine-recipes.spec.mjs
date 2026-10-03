import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const entries = buildFeatureMap().entries
if (process.env.LEAF_WALK_PROOF === '1') {
  test.describe('W1w real engine recipes', () => {
    test.describe.configure({ mode: 'serial' })
    for (const [featureId, state] of [
      ['action:author-tool', 'authoring-off'],
      ['action:draw-create-block', 'engine-busy'],
      ['action:draw-create-block', 'engine-not-parsed'],
      ['action:modify-set-color', 'engine-busy'],
      ['action:modify-set-color', 'no-selection'],
      ['action:draw-create-block', 'ready'],
      ['action:clipboard-copy-clip', 'ready'],
    ]) {
      test(`${featureId} [${state}] W1w @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
        const probe = resolveProbe(entries.find((entry) => entry.id === featureId), state)
        let failure
        try {
          const result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo })
          expect(result?.unsupported).not.toBe(true)
        } catch (error) { failure = error }
        const errors = [failure?.message,
          ...(walkEvidence.cleanupErrors || []).map((error) => error.message),
          ...walkEvidence.pageErrors.map((error) => error.message),
          ...walkEvidence.consoleErrors.map((error) => error.text)].filter(Boolean)
        expect(errors.join('\n')).not.toContain('Route is already handled')
        expect(walkEvidence.setupCompleted, failure?.message).toBeTruthy()
        expect(walkEvidence.cleanupCompleted, failure?.message).toBe(true)
        expect(walkEvidence.oracleReached, failure?.message).toBe(probe.assertion.assertionId)
        await testInfo.attach('w1w-engine-proof', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
          featureId, state, setup: walkEvidence.setupCompleted, oracleReached: walkEvidence.oracleReached,
          cleanupCompleted: walkEvidence.cleanupCompleted,
          productVerdict: failure ? { message: failure.message } : null,
        })) })
        if (failure) {
          if (!failure.matcherResult || walkEvidence.failure?.message !== failure.message) throw failure
          testInfo.annotations.push({ type: 'product_verdict', description: failure.message })
          test.fail(true, failure.message)
          throw failure
        }
      })
    }
  })
}
