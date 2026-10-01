import { createHash } from 'node:crypto'
import { performance } from 'node:perf_hooks'
import { PNG } from 'pngjs'
import pixelmatch from 'pixelmatch'

/**
 * Evidence contract (leaf.walk-evidence.v1): retain steps[].name/elapsedMs,
 * phase:'setup', timeToTaskMs and accessibility.violations/incomplete verbatim.
 * Additional capture fields live in measurements, never inferred from names.
 * Journey measurements: baselineId, goldenPathVersion, scope, captureComplete,
 * task:{startedMs,completedMs,success,oracle}, faults:[{id,injectedMs,
 * completedMs,outcome:'recovered'|'unrecovered'}]. Timestamps share one monotonic
 * clock. task.completedMs is the success oracle observation, not teardown.
 * Step measurements: kind:'user-action'|'setup'|'assertion', actionType,
 * featureId, startedMs, completedMs, outcome:'success'|'dead-end'|'failed',
 * recoveryFor (fault ID), preAction:{targetBounds:{x,y,width,height},
 * viewport:{width,height},scrollPosition:{x,y}}, scrollRequired:boolean,
 * scrollEvents:[{x,y,atMs,required:boolean}]. Bounds are viewport-relative and
 * measured BEFORE an action can scroll the target. Missing fields are unknown.
 * Baseline: {id,goldenPathVersion,scope,goldenUserActionCount}; the golden path
 * is authored, and does not claim measured discoverability. Capture completion
 * attests that all actions, unsuccessful branches and injected faults are present.
 * Legacy timeToTaskMs is an explicit completion measurement from the v1 journey;
 * a failure always overrides it. Other legacy fields cannot prove action counts.
 * Axe input: {scope,accessibility:{violations,incomplete}}. Pixel provenance:
 * {before,after,threshold}, each side including deploymentId and capture scope.
 * masks are measured {x,y,width,height,source} rectangles in PNG pixel
 * coordinates, not CSS coordinates; deviceScale is checked against dimensions.
 * Results can be placed in finding.evidence with category/severity/assertion_id;
 * pixel differences are review evidence, never automatic functional defects.
 * Dependencies are already declared: in a fresh worktree run npm ci in web
 * before verification (the executor does not install or modify dependencies).
 */
const clone = (value) => value === undefined ? null : structuredClone(value)
const known = (value, raw = null) => ({ status: 'known', value, raw: clone(raw) })
const unknown = (reason, raw = null) => ({ status: 'unknown', value: null, reason, raw: clone(raw) })
const finite = (n) => typeof n === 'number' && Number.isFinite(n)
const time = (n) => finite(n) && n >= 0
const text = (s) => typeof s === 'string' && s.length > 0
const stable = (v) => JSON.stringify(normalize(v))
function normalize(v) {
  if (Array.isArray(v)) return v.map(normalize)
  if (v && typeof v === 'object') return Object.fromEntries(Object.keys(v).sort().map((key) => [key, normalize(v[key])]))
  return v
}
const scopeKeys = ['journey', 'state', 'fixtureVersion', 'catalogVersion', 'viewport', 'browser']
function scopeReason(a, b, keys = scopeKeys) {
  for (const key of keys) {
    if (a?.[key] === undefined || b?.[key] === undefined || a[key] === null || b[key] === null || a[key] === '') return `Missing scope: ${key}`
    for (const value of [a[key], b[key]]) {
      if (typeof value === 'object' && (Array.isArray(value) || !Object.keys(value).length)) return `Malformed scope: ${key}`
      if (key === 'viewport' && (typeof value !== 'object' || !finite(value.width) || !finite(value.height) || value.width <= 0 || value.height <= 0)) return 'Malformed scope: viewport'
    }
    if (stable(a[key]) !== stable(b[key])) return `Incompatible scope: ${key}`
  }
  return null
}
function geometryReason(pre) {
  const b = pre?.targetBounds, v = pre?.viewport, s = pre?.scrollPosition
  if (!b || !v || !s || ![b.x, b.y, b.width, b.height, v.width, v.height, s.x, s.y].every(finite)
    || b.width <= 0 || b.height <= 0 || v.width <= 0 || v.height <= 0) return 'Missing or malformed pre-action geometry'
  return null
}
const failed = (e) => !!e?.failure || ['failed', 'timedOut', 'interrupted', 'skipped'].includes(e?.status)
  || ['failed', 'unsupported_local', 'skipped'].includes(e?.result?.result) || e?.measurements?.task?.success === false

