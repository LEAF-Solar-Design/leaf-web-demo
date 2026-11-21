// Dormant until explicitly selected as a Playwright CLI reporter.
// Only public TestCase, TestResult and TestStep properties are read.
import { posix } from 'node:path'

const TARGET_FILES = new Set([
  'campaign-panel.spec.mjs',
  'recovery-controls.spec.mjs',
  'workspace-create-no-prompt.spec.mjs',
  'viewer-grid-toggle.spec.mjs',
  'studio-ux-regressions.spec.mjs',
  'project-page-editor.spec.mjs',
])
const STATUSES = new Set(['passed', 'failed', 'timedOut', 'skipped', 'interrupted'])
const MAX_INTERVALS = 10000
const MAX_PARENT_DEPTH = 128
const MAX_API_LINES = 16
// Keep rounded millisecond arithmetic within safe integer microseconds.
const MAX_DURATION_MS = Math.floor(Number.MAX_SAFE_INTEGER / 1000)

function targetFile(test) {
  const file = test?.location?.file
  if (typeof file !== 'string') return null
  const basename = file.split(/[\\/]/).pop()
  return TARGET_FILES.has(basename) ? `web/e2e/${basename}` : null
}

function normalizedFile(file) {
  return typeof file === 'string' && file.length ? posix.normalize(file.replace(/\\/g, '/')) : null
}

function sourceLine(test, step) {
  const line = step?.location?.line
  return targetFile(test) && normalizedFile(step?.location?.file) !== null
    && normalizedFile(step.location.file) === normalizedFile(test?.location?.file)
    && Number.isSafeInteger(line) && line > 0 ? line : null
}

function safeNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? Math.round(Math.min(value, MAX_DURATION_MS) * 1000) / 1000 : 0
}

function timestamp(value) {
  return value instanceof Date ? Date.prototype.getTime.call(value) : NaN
}

function union(intervals) {
  const sorted = intervals.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1])
  const merged = []
  for (const [start, end] of sorted) {
    const last = merged[merged.length - 1]
    if (last && start <= last[1]) last[1] = Math.max(last[1], end)
    else merged.push([start, end])
  }
  return merged
}

// Both inputs are already unions, so subtraction is linear in their sizes.
function subtract(intervals, occupied) {
  const remaining = []
  let index = 0
  for (const [start, end] of intervals) {
    let cursor = start
    while (index < occupied.length && occupied[index][1] <= cursor) index++
    let next = index
    while (next < occupied.length && occupied[next][0] < end) {
      const [left, right] = occupied[next]
      if (left > cursor) remaining.push([cursor, Math.min(left, end)])
      cursor = Math.max(cursor, right)
      if (cursor >= end) break
      next++
    }
    if (cursor < end) remaining.push([cursor, end])
    index = next
  }
  return remaining
}

function elapsed(intervals) {
  return intervals.reduce((total, [start, end]) => total + (end - start), 0)
}

function timing(state, result) {
  const rawDuration = result?.duration
  const duration = typeof rawDuration === 'number' && Number.isFinite(rawDuration)
    && rawDuration >= 0 ? Math.min(rawDuration, MAX_DURATION_MS) : 0
  const start = timestamp(result?.startTime)
  const groups = { setup: [], teardown: [], api: [], navigation: [] }
  const lines = new Map()
  if (Number.isFinite(start)) {
    for (const item of state?.intervals || []) {
      const left = Math.max(0, item.start - start)
      const right = Math.min(duration, item.start - start + item.duration)
      if (!Number.isFinite(left) || !Number.isFinite(right) || right <= left) continue
      groups[item.kind].push([left, right])
      if (item.line !== null && item.kind === 'api') {
        if (!lines.has(item.line)) lines.set(item.line, [])
        lines.get(item.line).push([left, right])
      }
      if (item.navigation) groups.navigation.push([left, right])
    }
  }
  const setup = union(groups.setup)
  const teardown = subtract(union(groups.teardown), setup)
  const hooks = union([...setup, ...teardown])
  const api = subtract(union(groups.api), hooks)
  const navigation = subtract(union(groups.navigation), hooks)
  // Allocate rounded microseconds from one budget to preserve the partition
  // even when independent rounding would otherwise add an extra microsecond.
  const durationTicks = Math.round(duration * 1000)
  let remaining = durationTicks
  const allocate = intervals => {
    const ticks = Math.min(remaining, Math.max(0, Math.round(elapsed(intervals) * 1000)))
    remaining -= ticks
    return ticks
  }
  const setupTicks = allocate(setup)
  const teardownTicks = allocate(teardown)
  const apiTicks = allocate(api)
  const navigationTicks = Math.min(apiTicks, Math.max(0, Math.round(elapsed(navigation) * 1000)))
  // Inclusive per line: nested calls on different lines overlap, not a partition.
  // Missing public locations stay unattributed; no source text is retained.
  const apiLines = [...lines].map(([line, intervals]) => ({
    line, elapsed: elapsed(subtract(union(intervals), hooks)),
  })).filter(item => item.elapsed > 0).map(item => ({
    line: item.line, api_ms: Math.min(apiTicks, Math.round(item.elapsed * 1000)) / 1000,
  })).sort((a, b) => b.api_ms - a.api_ms || a.line - b.line)
  return {
    duration_ms: durationTicks / 1000,
    setup_ms: setupTicks / 1000,
    teardown_ms: teardownTicks / 1000,
    api_ms: apiTicks / 1000,
    reported_navigation_api_ms: navigationTicks / 1000,
    unattributed_ms: remaining / 1000,
    api_lines: apiLines.slice(0, MAX_API_LINES),
    api_lines_omitted: Math.max(0, apiLines.length - MAX_API_LINES),
  }
}

