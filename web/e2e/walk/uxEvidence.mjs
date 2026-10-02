export const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/
export const MAX_UX_OBSERVATIONS = 64

export const assertionAlias = (metricId) => 'ux-' + metricId.replaceAll('_', '-')

function geometry(sample) {
  if (!sample || sample.visible !== true || sample.enabled !== true) return null
  const { rect, viewport } = sample
  if (!rect || !viewport || ![rect.x, rect.y, rect.width, rect.height, viewport.width, viewport.height]
    .every((value) => typeof value === 'number' && Number.isFinite(value))) return null
  if (rect.width <= 0 || rect.height <= 0 || viewport.width <= 0 || viewport.height <= 0) return null
  const right = rect.x + rect.width
  const bottom = rect.y + rect.height
  if (!Number.isFinite(right) || !Number.isFinite(bottom)) return null
  return { rect, viewport, right, bottom }
}

export function coveredAtRest(sample) {
  const box = geometry(sample)
  if (!box) return null
  const { rect, viewport } = box
  const x = rect.x + rect.width / 2
  const y = rect.y + rect.height / 2
  if (x < 0 || y < 0 || x >= viewport.width || y >= viewport.height) return null
  if (sample.hit === 'other') return 1
  if (sample.hit === 'self' || sample.hit === 'descendant') return 0
  return null
}

export function scrollNeeded(sample) {
  const box = geometry(sample)
  if (!box) return null
  const { rect, viewport, right, bottom } = box
  return rect.x < 0 || rect.y < 0 || right > viewport.width || bottom > viewport.height ? 1 : 0
}

export function uxObservation(input) {
  const fields = ['lensId', 'metricId', 'viewport', 'state', 'observed']
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new TypeError('observation must be an object')
  for (const key of Reflect.ownKeys(input)) {
    if (!fields.includes(key)) throw new TypeError(`Unexpected field ${String(key)}`)
  }
  for (const field of fields.slice(0, 4)) {
    if (typeof input[field] !== 'string' || !ID.test(input[field])) throw new TypeError(`Invalid ${field}`)
  }
  const { lensId, metricId, viewport, state, observed } = input
  if (observed !== null && (typeof observed !== 'number' || !Number.isFinite(observed))) throw new TypeError('Invalid observed')
  return Object.freeze({ ux_version: 1, lens_id: lensId, metric_id: metricId, viewport, state, observed })
}

export function packUxEvidence(observations) {
  if (!Array.isArray(observations)) throw new TypeError('ux_observations must be an array')
  if (observations.length > MAX_UX_OBSERVATIONS) throw new TypeError('ux_observations exceeds 64 bound')
  const fields = ['ux_version', 'lens_id', 'metric_id', 'viewport', 'state', 'observed']
  const packed = Array.from(observations, (row) => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) throw new TypeError('Invalid observation shape')
    for (const key of Reflect.ownKeys(row)) {
      if (!fields.includes(key)) throw new TypeError(`Unexpected field ${String(key)}`)
    }
    for (const field of fields) if (!Object.hasOwn(row, field)) throw new TypeError(`Missing ${field}`)
    if (row.ux_version !== 1) throw new TypeError('Invalid ux_version')
    return uxObservation({ lensId: row.lens_id, metricId: row.metric_id,
      viewport: row.viewport, state: row.state, observed: row.observed })
  })
  return { ux_observations: packed }
}

export async function collectControlSample(evaluate, handle) {
  return evaluate((control) => {
    const rect = control.getBoundingClientRect()
    const view = control.ownerDocument.defaultView
    const style = view.getComputedStyle(control)
    const hit = control.ownerDocument.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)
    return {
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      viewport: { width: view.innerWidth, height: view.innerHeight },
      hit: hit === control ? 'self' : hit && control.contains(hit) ? 'descendant' : hit ? 'other' : 'none',
      enabled: !control.matches(':disabled') && control.getAttribute('aria-disabled') !== 'true',
      visible: style.display !== 'none' && !['hidden', 'collapse'].includes(style.visibility)
        && rect.width > 0 && rect.height > 0,
    }
  }, handle)
}

// Read-only, optional evidence: a slow or ambiguous target must not hold up
// activation or supply a measurement from the post-activation page.
export async function collectProbeUxEvidence(probe, locator, viewport, {
  setTimer = setTimeout, clearTimer = clearTimeout,
} = {}) {
  if (!probe.locator?.role || ['surface', 'journey', 'census'].includes(probe.kind)) return []
  let timer
  let expired = false
  const timeout = new Promise((resolve) => {
    timer = setTimer(() => { expired = true; resolve([]) }, 2000)
  })
  const collect = async () => {
    await locator.waitFor({ state: 'attached', timeout: 2000 })
    if (expired || await locator.count() !== 1 || expired) return []
    const sample = await collectControlSample((evaluate) => locator.evaluate(evaluate, undefined, { timeout: 2000 }))
    if (expired || await locator.count() !== 1 || expired) return []
    let state = String(probe.state).replace(/[^A-Za-z0-9_-]/g, '-')
    if (!/^[A-Za-z0-9]/.test(state)) state = 'state-' + state
    state = state.slice(0, 64)
    return [['control_covered_at_rest', coveredAtRest(sample)], ['scroll_needed_steps', scrollNeeded(sample)]]
      .filter(([, observed]) => observed !== null)
      .map(([metricId, observed]) => uxObservation({ lensId: 'reachability', metricId, viewport, state, observed }))
  }
  try { return await Promise.race([collect().catch(() => []), timeout]) }
  finally { clearTimer(timer) }
}
