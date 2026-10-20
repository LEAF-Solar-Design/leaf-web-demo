import { expect, test } from '@playwright/test'

async function expectNoOverlap(a, b) {
  // Hidden results and the collapsed catalog have no painted box to intersect.
  if (!await a.isVisible() || !await b.isVisible()) return
  await expect.poll(async () => {
    const [first, second] = await Promise.all([a.boundingBox(), b.boundingBox()])
    if (!first || !second) return false
    return first.x + first.width <= second.x || second.x + second.width <= first.x
      || first.y + first.height <= second.y || second.y + second.height <= first.y
  }, { message: `${a} must not overlap ${b}` }).toBe(true)
}

async function expectCanvasChromeClear(studio) {
  const rail = studio.locator('aside.nav')
  const rightRail = studio.locator('aside.rail')
  const find = studio.locator('.drawing-navigation')
  const objects = studio.locator('details.drawing-objects-panel')
  const view = studio.locator('.cockpit-view')
  await expect(find).toBeVisible()
  // The dev app omits the optional CAD edit surface; staging enables it.
  for (const control of [objects, view]) {
    if (await control.count() > 0) await expect(control).toBeVisible()
  }
  for (const control of [studio.locator('.result-block'), find, objects, view]) {
    await expectNoOverlap(rail, control)
    await expectNoOverlap(rightRail, control)
  }
  await expectNoOverlap(find, objects)
  await expectNoOverlap(find, view)
  await expectNoOverlap(objects, view)
}

async function expectInboxCountGap(studio) {
  const heading = studio.locator('.job-inbox h2')
  await expect(heading).toBeVisible()
  await expect.poll(() => heading.evaluate((h2) => {
    const range = document.createRange()
    range.selectNodeContents(h2.firstChild)
    return h2.querySelector('.n').getBoundingClientRect().left - range.getBoundingClientRect().right
  })).toBeGreaterThanOrEqual(4)
}

test('the demo tour reaches step 7 using only Next', async ({ page }) => {
  test.setTimeout(90_000)
  await page.goto('/try?demo=1')

  const studio = page.locator('.app[data-studio-shell="cockpit"]')
  const coach = studio.locator('.tour-card')
  const next = coach.getByRole('button', { name: 'Next', exact: true })
  const step = coach.locator('.tour-card-step')
  const result = studio.locator('.result-block')
  await expect(studio).toBeVisible({ timeout: 30_000 })
  await expect(step).toHaveText('Step 1 of 9')
  // Start with the ordinary collapsed catalog; no saved rail preference can
  // accidentally supply the author seat this walk is intended to exercise.
  await expect(studio.locator('aside.nav')).toHaveAttribute('data-spine', 'hidden')

  await next.click()
  await expect(step).toHaveText('Step 2 of 9')
  await next.click()

  for (const [number, tool] of [
    [3, 'count-by-layer'],
    [4, 'highlight-panels-near-edge'],
    [5, 'measure-panel-area'],
  ]) {
    await expect(step).toHaveText(`Step ${number} of 9`)
    await expect(next).toBeDisabled()
    await expect(result.getByRole('heading', { name: 'Result', exact: true })).toBeVisible({ timeout: 20_000 })
    await expect(result.locator('.result-tool')).toContainText(tool, { timeout: 20_000 })
    await expect(result.locator('.ok.yes')).toHaveText('Passed')
    await expect(next).toBeEnabled({ timeout: 20_000 })
    await next.click()
  }

  await expect(step).toHaveText('Step 6 of 9')
  await expect(next).toBeDisabled()
  const authorSection = studio.locator('.author-section')
  const author = authorSection.locator('.author-panel')
  await expect(authorSection).toBeVisible()
  await expect(author).toHaveCount(1)
  await expect(author).toBeVisible()
  const staged = author.locator('.authored')
  await expect(staged).toBeVisible({ timeout: 20_000 })
  await expect(staged.locator('.tool-name')).toHaveText(/\S+/, { timeout: 20_000 })
  await expect(staged).toContainText('Demo preview staged. Publish to make it available in this demo.')
  await expect(next).toBeEnabled({ timeout: 20_000 })

  await expect(studio.locator('aside.nav')).not.toHaveAttribute('data-spine')
  await expectCanvasChromeClear(studio)
  await expectInboxCountGap(studio)

  await next.click()
  await expect(step).toHaveText('Step 7 of 9')
  await expect(coach.locator('.tour-card-title')).toHaveText('Edits are versioned, your call')
  await expect(studio.locator('aside.nav')).not.toHaveAttribute('data-spine')
  await expectCanvasChromeClear(studio)
  await expectInboxCountGap(studio)
})

