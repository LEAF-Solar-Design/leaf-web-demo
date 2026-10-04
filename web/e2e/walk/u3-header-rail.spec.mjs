import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'
import { readFile } from 'node:fs/promises'

const detailsEntry = buildFeatureMap().entries.find(entry => entry.id === 'control:session-details')
const shell = '.studio-shell .app[data-studio-shell="cockpit"][data-surface="cad"]'
const overlaps = (a, b) => Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x) > 0.5
  && Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y) > 0.5

async function reachable(locator, pointerRequired = true) {
  await expect(locator).toBeVisible()
  await locator.scrollIntoViewIfNeeded()
  const measurement = await locator.evaluate(element => {
    const rect = element.getBoundingClientRect()
    const points = [[0.5, 0.5], [0.2, 0.5], [0.8, 0.5]]
    const clip = { left: 0, top: 0, right: innerWidth, bottom: innerHeight }
    for (let parent = element; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent)
      const box = parent.getBoundingClientRect()
      if (['auto', 'scroll', 'hidden', 'clip'].includes(style.overflowX)) {
        clip.left = Math.max(clip.left, box.left)
        clip.right = Math.min(clip.right, box.right)
      }
      if (['auto', 'scroll', 'hidden', 'clip'].includes(style.overflowY)) {
        clip.top = Math.max(clip.top, box.top)
        clip.bottom = Math.min(clip.bottom, box.bottom)
      }
    }
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT)
    const textRects = []
    while (walker.nextNode()) {
      const node = walker.currentNode
      if (!node.textContent.trim() || node.parentElement.namespaceURI !== 'http://www.w3.org/1999/xhtml') continue
      const range = document.createRange()
      range.selectNodeContents(node)
      for (const box of range.getClientRects()) {
        if (box.width && box.height) textRects.push({
          left: box.left, right: box.right, top: box.top, bottom: box.bottom,
        })
      }
    }
    return {
      label: element.innerText || element.getAttribute('aria-label'),
      x: rect.x, y: rect.y, width: rect.width, height: rect.height,
      scrollWidth: element.scrollWidth, clientWidth: element.clientWidth,
      textRects, clip,
      labelsVisible: textRects.every(box => box.left >= clip.left - 1 && box.right <= clip.right + 1
        && box.top >= clip.top - 1 && box.bottom <= clip.bottom + 1),
      hits: points.map(([x, y]) => {
        const target = document.elementFromPoint(rect.x + rect.width * x, rect.y + rect.height * y)
        return target === element || element.contains(target)
      }),
    }
  })
  expect(measurement.width).toBeGreaterThan(0)
  expect(measurement.height).toBeGreaterThan(0)
  expect(measurement.scrollWidth).toBeLessThanOrEqual(measurement.clientWidth + 1)
  expect(measurement.labelsVisible, measurement.label).toBe(true)
  if (pointerRequired) expect(measurement.hits).toEqual([true, true, true])
  return measurement
}

async function headerGeometry(page) {
  const controls = page.locator(`${shell} header.top button, ${shell} .tc-product-tabs [role="tab"]`)
  const rectangles = []
  for (const locator of await controls.all()) {
    if (!await locator.isVisible()) continue
    // Disabled ribbon tabs still need readable labels; enabled controls also
    // need ordinary pointer targets, including Sign out without activating it.
    rectangles.push(await reachable(locator, await locator.isEnabled()))
  }
  expect(rectangles.length).toBeGreaterThan(3)
  for (let i = 0; i < rectangles.length; i++) {
    for (let j = i + 1; j < rectangles.length; j++) {
      expect(overlaps(rectangles[i], rectangles[j]),
        `Header overlap: ${rectangles[i].label} / ${rectangles[j].label}`).toBe(false)
    }
  }
  const header = await page.locator(`${shell} header.top`).boundingBox()
  const nav = await page.locator(`${shell} .tc-product-nav`).boundingBox()
  expect(nav.y).toBeGreaterThanOrEqual(header.y + header.height - 0.5)
  expect(nav.height).toBeCloseTo(30, 0)
  return { header, nav, rectangles }
}

