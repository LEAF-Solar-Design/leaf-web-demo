import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { arch, cpus, hostname, platform } from 'node:os'
import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

// A25 budget probe (uiqol S12). Fixture mode: types 20 characters into the
// command bar and reads interaction latency, then drives a scripted wheel and
// right-button pan on the resident viewer and reads requestAnimationFrame
// deltas. Both numbers, their sample counts, the host and the source commit
// land in perf-budgets.result.json in the test output dir. The budgets live in
// perf-budgets.json. Headless numbers differ from user hardware, so a breach
// fails the row only when LEAF_PERF_ENFORCE=1; record mode asserts only that
// the measurement is real (finite, above 0, enough samples) and written.

const BUDGET = JSON.parse(readFileSync(new URL('./perf-budgets.json', import.meta.url), 'utf8'))
const ENFORCE = process.env[BUDGET.enforce_env] === '1'
// Exactly 20 plain characters, so each keystroke is one interaction.
const TYPED = 'zoom to panel row 12'

async function install(page) {
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const body = request.postData() ? request.postDataJSON() : {}
    const result = catProofResponse({ method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })
  // Event Timing reports an interaction only at or above its 16 ms floor, so
  // every recorded keydown also carries its own event-to-next-frame latency
  // (rAF, then the task after it). The interaction's latency is the larger of
  // the two, so a missing Event Timing entry never reads as zero.
  await page.addInitScript(() => {
    const perf = { recordKeys: false, keys: [], entries: [], framesOn: false, frames: [] }
    window.__leafPerf = perf
    try {
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if (entry.interactionId) {
            perf.entries.push({ name: entry.name, start: entry.startTime, duration: entry.duration, id: entry.interactionId })
          }
        }
      }).observe({ type: 'event', buffered: true, durationThreshold: 16 })
    } catch {}
    addEventListener('keydown', (event) => {
      if (!perf.recordKeys || !event.isTrusted) return
      const row = { start: event.timeStamp, nextFrame: null }
      perf.keys.push(row)
      requestAnimationFrame(() => setTimeout(() => { row.nextFrame = performance.now() - row.start }, 0))
    }, true)
  })
}

// Nearest-rank percentile over a non-empty list of finite numbers.
function percentile(values, p) {
  const sorted = [...values].sort((a, b) => a - b)
  return sorted[Math.max(0, Math.ceil(p * sorted.length) - 1)]
}

function sourceCommit() {
  for (const name of ['LEAF_SOURCE_SHA', 'CODEBUILD_RESOLVED_SOURCE_VERSION', 'GITHUB_SHA']) {
    const value = process.env[name]
    if (value && /^[0-9a-f]{40}$/.test(value)) return { sha: value, source: name }
  }
  try {
    const sha = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8', timeout: 5000, stdio: ['ignore', 'pipe', 'ignore'] }).trim()
    if (/^[0-9a-f]{40}$/.test(sha)) return { sha, source: 'git' }
  } catch {}
  return { sha: null, source: 'unavailable' }
}

