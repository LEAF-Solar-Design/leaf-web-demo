import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const entries = buildFeatureMap().entries
if (process.env.LEAF_WALK_PROOF === '1') {
  test.describe('W1z B1 state recipes', () => {
    test.describe.configure({ mode: 'default' })
    for (const [featureId, state, viewport] of [
      ['action:solar-panels-array-rect', 'no-drawing', 'desktop'],
      ['action:solar-panels-move', 'no-drawing', 'desktop'],
      ['action:solar-panels-rotate', 'no-drawing', 'desktop'],
      ['action:solar-panels-create-rectangle', 'no-drawing', 'desktop'],
      ['surface:browser', 'signed-out', 'desktop'],
      ['surface:solar', 'no-drawing', 'desktop'],
      ['surface:solar', 'solar-not-ready', 'desktop'],
      ['action:author-tool', 'ready', 'desktop'],
      ['action:author-tool', 'unentitled', 'desktop'],
      ['action:properties-pane', 'pane-open', 'phone'],
      ['action:rail-expand', 'ready', 'phone'],
    ]) {
      test(`${featureId} [${state}] B1 @${viewport}`, async ({ page, stack, walkEvidence }, testInfo) => {
        const entry = entries.find((entry) => entry.id === featureId)
        expect(entry, featureId).toBeTruthy()
        const probe = resolveProbe(entry, state)
        let result
        let failure
        try { result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo }) }
        catch (error) { failure = error }
        await testInfo.attach('w1z-states-proof', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
          featureId, state, viewport, recipe: probe.setup, expected: probe.assertion,
          effectiveEffect: walkEvidence.effectiveEffect, setup: walkEvidence.setupCompleted,
          oracleReached: walkEvidence.oracleReached, cleanupCompleted: walkEvidence.cleanupCompleted,
          solarDocument: walkEvidence.solarDocument, solarReadiness: walkEvidence.solarReadiness,
          heldProjection: walkEvidence.solarProjectionRequests, surfaceHost: walkEvidence.surfaceHost,
          authorAvailability: walkEvidence.authorAvailability, authorPolicy: walkEvidence.authorPolicy,
          phoneProperties: walkEvidence.phoneProperties, phoneRail: walkEvidence.phoneRail, result,
          failure: failure ? { message: failure.message, expected: failure.matcherResult?.expected,
            observed: failure.matcherResult?.actual } : null,
        })) })
        if (failure) throw failure
        expect(walkEvidence.cleanupCompleted).toBe(true)
        if (featureId === 'action:author-tool' && state === 'ready') {
          expect(result?.unsupported).toBe(true)
          expect(walkEvidence.authorAvailability.policy.availability.author_stage).toBe(false)
          expect(walkEvidence.authorAvailability.configuration.LEAF_CUSTOMIZATION_R5_MODE).toBe('off')
          expect(result.reason).toContain('LEAF_CUSTOMIZATION_R5_MODE=off')
          expect(walkEvidence.oracleReached).toBeUndefined()
        } else if (featureId === 'action:rail-expand' && viewport === 'phone') {
          expect(result?.unsupported).toBe(true)
          expect(walkEvidence.phoneRail.rendered).toBe(0)
          expect(walkEvidence.phoneRail.disclosure).toBe('true')
          expect(result.reason).toContain('wideViewport')
          expect(walkEvidence.oracleReached).toBeUndefined()
        } else {
          expect(result?.unsupported, result?.reason).not.toBe(true)
          expect(walkEvidence.setupCompleted).toBeTruthy()
          expect(walkEvidence.oracleReached).toBe(probe.assertion.assertionId)
          if (walkEvidence.solarDocument) {
            expect(walkEvidence.solarDocument.documentId).toBe('solar-starter.dxf')
            expect(walkEvidence.solarDocument.outline.enabled).toBe(true)
            if (featureId === 'action:solar-panels-create-rectangle') {
              expect(walkEvidence.solarDocument.originalAssertion).toBeTruthy()
              expect(walkEvidence.effectiveEffect.kind).toBe('opens')
            } else expect(walkEvidence.effectiveEffect.reason).toBe('select an entity in the drawing')
          }
          if (state === 'solar-not-ready') expect(walkEvidence.solarReadiness).toMatchObject({ pending: 'Beta', released: 'Ready' })
          if (state === 'unentitled') {
            expect(walkEvidence.authorPolicy.policyResponse.entitlements.build).toBe(false)
            expect(walkEvidence.authorPolicy.afterDisclosure.visible).toBe(true)
          }
          if (viewport === 'phone') expect(walkEvidence.phoneProperties).toEqual({ host: 'Plan', before: true, after: false })
        }
      })
    }
  })
}
