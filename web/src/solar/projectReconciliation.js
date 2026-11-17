import { decodeGroundFrameSlots } from './solarGroundSlots.js'
import { createSolarDrawingBinding } from './solarDrawingBinding.js'

export const RECONCILIATION_METRICS = ['Panels', 'Optimizers', 'Inverters', 'Strings', 'kW DC']
const ID = /^leaf:[a-z][a-z0-9-]*:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const validId = (id, kind) => typeof id === 'string' && id.length <= 100 && id.startsWith(`leaf:${kind}:`) && ID.test(id)
const SCALE = 10n ** 18n
const LIMIT = 10n ** 36n

// Eighteen integer and fractional digits; all arithmetic stays in scaled integers.
export function parseReconciliationNumber(value, count = false) {
  if (typeof value !== 'string' || value.length > 37 ||
      !(count ? /^\d{1,16}$/ : /^\d{1,18}(?:\.\d{1,18})?$/).test(value)) return null
  const [whole, fraction = ''] = value.split('.')
  const scaled = BigInt(whole) * SCALE + BigInt(fraction.padEnd(18, '0'))
  if (count && BigInt(whole) > BigInt(Number.MAX_SAFE_INTEGER)) return null
  return scaled < LIMIT ? scaled : null
}

export function formatReconciliationNumber(value) {
  const sign = value < 0n ? '-' : ''
  const digits = (value < 0n ? -value : value).toString().padStart(19, '0')
  const fraction = digits.slice(-18).replace(/0+$/, '')
  return sign + digits.slice(0, -18) + (fraction ? '.' + fraction : '')
}

function watts(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null
  let text = String(value)
  if (text.includes('e')) {
    const [mantissa, exponent] = text.split('e')
    const [whole, fraction = ''] = mantissa.split('.')
    const digits = whole + fraction, point = whole.length + Number(exponent)
    if (point > 18 || digits.length - point > 15) return null
    text = point <= 0 ? '0.' + '0'.repeat(-point) + digits
      : point >= digits.length ? digits + '0'.repeat(point - digits.length)
        : digits.slice(0, point) + '.' + digits.slice(point)
  }
  const parsed = parseReconciliationNumber(text)
  // Division by 1000 must remain exact within the supported DC scale.
  return parsed !== null && parsed % 1000n === 0n ? parsed : null
}

export function validReconciliationResponse(response, binding) {
  try {
    const owner = createSolarDrawingBinding(binding), data = response?.data
    const graph = data?.intake?.solar_design_graph
    return response?.ok === true && ['organization_id', 'project_id', 'drawing_id', 'input_version_id']
      .every((key) => data?.context?.[key] === owner[key]) &&
      Number.isSafeInteger(graph?.rev) && graph.rev >= 0 &&
      ['frames', 'panels', 'strings', 'inverters'].every((key) => Array.isArray(graph[key])) &&
      typeof data.intake.solar_design_graph_sha256 === 'string' && /^[0-9a-f]{64}$/i.test(data.intake.solar_design_graph_sha256)
  } catch { return false }
}

function uniqueIds(items, kind) {
  const ids = new Set()
  for (const item of items) {
    if (!validId(item?.id, kind) || ids.has(item.id)) return null
    ids.add(item.id)
  }
  return ids
}

