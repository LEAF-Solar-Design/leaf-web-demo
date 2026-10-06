import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const entries = buildFeatureMap().entries
if (process.env.LEAF_WALK_PROOF === '1') {
  test.describe('W1z E4 engine history and keyboard Repeat', () => {
    for (const [featureId, state] of [
      ['action:engine-undo', 'ready'],
      ['action:engine-undo', 'engine-nothing-to-undo'],
      ['action:engine-redo', 'ready'],
      ['action:engine-repeat', 'ready'],
      ['action:engine-repeat', 'no-command-to-repeat'],
      ['action:engine-repeat', 'engine-crashed'],
    ]) {
      test(`${featureId} [${state}] E4 @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
        const entry = entries.find((entry) => entry.id === featureId)
        expect(entry, featureId).toBeTruthy()
        const probe = resolveProbe(entry, state)
        let result
        let failure
        try { result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo }) }
        catch (error) { failure = error }
        await testInfo.attach('w1z-engine-history-proof', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
          featureId, state, recipe: probe.setup, expected: probe.assertion,
          setup: walkEvidence.setupCompleted, oracleReached: walkEvidence.oracleReached,
          cleanupCompleted: walkEvidence.cleanupCompleted, repeatIgnored: walkEvidence.repeatIgnored,
          repeatRefusal: walkEvidence.repeatRefusal, repeatDispatches: walkEvidence.repeatDispatches,
          result, failure: failure ? { message: failure.message } : null,
        })) })
        if (failure) throw failure
        expect(result?.unsupported, result?.reason).not.toBe(true)
        expect(walkEvidence.oracleReached).toBe(probe.assertion.assertionId)
        expect(walkEvidence.cleanupCompleted).toBe(true)
        if (featureId === 'action:engine-repeat') {
          expect(walkEvidence.repeatDispatches.after).toEqual(walkEvidence.repeatDispatches.before)
          if (state === 'engine-crashed') expect(walkEvidence.repeatIgnored).toBe(true)
          if (state === 'no-command-to-repeat') expect(walkEvidence.repeatRefusal?.text).toBe(probe.assertion.reason)
          if (state === 'no-command-to-repeat') expect(typeof walkEvidence.repeatRefusal?.visible).toBe('boolean')
        }
      })
    }
  })
}