async function railGeometry(page) {
  const stack = page.locator(`${shell} > .rail-stack`)
  const inbox = stack.locator('.job-inbox')
  await expect(inbox).toBeVisible()
  const toggle = inbox.getByRole('button', { name: 'Collapse the notification inbox', exact: true })
  const pointer = await reachable(toggle)
  const seat = await stack.boundingBox()
  const box = await inbox.boundingBox()
  expect(seat.width).toBeCloseTo(300, 0)
  expect(box.width).toBeCloseTo(300, 0)
  expect(box.x).toBeGreaterThanOrEqual(seat.x - 0.5)
  expect(box.x + box.width).toBeLessThanOrEqual(seat.x + seat.width + 0.5)
  const bodies = await inbox.locator('.rail-note, .rail-detail').evaluateAll(elements => elements.map(element => {
    const style = getComputedStyle(element)
    return { text: element.textContent, width: element.getBoundingClientRect().width,
      whiteSpace: style.whiteSpace, wordBreak: style.wordBreak,
      scrollWidth: element.scrollWidth, clientWidth: element.clientWidth }
  }))
  expect(bodies.length).toBeGreaterThan(0)
  for (const body of bodies) {
    expect(body.width).toBeGreaterThan(100)
    expect(body.whiteSpace).toBe('normal')
    expect(body.wordBreak).toBe('normal')
    expect(body.scrollWidth).toBeLessThanOrEqual(body.clientWidth + 1)
  }
  const cards = await stack.locator(':scope > :not([data-spine])').evaluateAll(elements =>
    elements.filter(element => element.getBoundingClientRect().height > 0).map(element => {
      const rect = element.getBoundingClientRect()
      return { label: element.className, x: rect.x, y: rect.y, width: rect.width, height: rect.height }
    }))
  for (let i = 0; i < cards.length; i++) {
    for (let j = i + 1; j < cards.length; j++) {
      expect(overlaps(cards[i], cards[j]), `Rail overlap: ${cards[i].label} / ${cards[j].label}`).toBe(false)
    }
  }
  const instruments = []
  const header = await page.locator(`${shell} header.top`).boundingBox()
  const nav = await page.locator(`${shell} .tc-product-nav`).boundingBox()
  for (const locator of await page.locator(`${shell} .cad-overview, ${shell} .cockpit-cube-wrap, ${shell} .cockpit-cube-wcs`).all()) {
    if (!await locator.isVisible()) continue
    const rect = await locator.boundingBox()
    const label = await locator.getAttribute('class')
    expect(rect.x, label).toBeGreaterThanOrEqual(0)
    expect(rect.x + rect.width, label).toBeLessThanOrEqual(seat.x - 17)
    expect(rect.y, label).toBeGreaterThanOrEqual(nav.y + nav.height)
    expect(overlaps(rect, header), `${label} / header`).toBe(false)
    expect(overlaps(rect, seat), `${label} / rail`).toBe(false)
    instruments.push({ label, ...rect })
  }
  for (let i = 0; i < instruments.length; i++) {
    for (let j = i + 1; j < instruments.length; j++) {
      expect(overlaps(instruments[i], instruments[j]),
        `Canvas instrument overlap: ${instruments[i].label} / ${instruments[j].label}`).toBe(false)
    }
  }
  return { seat, inbox: box, pointer, bodies, cards, instruments }
}