export function computeJourneyMetrics(evidence, baseline) {
  const e = evidence || {}, m = e.measurements || {}, task = m.task
  const steps = Array.isArray(e.steps) ? e.steps : []
  const actions = steps.filter((s) => s?.phase !== 'setup' && s?.phase !== 'assertion' && s?.measurements?.kind === 'user-action')
  const result = {
    raw: clone(e), baseline: clone(baseline), elapsed_ms: time(e.elapsedMs) ? known(e.elapsedMs) : unknown('Missing elapsedMs'),
  }
  let taskReason = failed(e) ? 'Journey failed; elapsed time is not successful time to task' : null
  let duration
  if (!taskReason && task) {
    if (task.success !== true || !text(task.oracle)) taskReason = 'Missing observed success oracle'
    else if (!time(task.startedMs) || !time(task.completedMs) || task.completedMs < task.startedMs) taskReason = 'Missing or invalid monotonic task timestamps'
    else duration = task.completedMs - task.startedMs
  } else if (!taskReason) {
    if (time(e.timeToTaskMs)) duration = e.timeToTaskMs
    else taskReason = 'Missing timeToTaskMs or observed success oracle'
  }
  result.time_to_task_ms = taskReason ? unknown(taskReason, task) : known(duration, task || { timeToTaskMs: e.timeToTaskMs, source: 'v1 completion measurement' })
  let traceReason = taskReason
  if (!traceReason && (!Array.isArray(e.steps) || m.captureComplete !== true)) traceReason = 'Missing complete classified action trace'
  if (!traceReason && steps.some((s) => !s || typeof s !== 'object' || Array.isArray(s))) traceReason = 'Malformed step record'
  if (!traceReason && steps.some((s) => s.phase !== 'setup' && s.phase !== 'assertion'
    && !['user-action', 'setup', 'assertion'].includes(s.measurements?.kind))) traceReason = 'Unclassified step; a name is not an action'
  if (!traceReason && actions.some((s) => !['success', 'dead-end', 'failed'].includes(s.measurements.outcome))) traceReason = 'Missing explicit action outcome'
  if (!traceReason && actions.some((s) => !time(s.measurements.startedMs) || !time(s.measurements.completedMs)
    || s.measurements.completedMs < s.measurements.startedMs)) traceReason = 'Missing or invalid action timestamps'
  if (!traceReason && actions.some((s, i) => i > 0 && s.measurements.startedMs < actions[i - 1].measurements.completedMs)) traceReason = 'Non-monotonic action trace'
  if (!traceReason && task && actions.some((s) => s.measurements.startedMs < task.startedMs || s.measurements.completedMs > task.completedMs)) traceReason = 'Action lies outside observed task interval'
  let comparable = traceReason
  if (!comparable && (!text(baseline?.id) || !text(baseline?.goldenPathVersion)
    || m.baselineId !== baseline.id || m.goldenPathVersion !== baseline.goldenPathVersion)) comparable = 'Missing or incompatible golden path identity'
  if (!comparable) comparable = scopeReason(m.scope, baseline.scope)
  if (!comparable && (!Number.isInteger(baseline.goldenUserActionCount) || baseline.goldenUserActionCount < 0)) comparable = 'Missing golden user-action count'
  result.user_action_count = traceReason ? unknown(traceReason) : known(actions.length)
  result.extra_steps = comparable ? unknown(comparable) : known(Math.max(0, actions.length - baseline.goldenUserActionCount), { observed: actions.length, golden: baseline.goldenUserActionCount })
  result.dead_ends = traceReason ? unknown(traceReason) : known(actions.filter((s) => s.measurements.outcome === 'dead-end').length)
  const requiredScroll = (m) => m.scrollRequired === true || (Array.isArray(m.scrollEvents) && m.scrollEvents.some((event) => event.required === true))
  const badGeometry = actions.map((s) => s.measurements.preAction == null && requiredScroll(s.measurements)
    ? null : geometryReason(s.measurements.preAction)).find(Boolean)
  const badScroll = actions.some((s) => s.measurements.scrollRequired !== undefined && typeof s.measurements.scrollRequired !== 'boolean'
    || s.measurements.scrollEvents !== undefined && (!Array.isArray(s.measurements.scrollEvents)
      || s.measurements.scrollEvents.some((event) => !finite(event.x) || !finite(event.y) || !time(event.atMs) || typeof event.required !== 'boolean')))
  result.scroll_needed_steps = traceReason || badGeometry || badScroll ? unknown(traceReason || badGeometry || 'Malformed scroll evidence') : known(actions.filter((s) => {
    if (requiredScroll(s.measurements)) return true
    const { targetBounds: b, viewport: v } = s.measurements.preAction
    return b.x < 0 || b.y < 0 || b.x + b.width > v.width || b.y + b.height > v.height
      || s.measurements.scrollRequired === true || s.measurements.scrollEvents?.some((event) => event.required === true)
  }).length, actions.map((s) => ({ name: s.name, measurements: s.measurements })))
  const faults = m.faults
  let recoveryReason = traceReason
  if (!recoveryReason && (!Array.isArray(faults) || faults.some((f) => !text(f.id))
    || new Set(faults.map((f) => f.id)).size !== faults.length)) recoveryReason = 'Missing or malformed injected fault inventory'
  if (!recoveryReason && actions.some((s) => s.measurements.recoveryFor !== undefined && !faults.some((f) => f.id === s.measurements.recoveryFor))) recoveryReason = 'Recovery refers to an unknown fault'
  result.recovery = recoveryReason ? unknown(recoveryReason, faults) : known(faults.map((f) => {
    const attempts = actions.filter((s) => s.measurements.recoveryFor === f.id)
    if (!time(f.injectedMs) || !time(f.completedMs) || f.completedMs < f.injectedMs
      || !['recovered', 'unrecovered'].includes(f.outcome)
      || attempts.some((s) => s.measurements.startedMs < f.injectedMs || s.measurements.completedMs > f.completedMs)) return { faultId: f.id, ...unknown('Missing or invalid fault completion evidence', f) }
    return { faultId: f.id, status: 'known', attempted: attempts.length > 0, recovered: f.outcome === 'recovered', actions_needed: attempts.length, elapsed_ms: f.completedMs - f.injectedMs, raw: clone(f) }
  }), faults)
  return result
}

