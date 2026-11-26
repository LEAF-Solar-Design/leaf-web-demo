import { decodeGroundFrameSlots } from './solarGroundSlots.js'
import { createSolarDrawingBinding } from './solarDrawingBinding.js'

export const RECONCILIATION_METRICS = ['Panels', 'Optimizers', 'Inverters', 'Strings', 'kW DC']
const ID = /^leaf:[a-z][a-z0-9-]*:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const validId = (id, kind) => typeof id === 'string' && id.length <= 100 && id.startsWith(`leaf:${kind}:`) && ID.test(id)
const SCALE = 10n ** 18n
const LIMIT = 10n ** 36n

export function engineeringTotalsPath(binding) {
  try {
    const owner = createSolarDrawingBinding(binding)
    return `.leaf/engineering-reconciliation/${owner.drawing_id}/${owner.input_version_id}.json`
  } catch { return null }
}

export function createEngineeringTotalsRecord(binding, graphSha256, drafts) {
  try {
    const owner = createSolarDrawingBinding(binding)
    if (typeof graphSha256 !== 'string' || !/^[0-9a-f]{64}$/.test(graphSha256) ||
        !Array.isArray(drafts) || drafts.length !== 5) return null
    const values = []
    for (let index = 0; index < 5; index++) {
      const descriptor = Object.getOwnPropertyDescriptor(drafts, String(index))
      if (!descriptor || !Object.hasOwn(descriptor, 'value') || typeof descriptor.value !== 'string') return null
      const value = descriptor.value
      if (!(index === 4 && value === '') && parseReconciliationNumber(value, index !== 4) === null) return null
      values.push(value)
    }
    return { schema: 'leaf.engineering-reconciliation.v1', binding: {
      organization_id: owner.organization_id, project_id: owner.project_id,
      drawing_id: owner.drawing_id, input_version_id: owner.input_version_id,
    }, graph_sha256: graphSha256, values, provenance: 'user_supplied_unverified' }
  } catch { return null }
}

// Private writer format: exactly JSON.stringify(record), with the field order above.
// Reconstructing and comparing bytes rejects duplicate/unknown fields and alternate encodings.
export function parseEngineeringTotalsRecord(content, binding, graphSha256) {
  try {
    if (typeof content !== 'string' || content.length > 4096 || new TextEncoder().encode(content).byteLength > 4096) return null
    const parsed = JSON.parse(content)
    const record = createEngineeringTotalsRecord(binding, graphSha256, parsed?.values)
    return record && JSON.stringify(record) === content ? record : null
  } catch { return null }
}

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

