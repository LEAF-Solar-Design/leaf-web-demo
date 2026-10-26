import { test, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { mkdir, writeFile } from 'node:fs/promises'
import os from 'node:os'
import { fileURLToPath } from 'node:url'

const budgets = { mountMs: 500, panP50Ms: 16.7, panP95Ms: 33 }
const receiptSha = () => {
  if (process.env.LAB_PERF_SHA) return process.env.LAB_PERF_SHA
  try {
    return execFileSync('git', ['rev-parse', 'HEAD'], {
      cwd: fileURLToPath(new URL('../../', import.meta.url)),
      timeout: 5000,
      stdio: 'pipe',
      encoding: 'utf8',
    }).trim() || 'unknown'
  } catch {
    return 'unknown'
  }
}
const percentile = (frames, fraction) => {
  const sorted = [...frames].sort((a, b) => a - b)
  // rAF timestamps have finite precision; retain microsecond resolution in ms.
  return sorted.length ? Number(sorted[Math.ceil(sorted.length * fraction) - 1].toFixed(3)) : null
}

test('production canvas: 500 blocks, 1000 pipes and hardware receipt', async ({ page, browser, browserName }) => {
  test.setTimeout(180000)
  let mount = { blocks: 500, pipes: 1000, mountMs: null }
  let renderedNodes = 0
  let renderedEdges = 0
  let relayoutMs = null
  let frames = []
  let clientHardware = {}
  let functionalError
  try {
    await page.goto('/e2e/lab-perf/harness.html')
    await page.waitForFunction(() => window.__labPerf, undefined, { timeout: 60000 })
    mount = await page.evaluate(() => window.__labPerf)
    clientHardware = await page.evaluate(() => ({
      hardwareConcurrency: navigator.hardwareConcurrency,
      deviceMemory: navigator.deviceMemory ?? null,
      userAgent: navigator.userAgent,
    }))
    renderedNodes = await page.locator('.react-flow__node .lab-node-card').count()
    expect(renderedNodes).toBe(500)
    await expect.poll(() => page.locator('.react-flow__edge').count()).toBeGreaterThan(0)
    renderedEdges = await page.locator('.react-flow__edge').count()

    // Let the real initial dependency layout finish before measuring pan alone.
    await expect(page.getByRole('status').filter({ hasText: /^Layout updated$/ })).toBeVisible({ timeout: 60000 })
    await page.getByRole('button', { name: 'Fit cards', exact: true }).click()
    const pane = page.locator('.react-flow__pane')
    const bounds = await pane.boundingBox()
    expect(bounds).not.toBeNull()
    const viewport = page.locator('.react-flow__viewport')
    const beforePan = await viewport.getAttribute('style')
    await page.mouse.move(bounds.x + bounds.width - 30, bounds.y + bounds.height - 100)
    await page.mouse.down()
    await page.evaluate(() => {
      window.__labPan = { frames: [], active: true, previous: null }
      const sample = time => {
        const pan = window.__labPan
        if (!pan.active) return
        if (pan.previous !== null) pan.frames.push(time - pan.previous)
        pan.previous = time
        requestAnimationFrame(sample)
      }
      requestAnimationFrame(sample)
    })
    const started = Date.now()
    let step = 0
    while (Date.now() - started < 2200) {
      const offset = 40 + 100 * (1 + Math.sin(step / 10))
      await page.mouse.move(bounds.x + bounds.width - offset, bounds.y + bounds.height - 100 + 30 * Math.sin(step / 7))
      await page.waitForTimeout(20)
      step += 1
    }
    frames = await page.evaluate(() => {
      window.__labPan.active = false
      return window.__labPan.frames
    })
    await page.mouse.up()
    expect(frames.length).toBeGreaterThan(0)
    expect(await viewport.getAttribute('style')).not.toBe(beforePan)

    const relayout = page.getByRole('button', { name: 'Relayout', exact: true })
    // Observe the completed state change even if the previous status had the same text.
    await page.evaluate(() => {
      window.__labRelayout = { start: performance.now(), end: null }
      const button = [...document.querySelectorAll('button')].find(item => item.textContent === 'Relayout')
      let busy = false
      const observer = new MutationObserver(records => {
        if (records.some(record => record.attributeName === 'disabled' && record.oldValue === null)) busy = true
        if (busy && !button.disabled) {
          window.__labRelayout.end = performance.now()
          observer.disconnect()
        }
      })
      observer.observe(button, { attributes: true, attributeOldValue: true, attributeFilter: ['disabled'] })
    })
    await relayout.click()
    await page.waitForFunction(() => window.__labRelayout.end !== null, undefined, { timeout: 60000 })
    await expect(relayout).toBeEnabled()
    await expect(page.getByRole('status').filter({ hasText: /^Layout updated$/ })).toBeVisible()
    relayoutMs = await page.evaluate(() => window.__labRelayout.end - window.__labRelayout.start)
    renderedEdges = await page.locator('.react-flow__edge').count()
    const marks = page.locator('.lab-pipe-label [data-budget-state]')
    await expect(marks.first()).toContainText('p50')
    await expect(marks.first()).toContainText('p99')
    for (const state of ['within-budget', 'over-budget']) {
      expect(await page.locator(`.lab-pipe-label [data-budget-state="${state}"]`).count()).toBeGreaterThan(0)
    }

    // The full graph exceeds the viewport. Dispatch to a real canvas node to
    // select its inspector without measuring zoom or changing graph state.
    await page.locator('.react-flow__node').first().dispatchEvent('click')
    const inspector = page.getByRole('region', { name: 'Block inspector' })
    // <section aria-label> exposes a region in Chromium.
    for (const name of ['Setup', 'Configure', 'Test', 'Code']) {
      const tab = inspector.getByRole('tab', { name, exact: true })
      await expect(tab).toBeVisible()
      await tab.click()
      await expect(tab).toHaveAttribute('aria-selected', 'true')
      await expect(inspector.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', `block-tab-${name}`)
      await expect(inspector.getByRole('tabpanel')).toBeVisible()
    }
  } catch (error) {
    functionalError = error
  }

  const panP50Ms = percentile(frames, 0.5)
  const panP95Ms = percentile(frames, 0.95)
  const pass = !functionalError && Number.isFinite(mount.mountMs) && mount.mountMs <= budgets.mountMs &&
    frames.length > 0 && panP50Ms <= budgets.panP50Ms && panP95Ms <= budgets.panP95Ms
  const cpus = os.cpus()
  const receipt = {
    schema: 'leaf.lab-canvas-perf.v1',
    sha: receiptSha(),
    ...mount, renderedNodes, renderedEdges,
    panFrames: frames.length, panP50Ms, panP95Ms, relayoutMs, budgets, pass,
    hardware: {
      platform: os.platform(), release: os.release(), cpuModel: cpus[0]?.model ?? 'unknown',
      cpuCount: cpus.length, totalMemoryGB: os.totalmem() / 1024 ** 3,
      browserName, browserVersion: browser.version(),
      hardwareConcurrency: null, deviceMemory: null, userAgent: null, ...clientHardware,
    },
    ...(functionalError ? { failure: functionalError.message } : {}),
  }
  console.log(`LAB_PERF_RECEIPT=${JSON.stringify(receipt)}`)
  const directory = fileURLToPath(new URL('../test-results/', import.meta.url))
  await mkdir(directory, { recursive: true })
  await writeFile(new URL('../test-results/lab-canvas-perf-receipt.json', import.meta.url), `${JSON.stringify(receipt, null, 2)}\n`)
  if (functionalError) throw functionalError
  expect(mount.mountMs, 'mount budget (ms)').toBeLessThanOrEqual(budgets.mountMs)
  expect(panP50Ms, 'pan p50 budget (ms)').toBeLessThanOrEqual(budgets.panP50Ms)
  expect(panP95Ms, 'pan p95 budget (ms)').toBeLessThanOrEqual(budgets.panP95Ms)
})
