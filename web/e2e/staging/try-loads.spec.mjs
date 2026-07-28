import { expect, test } from '@playwright/test'

// Pure connectivity/regression check, deliberately without a capability
// receipt (same shape as e2e/prod/unified-prod-readonly.spec.mjs). The
// original version of this file claimed CA-01 from a catalog badge that
// renders from a client-side mock fixture while signed out
// (web/src/site/ToolCast.jsx: transportMock = PUBLIC_DEMO || !sessionReady,
// which is always true for an anonymous /try visit). That was a vacuous
// pass. CA-01 requires an authenticated session on this deployed surface
// (see e2e/staging/staging-health.spec.mjs and e2e/staging/auth-required.spec.mjs);
// it is not claimed here.
//
// This test must fail, not pass around, a real error signal. ToolCast.jsx
// deliberately renders the operator-phase testid as "Request failed" for
// EVERY anonymous /try visit today (the 401 branch of getSession() and the
// sessionAuthRequired effect both force phase to 'failed', regardless of
// whether the deployed backend is actually healthy). That is a real,
// currently-observed UX gap: an anonymous visitor cannot tell "you are not
// signed in" apart from "the backend is broken" by reading this banner. This
// test intentionally does not paper over that; it fails when the banner is
// visible, exactly as directed, so the gap stays visible in this suite until
// product code stops conflating the two states.
test('the deployed staging /try surface loads without an HTTP-level or application error', async ({ page }) => {
  await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
  await expect(page).toHaveURL(/\/try(?:\?|$)/)
  await expect(page.locator('body')).not.toContainText('Internal Server Error')
  await expect(page.locator('body')).not.toContainText('Application error')

  await expect(page.getByTestId('operator-phase')).not.toContainText('Request failed', { timeout: 15_000 })
})
