import { test, expect, setupStep } from './fixtures.mjs'

if (process.env.LEAF_WALK_PROOF === '1') {
  test('w3 a 5xx response carries its error id and the stack log into walk evidence @desktop',
    async ({ page, stack, walkEvidence }, testInfo) => {
      const probe = { setup: { steps: [] } }
      const runtime = { page, stack, evidence: walkEvidence, testInfo, cleanup: [] }
      const pattern = '**/api/capabilities*'
      const fail = (route) => route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ error: {
          code: 'internal', message: 'internal server error (error_id: 0123456789abcdef)', retryable: false,
        } }),
      })
      await page.route(pattern, fail)
      await setupStep(probe, runtime, { kind: 'open-private-drawing', surface: 'solar' })
      await expect.poll(() => walkEvidence.serverErrors.find((r) => r.errorId === '0123456789abcdef')?.serverOutput.length || 0,
        { timeout: 30_000 }).toBeGreaterThan(0)
      const record = walkEvidence.serverErrors.find((r) => r.errorId === '0123456789abcdef')
      expect(record.status).toBe(500)
      expect(record.matched).toBe(false)
      expect(walkEvidence.serverErrors.length).toBeLessThanOrEqual(5)
      await page.unroute(pattern, fail)
      await testInfo.attach('w3-server-diagnostics-proof', {
        body: Buffer.from(JSON.stringify({
          serverErrors: walkEvidence.serverErrors.map(({ serverOutput, ...rest }) =>
            ({ ...rest, serverOutputChars: serverOutput.length })),
          dropped: walkEvidence.serverErrorsDropped,
        }, null, 2)),
        contentType: 'application/json',
      })
    })
}
