const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const text = value => typeof value === 'string' && value.length > 0 && value.length <= 128
const count = value => Number.isSafeInteger(value) && value >= 0
const decimal = value => typeof value === 'string' && /^[0-9]{1,30}(?![\s\S])/.test(value)
export const nsToMs = value => decimal(value) ? Number(value) / 1e6 : null
const nanoseconds = value => value === null || decimal(value) || count(value)
const timestamp = value => count(value) ? value : text(value) && Number.isFinite(Date.parse(value)) ? value : null
const stages = ['book_seen', 'decision', 'money_check', 'send', 'exchange_accepted', 'fill', 'result']
const receiptTime = receipt => {
  const value = receipt.requested_at ?? receipt.finished_at
  return typeof value === 'number' ? value : value === null ? 0 : Date.parse(value)
}

function segmentsOf(value) {
  if (!Array.isArray(value) || value.length !== 6) return null
  const result = []
  for (const [index, segment] of value.entries()) {
    if (!object(segment) || segment.from !== stages[index] || segment.to !== stages[index + 1]
      || !['measured', 'not measured'].includes(segment.measurement)
      || !(segment.measurement === 'measured' ? decimal(segment.duration_ns) : segment.duration_ns === null)) return null
    result.push({ from: segment.from, to: segment.to, measurement: segment.measurement, duration_ns: segment.duration_ns })
  }
  return result
}

function receiptOf(value) {
  if (!object(value) || !text(value.receipt_id) || !text(value.status)) return null
  const result = { receipt_id: value.receipt_id, status: value.status, segments: segmentsOf(value.segments) }
  for (const key of ['control_id', 'state', 'error_code', 'result_status', 'run_id']) {
    result[key] = text(value[key]) ? value[key] : null
  }
  for (const key of ['requested_at', 'applied_at', 'finished_at']) result[key] = timestamp(value[key])
  result.mode = ['live', 'candidate', 'Live', 'Candidate'].includes(value.mode) ? value.mode.toLowerCase() : null
  result.source_sha = typeof value.source_sha === 'string' && /^(?:[a-f0-9]{7,40}|[a-f0-9]{64})(?![\s\S])/.test(value.source_sha) ? value.source_sha : null
  result.unreadable = value.unreadable === true
  result.fill = object(value.fill) && ['fill_count', 'price_cents', 'fee_cents', 'cost_cents'].every(key => count(value.fill[key]))
    ? Object.fromEntries(['fill_count', 'price_cents', 'fee_cents', 'cost_cents'].map(key => [key, value.fill[key]])) : null
  return result
}

function histogramOf(value) {
  if (!object(value) || !text(value.metric) || !text(value.receipt_id)
    || !['p50_ns', 'p99_ns', 'budget_ns'].every(key => nanoseconds(value[key]))
    || !count(value.count) || !count(value.observed_count) || typeof value.over_budget !== 'boolean') return null
  return Object.fromEntries(['metric', 'receipt_id', 'p50_ns', 'p99_ns', 'budget_ns', 'count', 'observed_count', 'over_budget'].map(key => [key, value[key]]))
}

// Host rows are snapshots; only the newest snapshot is authoritative.
export function readTestTradeEvidence(provider) {
  const empty = { receipts: [], histograms: [], stale: false, truncated: false }
  try {
    const rows = provider?.rowsOf?.('evidence')
    if (!Array.isArray(rows) || !rows.length) return empty
    const row = rows.at(-1)
    const flags = { stale: row?.stale === true, truncated: row?.truncated === true }
    const data = row?.data
    if (!object(data)) return { ...empty, ...flags }
    // Retain only twenty summaries while considering every receipt in the snapshot.
    const receipts = []
    if (Array.isArray(data.receipts)) for (const value of data.receipts) {
      const receipt = receiptOf(value)
      if (!receipt) continue
      const index = receipts.findIndex(item => receiptTime(receipt) > receiptTime(item))
      if (index !== -1) receipts.splice(index, 0, receipt)
      else if (receipts.length < 20) receipts.push(receipt)
      if (receipts.length > 20) receipts.pop()
    }
    const histograms = Array.isArray(data.histograms) ? data.histograms.slice(0, 96).map(histogramOf).filter(Boolean) : []
    return { receipts, histograms, stale: flags.stale,
      truncated: flags.truncated || data.truncated_receipts === true || (count(data.truncated_receipts) && data.truncated_receipts > 0)
        || (Array.isArray(data.receipts) && data.receipts.length > 20)
        || (Array.isArray(data.histograms) && data.histograms.length > 96) }
  } catch { return empty }
}

export function stepStats(receipts) {
  const valid = Array.isArray(receipts) ? receipts.slice(0, 20).map(receipt => segmentsOf(receipt?.segments)) : []
  return Array.from({ length: 6 }, (_, index) => {
    const values = []
    let notMeasuredCount = 0
    for (const segments of valid) {
      if (!segments) continue
      const segment = segments[index]
      if (segment.measurement === 'measured' && segment.duration_ns !== null) values.push(nsToMs(segment.duration_ns))
      else notMeasuredCount += 1
    }
    values.sort((a, b) => a - b)
    return { from: stages[index], to: stages[index + 1],
      measuredCount: values.length, notMeasuredCount,
      p50_ms: values.length ? values[Math.ceil(values.length * 0.5) - 1] : null,
      p99_ms: values.length ? values[Math.ceil(values.length * 0.99) - 1] : null }
  })
}
