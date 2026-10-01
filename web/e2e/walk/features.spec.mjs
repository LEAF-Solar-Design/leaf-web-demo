import { test, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const smoke = new Set(['action:fit', 'surface:sheets', 'drawer:nav', 'tab:drafting:view', 'tool:count-panels'])

// Expand at collection time. Each project selects its own viewport suffix,
// rather than running every definition once in each project and doubling it.
for (const entry of buildFeatureMap().entries) {
  for (const state of entry.states) {
    const probe = resolveProbe(entry, state)
    for (const viewport of entry.viewports) {
      const title = `${entry.id} [${state}] @${viewport}`
      const options = { tag: smoke.has(entry.id) ? ['@smoke'] : [] }
      if (probe.certification) {
        test(title, options, async ({ walkEvidence }, testInfo) => {
          const result = { featureId: entry.id, state, ...probe.certification }
          walkEvidence.certification = result
          testInfo.annotations.push({ type: 'certification', description: result.result })
          await testInfo.attach('walk-certification', { body: Buffer.from(JSON.stringify(result)), contentType: 'application/json' })
          // Expected failure is a distinct Playwright outcome. A declared
          // exclusion neither passes an effect assertion nor skips a test.
          test.fail(true, result.reason)
          throw new Error(`CERTIFICATION_CLASS: ${result.result}: ${result.reason}`)
        })
      } else {
        test(title, options, async ({ page, stack, walkEvidence }, testInfo) => {
          await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo })
        })
      }
    }
  }
}
