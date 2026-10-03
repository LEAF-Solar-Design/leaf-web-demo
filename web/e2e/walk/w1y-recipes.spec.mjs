import { test, expect, runProbe, CATALOG_SOLAR_FLAG_REASON } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const entries = buildFeatureMap().entries
if (process.env.LEAF_WALK_PROOF === '1') {
  test.describe('W1y drawing readiness and recipes', () => {
    test.describe.configure({ mode: 'default' })
    for (const [featureId, state] of [
      ['tool:solar-settings', 'ready'],
      // tool:solar-autofill [ready] waits on the Solar producer adapters.
      ['action:modify-copy', 'placed-dimension'],
      ['action:solar-panels-move', 'ready'],
      ['action:solar-panels-array-rect', 'no-drawing'],
      ['surface:browser', 'ready'],
      ['action:bar-escape', 'job-running'],
    ]) {
      test(`${featureId} [${state}] W1y @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
        const entry = entries.find((entry) => entry.id === featureId)
        expect(entry, featureId).toBeTruthy()
        const probe = resolveProbe(entry, state)
        let failure
        let result
        try { result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo }) }
        catch (error) { failure = error }
        await testInfo.attach('w1y-recipes-proof', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
          featureId, state, assertionId: probe.assertion.assertionId,
          setup: walkEvidence.setupCompleted, oracleReached: walkEvidence.oracleReached,
          cleanupCompleted: walkEvidence.cleanupCompleted, solarSeed: walkEvidence.solarSeed,
          buildFlags: walkEvidence.buildFlags, disabledReason: walkEvidence.disabledReason,
          result, productVerdict: failure ? { message: failure.message } : null,
        })) })
        if (featureId === 'tool:solar-settings' && walkEvidence.buildFlags?.VITE_SOLAR_SETTINGS_FORM !== '1') {
          if (failure) throw failure
          expect(result?.unsupported).toBe(true)
          expect(result.reason).toBe(CATALOG_SOLAR_FLAG_REASON)
          expect(walkEvidence.result.flags).toEqual(walkEvidence.buildFlags)
          expect(walkEvidence.steps).toHaveLength(0)
          expect(walkEvidence.cleanupCompleted).toBe(true)
          return
        }
        expect(result?.unsupported, result?.reason).not.toBe(true)
        expect(walkEvidence.setupCompleted, failure?.message).toBeTruthy()
        expect(walkEvidence.cleanupCompleted, failure?.message).toBe(true)
        expect(walkEvidence.oracleReached, failure?.message).toBe(probe.assertion.assertionId)
        if (failure) {
          // Only the named oracle's assertion can be a product verdict.
          // Setup, activation, runner and cleanup failures stay unexpected.
          if (!failure.matcherResult || walkEvidence.failure?.message !== failure.message) throw failure
          const description = `${probe.assertion.assertionId}: ${failure.message}`
          testInfo.annotations.push({ type: 'product_verdict', description })
          if (featureId === 'action:solar-panels-array-rect') {
            expect(walkEvidence.disabledReason.expected.join('\n')).toContain(probe.assertion.reason)
            expect(walkEvidence.disabledReason.observed).toContain('select an entity in the drawing')
            console.log('ORACLE:', description, JSON.stringify(walkEvidence.disabledReason))
            return
          }
          test.fail(true, description)
          throw failure
        }
      })
    }
  })
}
