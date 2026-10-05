import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const entries = buildFeatureMap().entries
if (process.env.LEAF_WALK_PROOF === '1') {
  test.describe('W1z G1 Solar forms', () => {
    for (const [featureId, state] of [
      ['tool:solar-unit-sync', 'ready'],
      ['tool:solar-design-presets', 'ready'],
    ]) {
      test(`${featureId} [${state}] G1 @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
        const entry = entries.find((entry) => entry.id === featureId)
        expect(entry, featureId).toBeTruthy()
        const probe = resolveProbe(entry, state)
        let result
        let failure
        try { result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo }) }
        catch (error) { failure = error }
        await testInfo.attach('w1z-solar-forms-proof', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
          featureId, state, recipe: probe.setup, expected: probe.assertion,
          setup: walkEvidence.setupCompleted, oracleReached: walkEvidence.oracleReached,
          cleanupCompleted: walkEvidence.cleanupCompleted, browserCatalog: walkEvidence.browserCatalog,
          solarStepEditor: walkEvidence.solarStepEditor,
          result, failure: failure ? { message: failure.message } : null,
        })) })
        if (failure) throw failure
        expect(result?.unsupported, result?.reason).not.toBe(true)
        expect(probe.assertion.target).toBe('solar-step-editor')
        expect(walkEvidence.oracleReached).toBe(probe.assertion.assertionId)
        expect(walkEvidence.cleanupCompleted).toBe(true)
        expect(walkEvidence.solarStepEditor.runRequests).toEqual([])
      })
    }
  })
}