async function snapshot(page, testInfo, name, measurements) {
  await testInfo.attach(name + '-geometry', {
    body: Buffer.from(JSON.stringify(measurements, null, 2)), contentType: 'application/json',
  })
  await testInfo.attach(name, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' })
}

for (const viewport of [{ width: 1600, height: 1000 }, { width: 1280, height: 800 }]) {
  for (const state of ['ready', 'failed-load']) {
    test(`u3-header-rail ${viewport.width}x${viewport.height} [${state}] @desktop`,
      async ({ page, stack, walkEvidence }, testInfo) => {
        await page.setViewportSize(viewport)
        // Reuse the walk's private upload or actual missing.invalid recipe and
        // its ordinary Details click and provenance oracle.
        await runProbe(resolveProbe(detailsEntry, state), { page, stack, evidence: walkEvidence, testInfo })
        const measurements = []
        try {
          const details = page.locator(`${shell} header.top`).getByRole('button', { name: 'Details', exact: true })
          await reachable(details)
          await reachable(page.locator(`${shell} header.top`).getByRole('button', { name: 'Sign out', exact: true }))
          const cost = page.locator(`${shell} header.top`).getByRole('button', { name: 'What Leaf costs to operate', exact: true })
          await reachable(cost)
          await cost.click()
          const costPanel = page.getByRole('region', { name: 'What Leaf costs to operate', exact: true })
          await expect(costPanel).toBeVisible()
          await costPanel.getByRole('button', { name: 'Close cost panel', exact: true }).click()
          await expect(costPanel).toBeHidden()
          await details.click()
          const dialog = page.getByRole('dialog', { name: 'Session · provenance', exact: true })
          await expect(dialog).toBeVisible()
          await dialog.getByRole('button', { name: 'Close details', exact: true }).click()
          await expect(dialog).toBeHidden()
          if (state === 'ready') {
            await expect(page.locator(`${shell} .cockpit-cube-wrap`)).toBeVisible()
            const overview = page.locator(`${shell} .cad-overview`)
            await expect(overview).toBeVisible()
            await reachable(overview.getByRole('button', { name: 'Collapse drawing overview', exact: true }))
            await overview.getByRole('button', { name: 'Collapse drawing overview', exact: true }).click()
            await reachable(overview.getByRole('button', { name: 'Expand drawing overview', exact: true }))
            await overview.getByRole('button', { name: 'Expand drawing overview', exact: true }).click()
          }

          for (const posture of ['hidden', 'expanded']) {
            const collapseNav = page.getByRole('button', { name: 'Collapse the tool rail to a spine', exact: true })
            if (posture === 'hidden' && await collapseNav.isVisible()) await collapseNav.click()
            if (posture === 'expanded') await page.getByRole('button', { name: 'Tool rail', exact: true }).click()
            measurements.push({ posture, jobs: 'collapsed', header: await headerGeometry(page), rail: await railGeometry(page) })
            const inbox = page.locator(`${shell} .job-inbox`)
            await inbox.getByRole('button', { name: 'Collapse the notification inbox', exact: true }).click()
            const expandInbox = inbox.getByRole('button', { name: 'Expand the notification inbox', exact: true })
            await expect(expandInbox).toHaveAttribute('aria-expanded', 'false')
            await reachable(expandInbox)
            await expandInbox.click()
            await expect(inbox.getByRole('button', { name: 'Collapse the notification inbox', exact: true }))
              .toHaveAttribute('aria-expanded', 'true')
            await page.getByRole('button', { name: /^Expand the job monitor \([0-9]+ live\)$/ }).click()
            measurements.push({ posture, jobs: 'expanded', header: await headerGeometry(page), rail: await railGeometry(page) })
            await snapshot(page, testInfo, posture + '-jobs-expanded', measurements.at(-1))
            await reachable(page.getByRole('button', { name: /^Linked services [0-9]+ linked$/ }))
            await page.getByRole('button', { name: 'Collapse the job monitor to a spine', exact: true }).click()
            await expect(page.getByRole('button', { name: /^Expand the job monitor \([0-9]+ live\)$/ })).toBeVisible()
          }
        } finally {
          await snapshot(page, testInfo, 'desktop-final', measurements)
        }
      })
  }
}

const phoneViewport = { width: 390, height: 844 }
const phoneRoute = '/app?surface=cad&drawing=missing.invalid'

async function phoneMeasurements(page, url) {
  await page.setViewportSize(phoneViewport)
  const reply = page.waitForResponse(response => {
    const url = new URL(response.url())
    return url.pathname === '/api/session' && url.searchParams.get('dwg') === 'missing.invalid'
  })
  await page.goto(url)
  expect([400, 404]).toContain((await reply).status())
  await expect(page.getByRole('alert').filter({ hasText: /Couldn['’]t load drawing/ })).toBeVisible()
  await expect(page.locator(`${shell} header.top`)).toBeVisible()
  await expect(page.getByRole('combobox', { name: 'Command bar', exact: true })).toBeVisible()
  return page.locator(shell).evaluate(element => {
    const rect = selector => {
      const node = element.querySelector(selector)
      const box = node.getBoundingClientRect()
      const style = getComputedStyle(node)
      return { x: box.x, y: box.y, width: box.width, height: box.height,
        position: style.position, overflow: style.overflow }
    }
    return { header: rect('header.top'), rail: rect('.rail-stack'), footer: rect('footer.foot-bar') }
  })
}

test('u3-header-rail phone comparison 390x844 @desktop', async ({ page }, testInfo) => {
  // The verifier supplies either a recording from origin/main or the URL
  // and SHA of its default build. Never manufacture a baseline from this
  // worktree's failing measurement or guessed phone CSS dimensions.
  let baseline
  if (process.env.LEAF_U3_PHONE_BASELINE_JSON) {
    baseline = JSON.parse(await readFile(process.env.LEAF_U3_PHONE_BASELINE_JSON, 'utf8'))
  } else if (process.env.LEAF_U3_PHONE_BASELINE_URL && process.env.LEAF_U3_PHONE_BASELINE_SHA) {
    baseline = {
      schema: 'leaf.u3-phone-baseline.v1', sourceRef: 'origin/main',
      revision: process.env.LEAF_U3_PHONE_BASELINE_SHA, viewport: phoneViewport, route: phoneRoute,
      measurements: await phoneMeasurements(page, new URL(phoneRoute, process.env.LEAF_U3_PHONE_BASELINE_URL).href),
    }
    await snapshot(page, testInfo, 'phone-origin-main', baseline)
  } else {
    throw new Error('U3_PHONE_BASELINE_REQUIRED: supply LEAF_U3_PHONE_BASELINE_JSON recorded from origin/main, or LEAF_U3_PHONE_BASELINE_URL and LEAF_U3_PHONE_BASELINE_SHA for its default build')
  }
  expect(baseline.schema).toBe('leaf.u3-phone-baseline.v1')
  expect(baseline.sourceRef).toBe('origin/main')
  expect(baseline.revision).toMatch(/^[a-f0-9]{40}$/)
  expect(baseline.viewport).toEqual(phoneViewport)
  expect(baseline.route).toBe(phoneRoute)
  const measurements = await phoneMeasurements(page, phoneRoute)
  await snapshot(page, testInfo, 'phone-account-row-seats', { baseline, measurements })
  // A3 adds a 44px account row so the quick access and ribbon tabs keep the full phone width.
  expect(measurements.header).toEqual({ ...baseline.measurements.header, height: baseline.measurements.header.height + 44 })
  expect(measurements.rail).toEqual(baseline.measurements.rail)
  expect(measurements.footer).toEqual(baseline.measurements.footer)
})