/** Bounded recorder; overflow is explicit and makes counts unknown. */
export function createJourneyStepCapture(evidence, { baseline, clock = () => performance.now(), maxSteps = 256, maxScrollEvents = 64 } = {}) {
  if (!Number.isInteger(maxSteps) || maxSteps < 1 || !Number.isInteger(maxScrollEvents) || maxScrollEvents < 1) throw new RangeError('Invalid capture bounds')
  evidence.steps ||= []
  const now = () => {
    const n = clock()
    if (!time(n) || n < last) throw new Error('Capture clock must be monotonic')
    last = n
    return n
  }
  let last = 0
  evidence.measurements = { ...evidence.measurements, baselineId: baseline?.id, goldenPathVersion: baseline?.goldenPathVersion,
    scope: clone(baseline?.scope), captureComplete: false, faults: [], task: { startedMs: now(), success: false } }
  let overflow = false
  return {
    async capture({ name, phase, actionType, featureId, recoveryFor, readGeometry, outcome, scrollEvents = [], scrollRequired }, action) {
      if (evidence.measurements.task.completedMs !== undefined) throw new Error('Task capture already completed')
      if (evidence.steps.length >= maxSteps) { overflow = true; throw new RangeError('Journey step capture limit exceeded') }
      const preAction = readGeometry ? clone(await readGeometry()) : null
      const startedMs = now()
      const measurements = { kind: phase === 'setup' || phase === 'assertion' ? phase : 'user-action', actionType, featureId,
        recoveryFor, startedMs, preAction, scrollRequired, scrollEvents: [] }
      const step = { name, ...(phase ? { phase } : {}), elapsedMs: null, measurements }
      evidence.steps.push(step)
      try {
        const value = await action()
        measurements.outcome = typeof outcome === 'function' ? await outcome(value) : outcome
        return value
      } catch (error) { measurements.outcome = 'failed'; throw error }
      finally {
        measurements.completedMs = now()
        step.elapsedMs = measurements.completedMs - startedMs
        if (!Array.isArray(scrollEvents) || scrollEvents.length > maxScrollEvents) overflow = true
        measurements.scrollEvents = clone(Array.isArray(scrollEvents) ? scrollEvents.slice(0, maxScrollEvents) : [])
      }
    },
    injectFault(id) {
      if (evidence.measurements.task.completedMs !== undefined) throw new Error('Task capture already completed')
      if (!text(id) || evidence.measurements.faults.some((f) => f.id === id)) throw new Error('Fault ID must be unique')
      if (evidence.measurements.faults.length >= maxSteps) { overflow = true; throw new RangeError('Fault capture limit exceeded') }
      evidence.measurements.faults.push({ id, injectedMs: now() })
    },
    completeFault(id, recovered) {
      if (evidence.measurements.task.completedMs !== undefined) throw new Error('Task capture already completed')
      const fault = evidence.measurements.faults.find((f) => f.id === id)
      if (!fault || fault.completedMs !== undefined || typeof recovered !== 'boolean') throw new Error('Invalid fault completion')
      Object.assign(fault, { completedMs: now(), outcome: recovered ? 'recovered' : 'unrecovered' })
    },
    finishTask(oracle, success) {
      if (!text(oracle) || typeof success !== 'boolean' || evidence.measurements.task.completedMs !== undefined) throw new Error('Explicit single task oracle required')
      Object.assign(evidence.measurements.task, { completedMs: now(), oracle, success })
      evidence.measurements.captureComplete = !overflow
      if (success) evidence.timeToTaskMs = evidence.measurements.task.completedMs - evidence.measurements.task.startedMs
    },
  }
}