test('the demo tour keeps canvas chrome clear at the desktop breakpoint', async ({ page }) => {
  test.setTimeout(90_000)
  await page.setViewportSize({ width: 981, height: 900 })
  await page.goto('/try?demo=1')

  const studio = page.locator('.app[data-studio-shell="cockpit"]')
  const coach = studio.locator('.tour-card')
  const next = coach.getByRole('button', { name: 'Next', exact: true })
  const step = coach.locator('.tour-card-step')
  const result = studio.locator('.result-block')
  await expect(studio).toBeVisible({ timeout: 30_000 })
  await expect(step).toHaveText('Step 1 of 9')
  await expect(studio.locator('aside.nav')).toHaveAttribute('data-spine', 'hidden')
  await next.click()
  await expect(step).toHaveText('Step 2 of 9')
  await next.click()

  for (const [number, tool] of [
    [3, 'count-by-layer'],
    [4, 'highlight-panels-near-edge'],
    [5, 'measure-panel-area'],
  ]) {
    await expect(step).toHaveText(`Step ${number} of 9`)
    await expect(result.locator('.result-tool')).toContainText(tool, { timeout: 20_000 })
    await expect(result.locator('.ok.yes')).toHaveText('Passed')
    await expect(next).toBeEnabled({ timeout: 20_000 })
    await next.click()
  }

  await expect(step).toHaveText('Step 6 of 9')
  await expect(studio.locator('.author-section .author-panel .authored')).toContainText(
    'Demo preview staged. Publish to make it available in this demo.', { timeout: 20_000 },
  )
  await expect(next).toBeEnabled({ timeout: 20_000 })
  await expect(studio.locator('aside.nav')).not.toHaveAttribute('data-spine')
  await expectCanvasChromeClear(studio)
  await expectInboxCountGap(studio)
})

test('Open author flow reveals the same studio panel outside the tour', async ({ page }) => {
  test.setTimeout(90_000)
  await page.goto('/try?demo=1')
  const studio = page.locator('.app[data-studio-shell="cockpit"]')
  await studio.locator('.tour-card').getByRole('button', { name: 'Skip', exact: true }).click()
  await expect(studio.locator('.tour-card')).toHaveCount(0)

  const description = 'build a tool that flags panels within 24 in of the roof edge'
  const command = studio.getByRole('combobox', { name: 'Command bar', exact: true })
  await command.fill(description)
  await command.press('Enter')
  const openAuthor = studio.getByRole('button', { name: 'Open author flow', exact: true })
  const author = studio.locator('.author-section .author-panel')
  await expect(openAuthor).toBeVisible()
  await expect(author).toBeVisible()
  await expect(studio.locator('aside.nav')).not.toHaveAttribute('data-spine')
  await expectCanvasChromeClear(studio)
  await expectInboxCountGap(studio)

  // Close the catalog after routing so this click must restore the seat too.
  await studio.getByRole('button', { name: 'Collapse the tool rail to a spine', exact: true }).click()
  await expect(author).toHaveCount(0)
  await expect(studio.locator('aside.nav')).toHaveAttribute('data-spine', 'hidden')
  await expectCanvasChromeClear(studio)
  await openAuthor.click()
  await expect(author).toHaveCount(1)
  await expect(author).toBeVisible()
  await expect(author.getByLabel('What should the tool do?')).toHaveValue(description)
  await expect(author.locator('.authored')).toHaveCount(0)
  await expect(studio.locator('aside.nav')).not.toHaveAttribute('data-spine')
  await expectCanvasChromeClear(studio)
})
