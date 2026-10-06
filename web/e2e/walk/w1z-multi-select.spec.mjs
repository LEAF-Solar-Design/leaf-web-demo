import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const entries = buildFeatureMap().entries
if (process.env.LEAF_WALK_PROOF === '1') {
  test.describe('W1z G4 selection-set verbs', () => {
    for (const op of ['delete', 'move', 'copy', 'rotate', 'scale', 'mirror']) {
      const featureId = `action:modify-${op}`
      const state = 'multiple-selected'
      test(`${featureId} [${state}] G4 @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
        const entry = entries.find((entry) => entry.id === featureId)
        expect(entry, featureId).toBeTruthy()
        const probe = resolveProbe(entry, state)
        expect(probe.assertion.kind).toBe(op === 'delete' ? 'submits' : 'opens')
        let result
        let failure
        try { result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo }) }
        catch (error) { failure = error }
        await testInfo.attach('w1z-multi-select-proof', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
          featureId, state, recipe: probe.setup, expected: probe.assertion,
          setup: walkEvidence.setupCompleted, oracleReached: walkEvidence.oracleReached,
          cleanupCompleted: walkEvidence.cleanupCompleted, multipleSelection: walkEvidence.multipleSelection,
          result, failure: failure ? { message: failure.message } : null,
        })) })
        if (failure) throw failure
        expect(result?.unsupported, result?.reason).not.toBe(true)
        expect(walkEvidence.oracleReached).toBe(probe.assertion.assertionId)
        expect(walkEvidence.cleanupCompleted).toBe(true)
        expect(walkEvidence.multipleSelection.selectionCount).toBe(2)
        if (op === 'delete') {
          expect(walkEvidence.multipleSelection.after).toBe(walkEvidence.multipleSelection.before - 2)
          expect(walkEvidence.multipleSelection.restored).toBe(walkEvidence.multipleSelection.before)
          expect(walkEvidence.multipleSelection.undoSteps).toBe(1)
        } else expect(walkEvidence.multipleSelection.promptArmed).toBe(true)
      })
    }
  })
}
