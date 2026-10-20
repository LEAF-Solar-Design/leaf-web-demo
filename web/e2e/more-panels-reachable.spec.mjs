import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

// Run from web/: node node_modules/@playwright/test/cli.js test -c playwright.config.mjs more-panels-reachable
// The standard config's live-mode cockpit keeps the real 300px activity rail.
// Only HTTP responses are mocked; no layout, ribbon measurements or clicks are overridden.
async function openCockpit(page, failedLoad) {
  const state = makeCatProofState()
  await page.addInitScript(() => {
    localStorage.setItem('leaf.jwt', 'ribbon-proof-token')
    localStorage.setItem('leaf.org_id', 'cat-proof-org')
  })
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const result = failedLoad && request.method() === 'GET'
      && url.pathname === '/api/session' && url.searchParams.get('dwg') === 'missing.invalid'
      ? { status: 404, body: { detail: 'Drawing not found.' } }
      : catProofResponse({
        method: request.method(), path: url.pathname,
        body: request.postData() ? request.postDataJSON() : {},
        query: Object.fromEntries(url.searchParams),
      }, state)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  // Same failed-drawing deep link as the walk, with a deterministic 404.
  await page.goto(`/app?surface=cad&drawing=${failedLoad ? 'missing.invalid' : 'cat-panels'}`)
  await expect(page.locator('.studio-shell .app')).toHaveAttribute('data-studio-shell', 'cockpit')
  if (failedLoad) {
    await expect(page.getByRole('alert').filter({ hasText: /Couldn['’]t load drawing/ })).toBeVisible()
  } else {
    await expect(page.locator('.viewer-title')).toContainText('cat.dwg')
  }
  const activity = page.locator('#project-workspace-activity')
  await expect(activity).toBeVisible()
  await expect(activity.locator('.conversation-list')).toBeVisible()
  const railBox = await activity.boundingBox()
  expect(railBox.width).toBeCloseTo(300, 0)
  await page.getByRole('tablist', { name: 'Ribbon', exact: true })
    .getByRole('tab', { name: 'Draw', exact: true }).click()
}

async function expectReachable(more) {
  // Poll the geometry so ResizeObserver can finish measuring the active tab.
  // Inspect BEFORE any locator click can scroll the control into view.
  await expect.poll(() => more.evaluate((button) => {
    const box = button.getBoundingClientRect()
    const ribbon = button.closest('.drafting-ribbon').getBoundingClientRect()
    const activity = document.querySelector('#project-workspace-activity').getBoundingClientRect()
    const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)
    return {
      insideViewport: box.width > 0 && box.height > 0 && box.left >= 0 && box.top >= 0
        && box.right <= innerWidth && box.bottom <= innerHeight,
      insideRibbon: box.left >= ribbon.left && box.right <= ribbon.right
        && box.top >= ribbon.top && box.bottom <= ribbon.bottom,
      separateFromActivity: box.right <= activity.left || box.left >= activity.right
        || box.bottom <= activity.top || box.top >= activity.bottom,
      centreHitsButton: hit?.closest('button') === button,
    }
  })).toEqual({
    insideViewport: true, insideRibbon: true, separateFromActivity: true, centreHitsButton: true,
  })
}

for (const viewport of [{ width: 1600, height: 1000 }, { width: 981, height: 900 }]) {
  test.describe(`More panels at ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport })
    for (const failedLoad of [false, true]) {
      test(`stays beside the activity rail and accepts a mouse click (${failedLoad ? 'failed load' : 'drawing ready'})`, async ({ page }) => {
        await openCockpit(page, failedLoad)
        const more = page.getByRole('button', { name: 'More panels', exact: true })
        // This fixture can fit every Draw cluster at 1600px. In that case
        // there is no disclosure to click, and no panel may be hidden.
        // At 981px overflow is required, so always exercise the real click.
        if (viewport.width === 1600 && !(await more.isVisible())) {
          await expect(more).toHaveCount(0)
          await expect(page.locator('#drafting-ribbon-panels .ribbon-cluster').first()).toBeVisible()
          await expect(page.locator('#drafting-ribbon-panels .ribbon-cluster[hidden]')).toHaveCount(0)
          return
        }
        await expect(more).toBeVisible()
        await expect(more).toBeEnabled()
        await expect(more).toHaveAttribute('aria-expanded', 'false')
        await expectReachable(more)
        const box = await more.boundingBox()
        await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2)
        await expect(more).toHaveAttribute('aria-expanded', 'true')
        await expect(page.locator('#drafting-ribbon-panels .ribbon-cluster[hidden]')).toHaveCount(0)
        await expectReachable(more)
        await page.keyboard.press('Escape')
        await expect(more).toHaveAttribute('aria-expanded', 'false')
        await expect(more).toBeFocused()
        await expectReachable(more)
      })
    }
  })
}