export function savedOptimizerMeasurement(graph) {
  try {
    const record = graph?.extra?.solaredge_import, measurement = record?.optimizer_measurement
    const hash = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
    const index = value => Number.isSafeInteger(value) && value >= 0 && value <= 1000000
    if (record?.schema !== 'leaf.solar-project-solaredge-import.v1' ||
        measurement?.schema !== 'leaf.solaredge-optimizer-measurement.v1' ||
        measurement.complete !== true || !Number.isSafeInteger(graph.rev) ||
        measurement.graph_rev !== graph.rev ||
        !Array.isArray(measurement.optimizers) || measurement.optimizers.length > 100000 ||
        !Array.isArray(measurement.string_counts) || !Array.isArray(record.strings) ||
        !Array.isArray(record.frames) || !Array.isArray(record.unassigned_panel_refs) ||
        record.unassigned_panel_refs.length ||
        !hash(record.source?.artifact_id) || !hash(record.source?.content_sha256) ||
        !hash(record.report?.artifact_id) || !hash(record.report?.content_sha256) || !hash(record.report?.graph_sha256) ||
        (record.acceptance && record.acceptance.status !== 'complete') ||
        !Array.isArray(graph.panels) || !Array.isArray(graph.frames)) return null
    const panels = uniqueIds(graph.panels, 'panel'), frames = uniqueIds(graph.frames, 'frame')
    if (!panels || !frames || record.frames.length !== frames.size ||
        record.strings.length !== measurement.string_counts.length) return null
    const panelHandles = new Map(graph.panels.map(p => [p.id, p.provenance?.source_handle?.toUpperCase()]))
    const frameOf = new Map(), matched = new Set()
    for (const frame of graph.frames) {
      if (!Array.isArray(frame.panel_refs) || frame.ground_slots) return null
      for (const ref of frame.panel_refs) {
        if (!panels.has(ref) || frameOf.has(ref)) return null
        frameOf.set(ref, frame.id)
      }
    }
    if (frameOf.size !== panels.size) return null
    for (const frame of record.frames) {
      if (!frames.has(frame?.frame_ref) || matched.has(frame.frame_ref) ||
          !index(frame.pdf_matrix) || !index(frame.pdf_grid)) return null
      matched.add(frame.frame_ref)
    }
    const owner = new Map(), counts = record.strings.map(() => 0)
    for (const [index, row] of record.strings.entries()) {
      if (row?.index !== index || row.partial !== false || !Number.isSafeInteger(row.pdf_matrix) || row.pdf_matrix < 0 ||
          !Array.isArray(row.panel_refs) || !row.panel_refs.length ||
          !['group', 'bridge'].includes(row.source) ||
          (row.source === 'group' && !matched.has(row.frame_ref))) return null
      for (const ref of row.panel_refs) {
        if (!panels.has(ref) || owner.has(ref) ||
            (row.source === 'group' && frameOf.get(ref) !== row.frame_ref)) return null
        owner.set(ref, index)
      }
    }
    const ids = new Set(), covered = new Set()
    for (const opt of measurement.optimizers) {
      if (typeof opt?.id !== 'string' || !/^pdf-optimizer-[0-9]{1,10}$/.test(opt.id) || ids.has(opt.id) ||
          !(opt.vendor_label === null || typeof opt.vendor_label === 'string' && /^[0-9]{1,32}$/.test(opt.vendor_label)) ||
          !Array.isArray(opt.panel_refs) || !opt.panel_refs.length || !Array.isArray(opt.panel_handles) ||
          opt.panel_refs.length !== opt.panel_handles.length || !Number.isSafeInteger(opt.string_index) ||
          opt.string_index < 0 || opt.string_index >= counts.length ||
          opt.pdf_matrix !== record.strings[opt.string_index].pdf_matrix) return null
      ids.add(opt.id)
      for (const [index, ref] of opt.panel_refs.entries()) {
        if (!panels.has(ref) || covered.has(ref) || owner.get(ref) !== opt.string_index ||
            typeof opt.panel_handles[index] !== 'string' || !/^[0-9A-F]{1,32}$/.test(opt.panel_handles[index]) ||
            panelHandles.get(ref) !== opt.panel_handles[index]) return null
        covered.add(ref)
      }
      counts[opt.string_index]++
    }
    if (covered.size !== panels.size || owner.size !== panels.size || measurement.total_count !== ids.size ||
        counts.some((count, index) => count !== measurement.string_counts[index])) return null
    return { total_count: ids.size, string_counts: counts }
  } catch { return null }
}

export function savedReconciliationValues(graph) {
  const unavailable = (reason) => ({ value: null, reason })
  const values = RECONCILIATION_METRICS.map(() => unavailable('Read a valid saved design first.'))
  values[1] = unavailable('The saved graph has no explicit measured optimizer count.')
  if (!graph || !['frames', 'panels', 'strings', 'inverters'].every((key) => Array.isArray(graph[key]))) return values
  const available = (value) => ({ value, reason: '' })
  // A missing frame may hide compact panels, so the entire panel union is unknown.
  let framesDense = true
  for (let index = 0; index < graph.frames.length; index++) {
    if (!Object.hasOwn(graph.frames, index)) { framesDense = false; break }
  }
  const decoded = framesDense ? decodeGroundFrameSlots(graph.frames) : null
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
  const stringsValid = panelValid && strings && graph.strings.every((item) => {
    const refs = item.ordered_panel_refs
    if (!Array.isArray(refs) || !Number.isSafeInteger(item.module_count) || item.module_count <= 0 ||
        item.module_count !== refs.length || new Set(refs).size !== refs.length) return false
    // Array iteration visits holes as undefined; every() would skip missing evidence.
    for (const id of refs) {
      if (!validId(id, 'panel') || !panels.has(id)) return false
      if (assigned.has(id)) { overlap = true; return false }
      assigned.add(id)
    }
    return true
  })
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
  const optimizers = savedOptimizerMeasurement(graph)
  if (optimizers) values[1] = available(BigInt(optimizers.total_count) * SCALE)
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

export function reconciliationCsv(rows, provenance) {
  const quote = (value) => '"' + value.replace(/"/g, '""') + '"'
  const metrics = ['panels', 'optimizers', 'inverters', 'strings', 'kw_dc']
  const header = 'metric,studio,oracle,delta,verdict' +
    (provenance ? ',design_revision,engineering_totals_revision,oracle_source' : '')
  const lines = rows.map((row, index) => {
    const values = [metrics[index], row.studio, row.oracle, row.delta, row.verdict]
    if (provenance) {
      values.push(String(provenance.designRevision), String(provenance.totalsRevision ?? ''), provenance.oracleSources[index])
    }
    return values.map(quote).join(',')
  })
  return [header, ...lines, ''].join('\r\n')
}
