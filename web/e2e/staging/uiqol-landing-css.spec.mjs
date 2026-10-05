import { expect, test } from '@playwright/test'
import { captureStagingIdentity } from './stagingIdentity.mjs'
import { assertPageOnAllowedOrigin } from './stagingConfig.mjs'

test('S4 deployed ground entrances and exits use the ratified CSS curves', async ({ page, request }, testInfo) => {
  const identity = await captureStagingIdentity(request)
  await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  await expect(page.locator('main.stage-root')).toBeVisible()

  // Read the served CSSOM, including imported/grouped rules. Wait for the
  // declarations themselves so a late stylesheet cannot produce a false pass.
  const readGroundRules = () => page.evaluate(() => {
    const selectors = {
      entering: '.studio-ground > [data-ground-phase="entering"]',
      leaving: '.studio-ground > [data-ground-phase="leaving"]',
    }
    const result = { entering: [], leaving: [] }
    const visit = (rules) => {
      for (const rule of rules) {
        if (rule.type === CSSRule.STYLE_RULE) {
          for (const [phase, selector] of Object.entries(selectors)) {
            if (rule.selectorText !== selector || rule.style.animationName !== (phase === 'entering' ? 'studioGroundIn' : 'studioGroundOut')) continue
            const duration = rule.style.animationDuration
            result[phase].push({
              timingFunction: rule.style.animationTimingFunction,
              durationMs: Number.parseFloat(duration) * (duration.endsWith('ms') ? 1 : 1000),
            })
          }
        } else if (rule.type === CSSRule.IMPORT_RULE) {
          visit(rule.styleSheet.cssRules)
        } else if (rule.cssRules) {
          visit(rule.cssRules)
        }
      }
    }
    for (const sheet of document.styleSheets) {
      // Third-party font sheets may be opaque; same-origin application
      // stylesheets must remain readable and fail hard if they are not.
      if (sheet.href && new URL(sheet.href).origin !== location.origin) continue
      visit(sheet.cssRules)
    }
    return result
  })

  const expected = {
    entering: [{ timingFunction: 'cubic-bezier(0.22, 1, 0.36, 1)', durationMs: 180 }],
    leaving: [{ timingFunction: 'ease', durationMs: 180 }],
  }
  await expect.poll(readGroundRules, { timeout: 15_000 }).toEqual(expected)
  const observed = await readGroundRules()
  expect(observed).toEqual(expected)
  await testInfo.attach('uiqol-landing-css', {
    contentType: 'application/json',
    body: Buffer.from(JSON.stringify({
      route: '/try',
      source_revision: identity.source_revision,
      ready: identity.ready,
      identity_endpoint: identity.endpoint,
      ground_rules: observed,
      scope: 'Served entrance/exit CSS declarations; no product-tab transition or visual-flight claim.',
    }, null, 2)),
  })
})
