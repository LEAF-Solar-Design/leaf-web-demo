import { expect, test } from '@playwright/test'
import { join } from 'node:path'
import { captureStagingIdentity } from './stagingIdentity.mjs'
import { writeProofReceipt } from '../proofReceipt.mjs'

const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'staging', 'polish-pins')

test('the command-bar shortcut keycap and reduced-motion shell pin hold on the deployed surface', async ({ page, request }) => {
  const identity = await captureStagingIdentity(request)

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })

  // HP-01: the command bar shows its keyboard-shortcut keycap without requiring
  // a signed-in session. This is a static hint pin, safe to read anonymously.
  const keycap = page.locator('.tc-bar-key')
  await expect(keycap).toBeVisible()
  await expect(keycap).toHaveText('⌘K')

  // MO-01: under a forced prefers-reduced-motion preference, the operator
  // shell renders fully painted (opacity 1) with animations collapsed to zero
  // duration, and the workspace grid stays intact -- no fill-mode snap, no
  // hidden panes. This holds for the signed-out shell, so it is provable
  // read-only.
  const surface = page.getByTestId('operator-surface')
  await expect(surface).toBeVisible()
  const style = await surface.evaluate((element) => {
    const computed = getComputedStyle(element)
    return { opacity: computed.opacity, animationDuration: computed.animationDuration }
  })
  expect(style.opacity).toBe('1')
  expect(style.animationDuration).toBe('0s')

  const grid = page.locator('#workspace-tabpanel')
  await expect(grid).toBeVisible()

  await page.screenshot({ path: join(PROOF_DIR, 'polish-pins.png'), fullPage: true })

  writeProofReceipt(join(PROOF_DIR, 'receipt.json'), {
    capability_ids: ['HP-01', 'MO-01'],
    evidence_tier: 'staging',
    route: '/try',
    runtime: 'deployed staging origin, real browser with prefers-reduced-motion: reduce, no request interception',
    api_endpoints: [],
    assertions: [
      'the command bar keycap hint rendered the exact shortcut label without a session',
      'the workspace shell painted at opacity 1 under forced reduced motion',
      'animation durations collapsed to zero seconds under forced reduced motion',
      'the workspace grid stayed visible and did not reflow into a hidden or empty state',
    ],
    artifacts: ['polish-pins.png'],
    result: {
      verdict: 'pass',
      computed_style: style,
      observed_source_revision: identity.source_revision,
      observed_ready: identity.ready,
    },
    limitations: [
      'This does not exercise a completed run under reduced motion (MO-02), because reaching a completed result requires an active session on the deployed surface.',
      'HP-01 is proven for the command-bar keycap only, not the full set of hover/focus hints or the first-run coach.',
    ],
  })
})
