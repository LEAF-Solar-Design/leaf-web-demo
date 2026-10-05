import { expect, test } from '@playwright/test'
import { assertPageOnAllowedOrigin, stagingProofPath } from './stagingConfig.mjs'

// uiqol S3 changed-behaviour check against the deployed staging origin, run
// after the D4 identity probe proves the candidate source is live. Chrome
// resolves env(safe-area-inset-*) to 0 on a desktop runner, so the safe-area
// proof is textual: the served stylesheet carries the inset. The OS modes are
// proven by rendering /try under each emulated preference with one screenshot
// each; reduced motion is proven on a pseudo-element, the case the old root
// rule (`*` only) missed.

const sheetText = (page) => page.evaluate(() => {
  const out = []
  for (const sheet of Array.from(document.styleSheets)) {
    let rules
    try { rules = sheet.cssRules } catch { continue } // cross-origin sheet
    for (const rule of Array.from(rules)) out.push(rule.cssText)
  }
  return out.join('\n')
})

test('the served index.html draws under the notch with viewport-fit=cover', async ({ page }) => {
  await page.goto('/try', { waitUntil: 'domcontentloaded', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  const content = await page.locator('meta[name="viewport"]').getAttribute('content')
  expect(content.split(',').map((s) => s.trim())).toContain('viewport-fit=cover')
})

test.describe('phone, touch', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('the shell stylesheet pads the dock, toast and foot bar with safe-area-inset-bottom', async ({ page }) => {
    await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
    assertPageOnAllowedOrigin(page)
    const css = await sheetText(page)
    expect(css).toContain('safe-area-inset-bottom')
    expect(css).toMatch(/\.bar-dock[^{}]*\{[^}]*safe-area-inset-bottom/)
    expect(css).toMatch(/\.toast\s*\{[^}]*safe-area-inset-bottom/)
    expect(css).toMatch(/footer\.foot-bar\s*\{[^}]*safe-area-inset-bottom/)
    await page.screenshot({ path: stagingProofPath('uiqol-shell-css', 'phone-390x844.png') })
  })
})

for (const [name, media] of [
  ['forced-colors', { forcedColors: 'active' }],
  ['contrast-more', { contrast: 'more' }],
]) {
  test(`/try renders under ${name}`, async ({ page }) => {
    await page.emulateMedia(media)
    await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
    assertPageOnAllowedOrigin(page)
    await expect(page.locator('body')).not.toContainText('Application error')
    const query = name === 'forced-colors' ? '(forced-colors: active)' : '(prefers-contrast: more)'
    expect(await page.evaluate((q) => matchMedia(q).matches, query)).toBe(true)
    expect(await sheetText(page)).toContain(`@media ${query}`)
    await page.screenshot({ path: stagingProofPath('uiqol-shell-css', `${name}.png`) })
  })
}

test('under reduced motion a ::before transition reads 0s', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  const duration = await page.evaluate(() => {
    const style = document.createElement('style')
    style.textContent = '.uiqol-motion-probe::before { content: ""; transition: opacity 300ms ease; }'
    document.head.appendChild(style)
    const probe = document.createElement('div')
    probe.className = 'uiqol-motion-probe'
    document.body.appendChild(probe)
    const value = getComputedStyle(probe, '::before').transitionDuration
    probe.remove()
    style.remove()
    return value
  })
  expect(duration).toBe('0s')
})
