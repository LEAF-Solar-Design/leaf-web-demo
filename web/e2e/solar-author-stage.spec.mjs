import { expect, test } from '@playwright/test'
import {
  AUTHORED_TOOL,
  catProofResponse,
  makeCatProofState,
} from './catProofFixture.mjs'

// Slice 7a (standardization, D3 close): the stage's workspace rail used to
// mount only inside `stageBranch === 'cad'`. This row proves the widened
// gate (`authoringOnStage`) on /try?surface=solar: the Author tab is present
// (the rail mounted at all) and the stage's author flow — stage, request
// publication, approve, publish, run — completes on solar exactly as it does
// on cad, with the published tool landing in a family this fixture labels
// 'stringing', one of solar's own declared `familyIds` (`productSurfaces.js`).
//
// `?proof=1` seeds MODE_DRAWING_ID = 'cat-panels' (ToolCast.jsx) regardless
// of `?surface=`, so the same deterministic drawing/session fixture cat's
// e2e row uses (`web/e2e/catProofFixture.mjs`) boots here unmodified; the
// ONLY local change is relabelling the authored tool's family from the
// fixture's own 'custom-authored' to 'stringing' on the way out, which is
// what proves the client passes a server-assigned family straight through
// rather than inventing one — no client code names a family at all
// (grepped: AuthorPanel.jsx and ToolCast.jsx's authorTool/publishAuthoredTool
// carry no family concept), so the assignment can only ever be the server's.
test('the stage author flow mounts and publishes on /try?surface=solar', async ({ page }) => {
  test.setTimeout(120_000)
  const proofState = makeCatProofState()
  const apiEndpoints = []
  let authorStageBody = null
  const authorPublicationBodies = []
  let authoredRunBody = null

  await page.addInitScript(() => localStorage.setItem('leaf.org_id', 'cat-proof-org'))

  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    apiEndpoints.push(`${request.method()} ${url.pathname}`)
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    if (url.pathname === '/api/author/stage') authorStageBody = body
    if (url.pathname === '/api/author/publication-requests') authorPublicationBodies.push(body)
    if (url.pathname === '/api/run' && body.tool === AUTHORED_TOOL.name) authoredRunBody = body
    const result = catProofResponse({ method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, proofState)
    // The ONE local edit: relabel the authored tool's family from the
    // fixture's 'custom-authored' to 'stringing', solar's own declared
    // family, so the flow below proves a real server-assigned family lands
    // on solar's Catalog tab rather than a fabricated client-side one.
    let responseBody = result.body
    if (url.pathname === '/api/capabilities' && responseBody?.families) {
      responseBody = {
        ...responseBody,
        families: responseBody.families.map((family) => (
          family.family_id === 'custom-authored'
            ? { ...family, family_id: 'stringing', label: 'Stringing' }
            : family
        )),
      }
    }
    await route.fulfill({
      status: result.status,
      contentType: responseBody == null ? undefined : 'application/json',
      body: responseBody == null ? '' : JSON.stringify(responseBody),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try?proof=1&surface=solar')
  await expect(page).toHaveURL(/\/try\?proof=1&surface=solar$/)
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })

  // Proof (a): the workspace rail mounted at all on solar's stage — the four
  // tabs are reachable, not just the ProductSurfaceFrame placeholder solar
  // rendered before this slice.
  const authorTab = page.getByRole('tab', { name: 'Author' })
  await expect(authorTab).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Operator' })).toBeVisible()
  await expect(page.getByRole('tab', { name: /Catalog/ })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Project' })).toBeVisible()

  // Proof (b): the author flow itself — stage, request publication, approve,
  // publish, run — completes on solar exactly as it does on cad.
  await authorTab.click()
  await page.getByLabel('What should the tool do?').fill('count panels within 24in of the roof edge')
  await page.getByRole('button', { name: 'Generate tool' }).click()
  await expect(page.getByText(AUTHORED_TOOL.name)).toBeVisible()
  await expect(page.getByText(/Staged and ready to publish/)).toBeVisible()
  expect(authorStageBody).toMatchObject({ description: 'count panels within 24in of the roof edge', mode: 'build' })

  await page.getByRole('button', { name: 'Request publication' }).click()
  await expect(page.getByText(/Awaiting independent approval/)).toBeVisible()
  expect(authorPublicationBodies).toHaveLength(1)
  proofState.independentApproved = true
  await page.getByRole('button', { name: 'Check approval & resume' }).click()
  await expect(page.getByRole('button', { name: 'Run it now' })).toBeVisible()
  await expect(page.locator('.toast')).toContainText(`Tool published, ${AUTHORED_TOOL.name}`)
  expect(authorPublicationBodies).toHaveLength(2)

  await page.getByRole('button', { name: 'Run it now' }).click()
  await expect(page.getByRole('button', { name: `Run ${AUTHORED_TOOL.name}` })).toBeVisible()
  await page.getByRole('button', { name: `Run ${AUTHORED_TOOL.name}` }).click()
  expect(authoredRunBody?.tool).toBe(AUTHORED_TOOL.name)
  await page.getByRole('tab', { name: 'Execution' }).click()
  await expect(page.getByTestId('catalog-run-result')).toContainText(AUTHORED_TOOL.name, { timeout: 12_000 })
  await expect(page.getByTestId('catalog-run-result')).toContainText('Passed')

  // The published tool now shows in the Catalog under the family this
  // fixture relabelled 'stringing' — solar's own declared familyIds
  // (productSurfaces.js: familyIds: ['stringing', 'placement']).
  await page.getByRole('tab', { name: /Catalog/ }).click()
  await expect(page.getByRole('button', { name: /Stringing/ })).toBeVisible()
  await expect(page.getByText(AUTHORED_TOOL.name).first()).toBeVisible()

  expect(apiEndpoints).toContain('POST /api/author/stage')
  expect(apiEndpoints).toContain('POST /api/author/publication-requests')
})