test('command bar INP p75 and canvas frame p95 are measured against the A25 budgets', async ({ page, browser }, testInfo) => {
  test.setTimeout(90_000)
  await install(page)
  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  const viewer = page.getByRole('region', { name: 'Drawing viewer' })
  await expect(viewer.locator('.stage-viewer')).toHaveClass(/settled/, { timeout: 20_000 })

  // Command bar: one keystroke per interaction, spaced so each gets a frame.
  const bar = page.getByLabel('Command bar', { exact: true })
  await bar.click()
  await expect(bar).toBeFocused()
  await page.evaluate(() => { window.__leafPerf.keys = []; window.__leafPerf.recordKeys = true })
  await page.keyboard.type(TYPED, { delay: 60 })
  await expect.poll(() => page.evaluate(() => window.__leafPerf.keys.filter((row) => row.nextFrame != null).length))
    .toBeGreaterThanOrEqual(TYPED.length)
  const typing = await page.evaluate(() => {
    const perf = window.__leafPerf
    perf.recordKeys = false
    const byId = new Map()
    for (const entry of perf.entries) byId.set(entry.id, Math.max(byId.get(entry.id) || 0, entry.duration))
    let eventTimingSamples = 0
    const latencies = perf.keys.map((row) => {
      const down = perf.entries.find((entry) => entry.name === 'keydown' && Math.abs(entry.start - row.start) < 0.5)
      const timed = down ? byId.get(down.id) : undefined
      if (timed !== undefined) eventTimingSamples += 1
      return Math.max(row.nextFrame, timed ?? 0)
    })
    return { latencies, eventTimingSamples }
  })
  await bar.fill('')
  await page.keyboard.press('Escape')

  // Canvas: wheel zoom, then a right-button pan (the 2D viewer's pan button),
  // with rAF deltas recorded across both. The canvas box is polled because the
  // engine head opening can replace the canvas after boot.
  const canvas = viewer.locator('.viewer-canvas canvas')
  await expect.poll(async () => (await canvas.boundingBox())?.width ?? 0, { timeout: 20_000 }).toBeGreaterThan(0)
  const box = await canvas.boundingBox()
  const cx = box.x + box.width / 2
  const cy = box.y + box.height / 2
  await page.mouse.move(cx, cy)
  await page.evaluate(() => {
    const perf = window.__leafPerf
    perf.frames = []
    perf.framesOn = true
    const tick = (time) => {
      if (!perf.framesOn) return
      perf.frames.push(time)
      requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  })
  for (let i = 0; i < 12; i += 1) {
    await page.mouse.wheel(0, i % 2 === 0 ? -240 : 240)
    await page.waitForTimeout(50)
  }
  await page.mouse.down({ button: 'right' })
  for (let i = 1; i <= 40; i += 1) {
    await page.mouse.move(cx + (box.width / 4) * Math.sin((i / 40) * Math.PI * 2), cy + (box.height / 8) * (i / 40))
    await page.waitForTimeout(16)
  }
  await page.mouse.up({ button: 'right' })
  const minFrames = BUDGET.minimum_samples.frames
  await expect.poll(() => page.evaluate(() => window.__leafPerf.frames.length)).toBeGreaterThan(minFrames)
  const frameDeltas = await page.evaluate(() => {
    const perf = window.__leafPerf
    perf.framesOn = false
    return perf.frames.slice(1).map((time, index) => time - perf.frames[index])
  })

  const inpP75 = typing.latencies.length ? percentile(typing.latencies, 0.75) : null
  const frameP95 = frameDeltas.length ? percentile(frameDeltas, 0.95) : null
  const budgets = BUDGET.budgets
  const breaches = []
  if (!(inpP75 <= budgets.command_bar_inp_p75_ms)) breaches.push('command_bar_inp_p75_ms')
  if (!(frameP95 <= budgets.canvas_frame_p95_ms)) breaches.push('canvas_frame_p95_ms')
  const commit = sourceCommit()
  const round = (value) => Math.round(value * 1000) / 1000
  const result = {
    schema: 'leaf.perf-budgets.result.v1',
    covers: BUDGET.covers,
    mode: ENFORCE ? 'enforce' : 'record',
    generated_at: new Date().toISOString(),
    source_commit: commit.sha,
    source_commit_source: commit.source,
    host: { hostname: hostname(), platform: platform(), arch: arch(), cpus: cpus().length },
    browser: { name: 'chromium', version: browser.version(), headless: true, viewport: page.viewportSize() },
    budgets,
    inp: {
      p75_ms: inpP75,
      samples: typing.latencies.length,
      event_timing_samples: typing.eventTimingSamples,
      typed_characters: TYPED.length,
      latencies_ms: typing.latencies.map(round),
    },
    frame: {
      p95_ms: frameP95,
      samples: frameDeltas.length,
      max_ms: frameDeltas.length ? round(Math.max(...frameDeltas)) : null,
      median_ms: frameDeltas.length ? round(percentile(frameDeltas, 0.5)) : null,
    },
    breaches,
  }
  const resultPath = testInfo.outputPath('perf-budgets.result.json')
  writeFileSync(resultPath, `${JSON.stringify(result, null, 2)}\n`)
  await testInfo.attach('perf-budgets.result.json', { path: resultPath, contentType: 'application/json' })

  expect(existsSync(resultPath)).toBe(true)
  expect(typing.latencies.length).toBeGreaterThanOrEqual(BUDGET.minimum_samples.input_events)
  expect(frameDeltas.length).toBeGreaterThanOrEqual(minFrames)
  expect(Number.isFinite(inpP75) && inpP75 > 0).toBe(true)
  expect(Number.isFinite(frameP95) && frameP95 > 0).toBe(true)
  if (ENFORCE) expect(breaches, `A25 budget breach: ${JSON.stringify({ inpP75, frameP95, budgets })}`).toEqual([])
})