/** Read-only Playwright helper: evaluate does not scroll the target. */
export async function captureTargetGeometry(page, locator) {
  return locator.evaluate((element) => {
    const b = element.getBoundingClientRect()
    return { targetBounds: { x: b.x, y: b.y, width: b.width, height: b.height },
      viewport: { width: innerWidth, height: innerHeight }, scrollPosition: { x: scrollX, y: scrollY } }
  })
}

function axeInstances(accessibility) {
  if (!Array.isArray(accessibility?.violations) || !Array.isArray(accessibility?.incomplete)) throw new Error('Missing axe violations/incomplete')
  const instances = new Map()
  for (const rule of accessibility.violations) {
    if (!text(rule.id) || !Array.isArray(rule.nodes) || rule.nodes.length === 0) throw new Error('Missing axe rule target context')
    for (const node of rule.nodes) {
      if (!Array.isArray(node.target) || !node.target.length || node.target.some((target) => !text(target) && !(Array.isArray(target) && target.length && target.every(text)))) throw new Error('Missing stable axe target')
      const key = stable([rule.id, node.target])
      instances.set(key, { ruleId: rule.id, target: clone(node.target), impact: rule.impact ?? null, node: clone(node) })
    }
  }
  return instances
}
export function diffAxe(current, baseline) {
  const raw = { current: clone(current), baseline: clone(baseline) }
  const reason = scopeReason(current?.scope, baseline?.scope, [...scopeKeys, 'axeVersion'])
  if (reason) return unknown(reason, raw)
  try {
    const a = axeInstances(current.accessibility), b = axeInstances(baseline.accessibility)
    const keys = (map) => [...map.keys()].sort()
    return { status: 'known', introduced: keys(a).filter((k) => !b.has(k)).map((k) => a.get(k)),
      unchanged: keys(a).filter((k) => b.has(k)).map((k) => a.get(k)), resolved: keys(b).filter((k) => !a.has(k)).map((k) => b.get(k)),
      incomplete: [...current.accessibility.incomplete, ...baseline.accessibility.incomplete].length
        ? unknown('Axe incomplete checks remain unknown', { current: current.accessibility.incomplete, baseline: baseline.accessibility.incomplete })
        : known([], { current: [], baseline: [] }), raw }
  } catch (error) { return unknown(error.message, raw) }
}

