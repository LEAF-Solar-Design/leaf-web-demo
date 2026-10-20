import { isDeepStrictEqual } from 'node:util'

const smoke = new Set(['action:fit', 'surface:sheets', 'drawer:nav', 'tab:drafting:view', 'tool:count-by-layer'])

export function collectProbeCases(entries, resolveProbe) {
  const cases = { ordinary: [], postgres: [] }
  for (const entry of entries) {
    for (const state of entry.states) {
      const probe = resolveProbe(entry, state)
      const workerOptions = probe.workerOptions
      let target
      if (workerOptions == null || isDeepStrictEqual(workerOptions, {})) target = cases.ordinary
      else if (isDeepStrictEqual(workerOptions, { walkPostgres: true })) target = cases.postgres
      else throw new Error(`Unsupported worker options for ${entry.id} [${state}]: ${JSON.stringify(workerOptions)}; add a dedicated top-level spec for this option set`)
      target.push({ entry, state, probe })
    }
  }
  return cases
}

// Expand at collection time. Each project selects its own viewport suffix,
// rather than running every definition once in each project and doubling it.
export function declareProbes(test, runProbe, cases) {
  for (const { entry, state, probe } of cases) {
    for (const viewport of entry.state_viewports?.[state] || entry.viewports) {
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
          if (probe.setup?.context?.projectSolar && probe.setup.context.projectSolar !== 'proposal-offline') testInfo.setTimeout(300_000)
          await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo })
        })
      }
    }
  }
}