export default class PanelsTimingReporter {
  constructor() {
    this.results = new WeakMap()
    this.finished = new WeakSet()
  }

  onTestBegin(test, result) {
    try {
      if (targetFile(test) && !this.finished.has(result)) {
        this.results.set(result, { intervals: [], truncated: false })
      }
    } catch { /* Telemetry must never change the runner verdict. */ }
  }

  onStepEnd(test, result, step) {
    try {
      const state = this.results.get(result)
      if (!state || !step) return
      if (state.intervals.length >= MAX_INTERVALS) {
        state.truncated = true
        return
      }
      // Find the outermost enclosing hook group without retaining the tree.
      let enclosingHook = null
      let current = step
      let depth = 0
      while (current && depth < MAX_PARENT_DEPTH) {
        if (current.category === 'hook'
            && (current.title === 'Before Hooks' || current.title === 'After Hooks')) {
          enclosingHook = current
        }
        current = current.parent
        depth++
      }
      if (current) {
        state.truncated = true
        return
      }
      let kind
      if (enclosingHook) {
        // Descendants are covered by the enclosing group's elapsed interval.
        if (enclosingHook !== step) return
        kind = enclosingHook.title === 'Before Hooks' ? 'setup' : 'teardown'
      } else if (step.category === 'pw:api') {
        kind = 'api'
      } else return
      const start = timestamp(step.startTime)
      const duration = step.duration
      if (!Number.isFinite(start) || typeof duration !== 'number'
          || !Number.isFinite(duration) || duration < 0) return
      state.intervals.push({
        kind, start, duration,
        line: kind === 'api' ? sourceLine(test, step) : null,
        // Public API names only; this is deliberately not all navigation.
        navigation: kind === 'api' && typeof step.title === 'string'
          && /^(?:page|frame)\.(?:goto|reload)(?:\s|\(|$)/.test(step.title),
      })
      if (state.intervals.length === MAX_INTERVALS) state.truncated = true
    } catch { /* Drop unsupported telemetry rather than fail a test. */ }
  }

  onTestEnd(test, result) {
    try {
      if (this.finished.has(result)) return
      const state = this.results.get(result)
      this.results.delete(result)
      this.finished.add(result)
      const file = targetFile(test)
      if (!file) return
      const row = {
        schema: 'leaf.ci.playwright-phase.v1',
        file,
        line: safeNumber(test?.location?.line),
        column: safeNumber(test?.location?.column),
        worker_index: safeNumber(result?.workerIndex),
        retry: safeNumber(result?.retry),
        repeat_index: safeNumber(test?.repeatEachIndex),
        status: STATUSES.has(result?.status) ? result.status : 'unknown',
        ...timing(state, result),
        truncated: state?.truncated === true,
        basis: 'playwright public test/step durations; gaps unattributed',
      }
      process.stdout.write('LEAF_PW_PHASE ' + JSON.stringify(row) + '\n')
    } catch { /* No errors, titles or other raw runner data may be printed. */ }
  }
}
