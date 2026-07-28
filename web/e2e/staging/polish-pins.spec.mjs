import { expect, test } from '@playwright/test'
import { join } from 'node:path'
import { captureStagingIdentity } from './stagingIdentity.mjs'
import { writeProofReceipt } from '../proofReceipt.mjs'

const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'staging', 'polish-pins')

test('the command-bar shortcut and the center-stage reduced-motion pin hold on the deployed surface', async ({ page, request, baseURL }) => {
  const identity = await captureStagingIdentity(request)
  const stagingOrigin = new URL(baseURL).origin
  const observedEndpoints = [identity.endpoint]
  page.on('response', (response) => {
    const url = new URL(response.url())
    if (url.origin === stagingOrigin) {
      observedEndpoints.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
    }
  })

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })

  // HP-01: the command bar keycap is not just a static label -- Control+K
  // (or Cmd+K) actually moves focus to the command-bar input
  // (web/src/site/SiteRoot.jsx ~line 110-113), and that works without a
  // session. Proving the shortcut fires is stronger evidence than reading
  // the keycap text alone.
  const keycap = page.locator('.tc-bar-key')
  await expect(keycap).toBeVisible()
  await expect(keycap).toHaveText('⌘K')
  const commandInput = page.locator('.tc-bar-input')
  await expect(commandInput).not.toBeFocused()
  await page.keyboard.press('Control+k')
  await expect(commandInput).toBeFocused()

  // MO-01: under a forced prefers-reduced-motion preference, the CENTER
  // STAGE (StageLayer's canvas mounts at main.stage-root, not the left
  // workspace rail) stays painted with a non-zero-area canvas, and the
  // three-column grid geometry does not drift or reflow across a settle
  // window. A blank, animated, or reflowing stage fails this.
  const stageRoot = page.locator('main.stage-root')
  await expect(stageRoot).toBeVisible()
  const canvas = stageRoot.locator('canvas').first()
  await expect(canvas).toBeVisible({ timeout: 15_000 })
  const canvasBoxBefore = await canvas.boundingBox()
  expect(canvasBoxBefore).not.toBeNull()
  expect(canvasBoxBefore.width).toBeGreaterThan(0)
  expect(canvasBoxBefore.height).toBeGreaterThan(0)

  const readStageStyle = () => stageRoot.evaluate((element) => {
    const computed = getComputedStyle(element)
    return { opacity: computed.opacity, animationDuration: computed.animationDuration }
  })
  const stageStyleBefore = await readStageStyle()
  expect(stageStyleBefore.animationDuration).toBe('0s')

  const columnSelectors = ['.tc-rail-l', '.tc-bar-wrap', '.tc-rail-r']
  const boxesBefore = {}
  for (const selector of columnSelectors) {
    boxesBefore[selector] = await page.locator(selector).first().boundingBox()
    expect(boxesBefore[selector]).not.toBeNull()
  }

  await page.waitForTimeout(600)

  const canvasBoxAfter = await canvas.boundingBox()
  expect(canvasBoxAfter).toEqual(canvasBoxBefore)
  const stageStyleAfter = await readStageStyle()
  expect(stageStyleAfter).toEqual(stageStyleBefore)
  for (const selector of columnSelectors) {
    const boxAfter = await page.locator(selector).first().boundingBox()
    expect(boxAfter).toEqual(boxesBefore[selector])
  }

  const screenshotPath = join(PROOF_DIR, 'polish-pins.png')
  await page.screenshot({ path: screenshotPath, fullPage: true })
  const videoPath = await page.video()?.path().catch(() => null)
  const artifacts = [screenshotPath, ...(videoPath ? [videoPath] : [])].map((p) => p.replaceAll('\\', '/'))

  writeProofReceipt(join(PROOF_DIR, 'receipt.json'), {
    capability_ids: ['HP-01', 'MO-01'],
    evidence_tier: 'staging',
    route: '/try',
    runtime: 'deployed staging origin, real browser with prefers-reduced-motion: reduce, no request interception',
    source_commit: identity.source_revision,
    api_endpoints: [...new Set(observedEndpoints)],
    assertions: [
      'the command bar keycap rendered the exact shortcut label without a session',
      'pressing Control+K actually moved focus onto the command-bar input',
      'the center-stage canvas painted with a non-zero area',
      'the center-stage element animation duration was zero seconds under forced reduced motion',
      'the canvas geometry, stage computed style, and the three workspace grid columns were bit-for-bit stable across a 600ms settle window',
    ],
    artifacts,
    result: {
      verdict: 'pass',
      canvas_box: canvasBoxBefore,
      stage_computed_style: stageStyleBefore,
      observed_source_revision: identity.source_revision,
      observed_ready: identity.ready,
    },
    limitations: [
      'This does not exercise a completed run under reduced motion (MO-02), because reaching a completed result requires an active session on the deployed surface.',
      'HP-01 is proven for the command-bar keycap and its focus behavior only, not hover/focus hints elsewhere or the first-run coach.',
      'The center-stage canvas rendered here is the public landing/demo scene (StageLayer loads a fixed rooftop intake), not a real authenticated drawing.',
    ],
  })
})
