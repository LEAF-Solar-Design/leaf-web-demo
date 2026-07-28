import { expect, test } from '@playwright/test'
import { captureStagingIdentity } from './stagingIdentity.mjs'
import { assertPageOnAllowedOrigin, stagingProofPath } from './stagingConfig.mjs'
import { writeProofReceipt } from '../proofReceipt.mjs'

test('the command-bar shortcut and the center-stage reduced-motion pin hold on the deployed surface', async ({ page, request, baseURL }) => {
  // Navigation + the bounded entrance-settle allowance + the dense stability
  // window can legitimately exceed Playwright's 30s default.
  test.setTimeout(90_000)
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
  const reducedMotionFlipAt = Date.now()
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

  // The stage entrance (.stage-viewer's one-time condense-in, --recast-enter
  // = 1730ms in landing.css) starts when the viewer reports ready, which is
  // before this test flips the media emulation, and a CSS transition that has
  // already started keeps its original duration when transition-* later
  // changes (CSS Transitions L1). A real reduced-motion user carries the
  // preference from first paint, so the entrance never animates for them.
  // That legitimate tail gets a BOUNDED allowance of 4s measured FROM THE
  // FLIP (1730ms transform leg + a late `.in` + scheduling latency). The
  // canvas is sampled continuously from here THROUGH the boundary until at
  // least 600ms past it, with no early exit, and every sample taken at or
  // after the boundary must be bit-for-bit identical: motion that starts
  // late, pauses, or resumes past the boundary is observed and fails.
  // Limitation (also mirrored in the receipt): motion confined entirely to
  // the pre-boundary allowance is geometrically indistinguishable from the
  // entrance tail and is not detected.
  const boundaryMs = 4_000
  const samples = []
  const windowEnd = Math.max(reducedMotionFlipAt + boundaryMs + 600, Date.now() + 600)
  // Each sample costs ~150-200ms (boundingBox round trip + the 100ms pause),
  // so keep sampling until the window has passed AND at least six samples
  // landed on or past the boundary; the hard stop only trips if sampling
  // itself degrades, and then the count assertion below fails loud.
  const hardStop = reducedMotionFlipAt + 10_000
  let settledCount = 0
  while ((Date.now() < windowEnd || settledCount < 6) && Date.now() < hardStop) {
    const at = Date.now() - reducedMotionFlipAt
    samples.push({ at, box: await canvas.boundingBox() })
    if (at >= boundaryMs) settledCount += 1
    await page.waitForTimeout(100)
  }
  const settledSamples = samples.filter((sample) => sample.at >= boundaryMs)
  expect(settledSamples.length).toBeGreaterThanOrEqual(6)
  const canvasBoxBefore = settledSamples[settledSamples.length - 1].box
  expect(canvasBoxBefore).not.toBeNull()
  expect(canvasBoxBefore.width).toBeGreaterThan(0)
  expect(canvasBoxBefore.height).toBeGreaterThan(0)
  for (const sample of settledSamples) {
    expect(sample.box, `canvas moved ${sample.at}ms after the reduced-motion flip (allowance is ${boundaryMs}ms)`).toEqual(canvasBoxBefore)
  }

  const columnSelectors = ['.tc-rail-l', '.tc-bar-wrap', '.tc-rail-r']
  const boxesBefore = {}
  for (const selector of columnSelectors) {
    boxesBefore[selector] = await page.locator(selector).first().boundingBox()
    expect(boxesBefore[selector]).not.toBeNull()
  }

  // Dense sampling: a canvas read every 100ms across the 600ms window, so
  // periodic motion with any period above ~200ms cannot alias past the pin
  // (the endpoint-only comparison this replaces could miss it).
  for (let sample = 1; sample <= 6; sample += 1) {
    await page.waitForTimeout(100)
    expect(await canvas.boundingBox(), `canvas moved ${sample * 100}ms into the settle window`).toEqual(canvasBoxBefore)
  }

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
      'The pin grants a bounded 4s post-flip allowance for the tail of the pre-flip stage entrance (a running CSS transition keeps its original duration when the media preference changes mid-flight); the canvas is sampled continuously through that boundary and must hold bit-for-bit from the boundary onward, but motion confined entirely to the pre-boundary allowance is indistinguishable from the entrance tail and is not detected.',
    ],
  })
})
