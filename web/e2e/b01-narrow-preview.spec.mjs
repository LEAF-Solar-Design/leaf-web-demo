import { test, expect } from '@playwright/test'
import { readFile } from 'node:fs/promises'

// Render the real production styles in the same short viewport available below
// Studio chrome. This isolates layout from authentication and project fixtures.
const css = await Promise.all(['browserAssetGround.css', 'projectPageEditor.css']
  .map(name => readFile(new URL(`../src/site/${name}`, import.meta.url), 'utf8')))

async function mount(page, width = 390, height = 844) {
  await page.setViewportSize({ width, height })
  await page.setContent(`<style>
    body { margin: 0; }
    :root { --leaf-surface: #111; --leaf-ink: #eee; --leaf-line: #444; }
    ${css.join('\n')}
    .browser-asset-viewport { top: 269px; bottom: 250px; height: auto; }
  </style><section class="browser-asset-ground">
    <div class="browser-asset-viewport">
      <div class="canvas-toolbar" aria-label="Page tools">
        <button>View</button><button>Edit</button><button>Save page</button><button>More</button>
        <div class="canvas-toolbar__status"><span>Saved</span><button>Page status</button></div>
      </div>
      <span class="browser-preview-label">Page draft · Sourdough Bakery</span>
      <main class="project-page"><div class="project-page__content">
        <p class="project-page__label">Sourdough Bakery</p>
        <h1 class="project-page__headline">Sourdough Bakery opens soon</h1>
        <p class="project-page__description">We sell naturally leavened bread, baked with care.</p>
        <p class="project-page__action"><a href="mailto:hello@example.com">Get updates</a></p>
      </div></main>
    </div></section>`)
}

test('390x844 page scrolls below tools and above the draft caption', async ({ page }, info) => {
  await mount(page)
  const toolbar = page.getByLabel('Page tools')
  const preview = page.locator('.project-page')
  const caption = page.locator('.browser-preview-label')
  const heading = page.getByRole('heading')
  const description = page.locator('.project-page__description')
  const contact = page.getByRole('link', { name: 'Get updates' })
  const tools = await toolbar.boundingBox(), frame = await preview.boundingBox(), label = await caption.boundingBox()
  expect(frame.y).toBeGreaterThanOrEqual(tools.y + tools.height)
  expect(label.y).toBeGreaterThanOrEqual(frame.y + frame.height)
  await heading.scrollIntoViewIfNeeded()
  const title = await heading.boundingBox()
  expect(title.y).toBeGreaterThanOrEqual(tools.y + tools.height)
  await description.scrollIntoViewIfNeeded()
  const text = await description.boundingBox()
  expect(text.y + text.height).toBeLessThanOrEqual(label.y)
  await contact.scrollIntoViewIfNeeded()
  await expect(contact).toHaveAttribute('href', 'mailto:hello@example.com')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: info.outputPath('b01-narrow-contact.png') })
  // Expanding tools must shrink the page, never cover it.
  await toolbar.locator('button').last().evaluate(node => {
    const details = document.createElement('p')
    details.className = 'canvas-toolbar__status-details'
    details.textContent = 'Saved page details and additional tool controls'
    node.parentElement.append(details)
  })
  const expanded = await toolbar.boundingBox(), reduced = await preview.boundingBox()
  expect(reduced.y).toBeGreaterThanOrEqual(expanded.y + expanded.height)
  expect(reduced.height).toBeLessThan(frame.height)
})

test('wide page keeps its existing toolbar and caption positioning', async ({ page }) => {
  await mount(page, 1440, 900)
  expect(await page.getByLabel('Page tools').evaluate(node => getComputedStyle(node).position)).toBe('sticky')
  expect(await page.locator('.browser-preview-label').evaluate(node => getComputedStyle(node).position)).toBe('absolute')
})
