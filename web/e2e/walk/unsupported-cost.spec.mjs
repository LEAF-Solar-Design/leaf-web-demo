import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const map = buildFeatureMap()
const probeFor = (id, state) => resolveProbe(map.entries.find((entry) => entry.id === id), state)

test.describe('unsupported probes preserve the worker stack', () => {
  test.describe.configure({ mode: 'serial' })
  const completed = []

  test.afterEach(async ({}, testInfo) => {
    completed.push(testInfo)
    expect(testInfo.status).toBe('passed')
    if (completed.length === 1) {
      expect(testInfo.annotations).toContainEqual({ type: 'unsupported_local',
        description: 'The production bundle has no mounted browser editing engine' })
    }
  })

  test('unsupported action:bar-escape [selection-present] @desktop', async ({ page, stack, walkEvidence }, testInfo) => {
    const result = await runProbe(probeFor('action:bar-escape', 'selection-present'),
      { page, stack, evidence: walkEvidence, testInfo })
    expect(result).toEqual({ unsupported: true,
      reason: 'The production bundle has no mounted browser editing engine' })
  })

  test('next control:session-details [ready] @desktop', async ({ page, stack, walkEvidence }, testInfo) => {
    expect(completed).toHaveLength(1)
    const first = completed[0].attachments.find((attachment) => attachment.name === 'walk-evidence')
    expect(first).toBeTruthy()
    const evidence = JSON.parse(first.body.toString('utf8'))
    expect(evidence.result.result).toBe('unsupported_local')
    expect(evidence.stack.ready).toBe(true)
    expect(walkEvidence.stack.instance).toBe(evidence.stack.instance)
    const result = await runProbe(probeFor('control:session-details', 'ready'),
      { page, stack, evidence: walkEvidence, testInfo })
    if (result?.unsupported) return
    expect(walkEvidence.result.result).toBe('passed')
  })

  test.afterAll(async () => {
    expect(completed).toHaveLength(2)
    const evidence = completed.map((info) => JSON.parse(info.attachments
      .find((attachment) => attachment.name === 'walk-evidence').body.toString('utf8')))
    expect(evidence[1].result.result).toBe('passed')
    expect(evidence[1].stack.instance).toBe(evidence[0].stack.instance)
  })
})
