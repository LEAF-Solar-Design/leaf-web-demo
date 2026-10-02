import { test, expect, runProbe, setupStep } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'
import { collectProbeUxEvidence } from './uxEvidence.mjs'

const detailsEntry = buildFeatureMap().entries.find(entry => entry.id === 'control:session-details')
const probe = resolveProbe(detailsEntry, 'ready')
const shell = '.studio-shell .app[data-studio-shell="cockpit"][data-surface="cad"]'

async function openReady(page, stack, evidence, testInfo) {
  await page.setViewportSize({ width: 1280, height: 800 })
  // Use only the ordinary probe's setup: Details has never been activated.
  const runtime = { page, stack, evidence, testInfo, cleanup: [] }
  for (const recipe of probe.setup.steps) await setupStep(probe, runtime, recipe)
  await expect(page.getByRole('dialog', { name: 'Session · provenance', exact: true })).toBeHidden()
  return page.locator(`${shell} header.top`).getByRole('button', { name: 'Details', exact: true })
}

test('ux-evidence uncovered mapped Details at rest @desktop', async ({ page, stack, walkEvidence }, testInfo) => {
  const locator = await openReady(page, stack, walkEvidence, testInfo)
  // No action on the target precedes this sample after the setup settles.
  const observations = await collectProbeUxEvidence(probe, locator, 'desktop')
  expect(observations.map(row => [row.metric_id, row.observed])).toEqual([
    ['control_covered_at_rest', 0], ['scroll_needed_steps', 0],
  ])
})

test('ux-evidence real Details feature probe attaches observations @desktop', async ({ page, stack, walkEvidence }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo })
  expect(walkEvidence.ux_observations).toEqual(expect.arrayContaining([
    expect.objectContaining({ lens_id: 'reachability', metric_id: 'control_covered_at_rest', observed: 0 }),
    expect.objectContaining({ lens_id: 'reachability', metric_id: 'scroll_needed_steps', observed: 0 }),
  ]))
})

test('ux-evidence opaque overlay covers mapped Details at rest @desktop', async ({ page, stack, walkEvidence }, testInfo) => {
  const locator = await openReady(page, stack, walkEvidence, testInfo)
  const rect = await locator.evaluate(element => {
    const { x, y, width, height } = element.getBoundingClientRect()
    return { x, y, width, height }
  })
  await page.evaluate(rect => {
    const overlay = document.createElement('div')
    overlay.id = 'ux-evidence-cover'
    Object.assign(overlay.style, { position: 'fixed', left: `${rect.x}px`, top: `${rect.y}px`,
      width: `${rect.width}px`, height: `${rect.height}px`, background: '#000', zIndex: '2147483647' })
    document.body.appendChild(overlay)
  }, rect)
  const observations = await collectProbeUxEvidence(probe, locator, 'desktop')
  expect(observations.find(row => row.metric_id === 'control_covered_at_rest')?.observed).toBe(1)
  expect(observations.find(row => row.metric_id === 'scroll_needed_steps')?.observed).toBe(0)
})