export function savedReconciliationValues(graph) {
  const unavailable = (reason) => ({ value: null, reason })
  const values = RECONCILIATION_METRICS.map(() => unavailable('Read a valid saved design first.'))
  values[1] = unavailable('The saved graph has no explicit measured optimizer count.')
  if (!graph || !['frames', 'panels', 'strings', 'inverters'].every((key) => Array.isArray(graph[key]))) return values
  const available = (value) => ({ value, reason: '' })
  const decoded = decodeGroundFrameSlots(graph.frames)
  const panels = uniqueIds(graph.panels, 'panel')
  let panelValid = panels !== null && decoded !== null
  if (panelValid) for (const id of decoded.panelIds) {
    if (!validId(id, 'panel') || panels.has(id)) panelValid = false
    panels.add(id)
  }
  values[0] = panelValid ? available(BigInt(panels.size) * SCALE)
    : unavailable('Panel IDs or compact Ground slots are invalid or duplicated.')
  const strings = uniqueIds(graph.strings, 'string')
  const assigned = new Set()
  let overlap = false
  const stringsValid = panelValid && strings && graph.strings.every((item) => Array.isArray(item.ordered_panel_refs) &&
    Number.isSafeInteger(item.module_count) && item.module_count > 0 && item.module_count === item.ordered_panel_refs.length &&
    new Set(item.ordered_panel_refs).size === item.ordered_panel_refs.length &&
    item.ordered_panel_refs.every((id) => {
      if (!validId(id, 'panel') || !panels.has(id)) return false
      if (assigned.has(id)) { overlap = true; return false }
      assigned.add(id)
      return true
    }))
  values[3] = stringsValid ? available(BigInt(strings.size) * SCALE)
    : unavailable(overlap ? 'A saved panel reference belongs to more than one string.'
      : 'Saved panel union is invalid, or string IDs, positive module counts and ordered panel references disagree.')
  const devices = uniqueIds(graph.inverters, 'inverter')
  const types = ['string_inverter', 'central_inverter', 'combiner_box']
  values[2] = devices && graph.inverters.every((item) => types.includes(item.equipment_type))
    ? available(BigInt(graph.inverters.filter((item) => item.equipment_type !== 'combiner_box').length) * SCALE)
    : unavailable('Saved device IDs or equipment types are missing, unknown or duplicated.')
  let dcValid = panelValid && panels.size > 0 && uniqueIds(graph.frames, 'frame') !== null
  const covered = new Set(), ground = new Map(decoded?.frames.map((frame) => [frame.frameIndex, frame.slots.map((slot) => slot.panelId)]) || [])
  let total = 0n
  for (const [index, frame] of graph.frames.entries()) {
    const refs = ground.has(index) ? ground.get(index) : frame?.panel_refs
    const power = watts(frame?.module_power_watts)
    if (!Array.isArray(refs) || power === null) { dcValid = false; continue }
    for (const ref of refs) {
      if (!validId(ref, 'panel') || !panels?.has(ref) || covered.has(ref)) dcValid = false
      covered.add(ref)
      total += power / 1000n
      if (total >= LIMIT) dcValid = false
    }
  }
  values[4] = dcValid && covered.size === panels.size ? available(total)
    : unavailable('DC needs complete, unique frame coverage and positive supported module watts for every panel.')
  return values
}

export function reconciliationRows(graph, drafts = []) {
  return reconciliationRowsFromValues(savedReconciliationValues(graph), drafts)
}

export function reconciliationRowsFromValues(studio, drafts = []) {
  return RECONCILIATION_METRICS.map((metric, index) => {
    const oracle = parseReconciliationNumber(drafts[index], index !== 4)
    const value = studio[index].value
    const delta = value !== null && oracle !== null ? value - oracle : null
    return { metric, studio: value === null ? '' : formatReconciliationNumber(value),
      oracle: oracle === null ? '' : drafts[index],
      delta: delta === null ? '' : formatReconciliationNumber(delta),
      verdict: delta === null ? 'UNREADABLE' : delta === 0n ? 'PASS' : 'FAIL',
      reason: studio[index].reason || (oracle === null ? 'Supply a valid engineering report/BOM value.' : '') }
  })
}

export function reconciliationCsv(rows) {
  const quote = (value) => '"' + value.replace(/"/g, '""') + '"'
  const metrics = ['panels', 'optimizers', 'inverters', 'strings', 'kw_dc']
  return 'metric,studio,oracle,delta,verdict\r\n' + rows.map((row, index) =>
    [metrics[index], row.studio, row.oracle, row.delta, row.verdict].map(quote).join(',')).join('\r\n') + '\r\n'
}