export function diffPixels({ before, after, provenance, masks } = {}) {
  const raw = { provenance: clone(provenance), masks: clone(masks), threshold: provenance?.threshold,
    imageHashes: { before: Buffer.isBuffer(before) ? createHash('sha256').update(before).digest('hex') : null,
      after: Buffer.isBuffer(after) ? createHash('sha256').update(after).digest('hex') : null } }
  const refuse = (reason) => unknown(reason, raw)
  const reason = scopeReason(provenance?.before, provenance?.after, [...scopeKeys, 'deviceScale', 'captureConditions'])
  if (reason) return refuse(reason)
  if (!text(provenance.before.deploymentId) || !text(provenance.after.deploymentId)) return refuse('Both deployment identities required')
  if (!finite(provenance.threshold) || provenance.threshold < 0 || provenance.threshold > 1) return refuse('Explicit pixelmatch threshold required')
  if (!Buffer.isBuffer(before) || !Buffer.isBuffer(after)) return refuse('PNG buffers required')
  try {
    const a = PNG.sync.read(before, { checkCRC: true }), b = PNG.sync.read(after, { checkCRC: true })
    raw.dimensions = { before: { width: a.width, height: a.height }, after: { width: b.width, height: b.height } }
    if (a.width !== b.width || a.height !== b.height) return refuse('Image dimensions differ')
    const viewport = provenance.before.viewport, scale = provenance.before.deviceScale
    if (!finite(scale) || scale <= 0 || !finite(viewport?.width) || !finite(viewport?.height)
      || a.width !== Math.round(viewport.width * scale) || a.height !== Math.round(viewport.height * scale)) return refuse('PNG dimensions disagree with viewport/device scale')
    if (!Array.isArray(masks)) return refuse('Explicit measured masks array required')
    const masked = new Uint8Array(a.width * a.height)
    for (const rect of masks) {
      if (!text(rect.source) || ![rect.x, rect.y, rect.width, rect.height].every(Number.isInteger)
        || rect.x < 0 || rect.y < 0 || rect.width <= 0 || rect.height <= 0
        || rect.x + rect.width > a.width || rect.y + rect.height > a.height) return refuse('Invalid or out-of-bounds measured mask')
      for (let y = rect.y; y < rect.y + rect.height; y++) for (let x = rect.x; x < rect.x + rect.width; x++) masked[y * a.width + x] = 1
    }
    raw.comparedPixelCount = masked.reduce((count, mask) => count + (mask ? 0 : 1), 0)
    if (!raw.comparedPixelCount) return refuse('No unmasked pixels to compare')
    // Flatten to a one-row image to prevent masked neighbor colors affecting
    // antialias detection. includeAA counts every threshold-crossing pixel.
    const left = Buffer.alloc(raw.comparedPixelCount * 4), right = Buffer.alloc(left.length)
    let offset = 0
    for (let i = 0; i < masked.length; i++) if (!masked[i]) {
      a.data.copy(left, offset, i * 4, i * 4 + 4); b.data.copy(right, offset, i * 4, i * 4 + 4); offset += 4
    }
    const changedPixelCount = pixelmatch(left, right, null, raw.comparedPixelCount, 1, { threshold: provenance.threshold, includeAA: true })
    return { status: 'known', comparedPixelCount: raw.comparedPixelCount, changedPixelCount,
      ratio: changedPixelCount / raw.comparedPixelCount, disposition: 'review-evidence', raw }
  } catch (error) { return refuse(`PNG comparison error: ${error.message}`) }
}
