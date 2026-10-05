import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { CENSUS_RECIPE_CONTROLS, resolveProbe } from './probes.mjs'

const entries = buildFeatureMap().entries
if (process.env.LEAF_WALK_PROOF === '1') {
  test.describe('W1z C2 census recipes', () => {
    for (const featureId of CENSUS_RECIPE_CONTROLS) {
      const entry = entries.find((entry) => entry.id === featureId)
      for (const state of entry.states) {
        test(`${featureId} [${state}] C2 @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
          expect(entry, featureId).toBeTruthy()
          const probe = resolveProbe(entry, state)
          let result
          let failure
          try { result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo }) }
          catch (error) { failure = error }
          await testInfo.attach('w1z-census-proof', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
            featureId, state, recipe: probe.setup, expected: probe.assertion,
            setup: walkEvidence.setupCompleted, oracleReached: walkEvidence.oracleReached,
            cleanupCompleted: walkEvidence.cleanupCompleted, disclosure: walkEvidence.censusDisclosure,
            script: walkEvidence.censusScript, edit: walkEvidence.censusEdit,
            save: walkEvidence.censusSave, downloadOnly: walkEvidence.censusDownloadOnly,
            filePicker: walkEvidence.censusFilePicker,
            result, failure: failure ? { message: failure.message } : null,
          })) })
          if (failure) throw failure
          expect(result?.unsupported, result?.reason).not.toBe(true)
          expect(walkEvidence.oracleReached).toBe(probe.assertion.assertionId)
          expect(walkEvidence.cleanupCompleted).toBe(true)
          for (const cleanup of [walkEvidence.censusDisclosure, walkEvidence.censusScript, walkEvidence.censusDownloadOnly, walkEvidence.censusFilePicker]) {
            if (cleanup) expect(cleanup.cleaned).toBe(true)
          }
        })
      }
    }
  })
}
