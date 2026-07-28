import { expect, test } from '@playwright/test'
import { captureStagingIdentity } from './stagingIdentity.mjs'
import { assertPageOnAllowedOrigin, stagingProofPath } from './stagingConfig.mjs'
import { writeProofReceipt } from '../proofReceipt.mjs'

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

  await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)

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

  // MO-01: use the caption, which has a nonzero opacity transition without
  // reduced motion, rather than the non-animated stage root. Then force the
  // preference and prove both animation and transition durations are zero.
  const motionTarget = page.locator('.tc-caption')
  await expect(motionTarget).toBeVisible()
  const readMotionStyle = () => motionTarget.evaluate((element) => {
    const computed = getComputedStyle(element)
    return { animationDuration: computed.animationDuration, transitionDuration: computed.transitionDuration }
  })
  const unrestrictedMotion = await readMotionStyle()
  expect(unrestrictedMotion.transitionDuration).not.toBe('0s')
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const reducedMotion = await readMotionStyle()
  expect(reducedMotion.animationDuration).toBe('0s')
  expect(reducedMotion.transitionDuration).toBe('0s')

  // The stage canvas is geometrically stable under reduced motion. WebGL
  // readback is unavailable in this read-only proof, so this does not claim
  // that visibility alone proves non-blank canvas pixels.
  const stageRoot = page.locator('main.stage-root')
  await expect(stageRoot).toBeVisible()
  const canvas = stageRoot.locator('canvas').first()
  await expect(canvas).toBeVisible({ timeout: 15_000 })
  const canvasBoxBefore = await canvas.boundingBox()
  expect(canvasBoxBefore).not.toBeNull()
  expect(canvasBoxBefore.width).toBeGreaterThan(0)
  expect(canvasBoxBefore.height).toBeGreaterThan(0)

  const columnSelectors = ['.tc-rail-l', '.tc-bar-wrap', '.tc-rail-r']
  const boxesBefore = {}
  for (const selector of columnSelectors) {
    boxesBefore[selector] = await page.locator(selector).first().boundingBox()
    expect(boxesBefore[selector]).not.toBeNull()
  }

  await page.waitForTimeout(600)

  const canvasBoxAfter = await canvas.boundingBox()
  expect(canvasBoxAfter).toEqual(canvasBoxBefore)
  const reducedMotionAfter = await readMotionStyle()
  expect(reducedMotionAfter).toEqual(reducedMotion)
  for (const selector of columnSelectors) {
    const boxAfter = await page.locator(selector).first().boundingBox()
    expect(boxAfter).toEqual(boxesBefore[selector])
  }

  const screenshotPath = stagingProofPath('polish-pins', 'polish-pins.png')
  await page.screenshot({ path: screenshotPath, fullPage: true })

  const common = {
    evidence_tier: 'staging',
    route: '/try',
    runtime: 'deployed staging origin, real browser with prefers-reduced-motion: reduce, no request interception',
    source_commit: identity.source_revision,
    api_endpoints: [...new Set(observedEndpoints)],
    artifacts: [screenshotPath],
    result: {
      verdict: 'pass',
      canvas_box: canvasBoxBefore,
      unrestricted_motion: unrestrictedMotion,
      reduced_motion: reducedMotion,
      observed_source_revision: identity.source_revision,
      observed_ready: identity.ready,
    },
  }
  writeProofReceipt(stagingProofPath('polish-pins', 'hp-01-receipt.json'), {
    ...common,
    capability_ids: ['HP-01'],
    sub_cases: { proven: ['shortcut labels'], not_proven: ['hover/focus hints', 'first-run coach'] },
    assertions: [
      'the command bar keycap rendered the exact shortcut label without a session',
      'pressing Control+K actually moved focus onto the command-bar input',
    ],
    limitations: ['The hover/focus hints and first-run coach sub-cases are not exercised.'],
  })
  writeProofReceipt(stagingProofPath('polish-pins', 'mo-01-receipt.json'), {
    ...common,
    capability_ids: ['MO-01'],
    sub_cases: { proven: ['allowed transitions', 'stable grid', 'zero duration preference'], not_proven: [] },
    assertions: [
      'the caption had a nonzero transition duration without reduced motion',
      'the caption animation duration was zero seconds under forced reduced motion',
      'the caption transition duration was zero seconds under forced reduced motion',
      'the canvas geometry and the three workspace grid columns were bit-for-bit stable across a 600ms settle window',
    ],
    limitations: [
      'Canvas non-blankness is not proven because this read-only WebGL proof does not use pixel readback.',
      'This does not exercise a completed run under reduced motion (MO-02), because reaching a completed result requires an active session on the deployed surface.',
    ],
  })
})
