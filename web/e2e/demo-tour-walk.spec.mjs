import { expect, test } from '@playwright/test'

test('the demo tour reaches step 7 using only Next', async ({ page }) => {
  test.setTimeout(90_000)
  await page.goto('/try?demo=1')

  const studio = page.locator('.app[data-studio-shell="cockpit"]')
  const coach = studio.locator('.tour-card')
  const next = coach.getByRole('button', { name: 'Next', exact: true })
  const step = coach.locator('.tour-card-step')
  const result = studio.locator('.result-block')
  await expect(studio).toBeVisible()
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
  await expect(author.locator('.inline-error').filter({
    hasText: 'Tool building is unavailable in this signed-out demo.',
  })).toBeVisible({ timeout: 20_000 })
  await expect(author.locator('.authored')).toHaveCount(0)
  await expect(next).toBeEnabled({ timeout: 20_000 })

  await next.click()
  await expect(step).toHaveText('Step 7 of 9')
  await expect(coach.locator('.tour-card-title')).toHaveText('Edits are versioned, your call')
})

test('Open author flow reveals the same studio panel outside the tour', async ({ page }) => {
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

  // Close the catalog after routing so this click must restore the seat too.
  await studio.getByRole('button', { name: 'Collapse the tool rail to a spine', exact: true }).click()
  await expect(author).toHaveCount(0)
  await openAuthor.click()
  await expect(author).toHaveCount(1)
  await expect(author).toBeVisible()
  await expect(author.getByLabel('What should the tool do?')).toHaveValue(description)
  await expect(author.locator('.authored')).toHaveCount(0)
})
