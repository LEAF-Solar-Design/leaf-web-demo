const SPAN_ORDER = [
  'submit', 'queue', 'task_start', 'image_pull', 'drawing_fetch', 'engine',
  'output_upload', 'version_write', 'publish', 'client_delivery',
]

const LABELS = {
  submit: 'submit',
  queue: 'provider queue',
  task_start: 'task start',
  image_pull: 'image pull',
  drawing_fetch: 'drawing fetch',
  engine: 'CAD engine',
  output_upload: 'output upload',
  version_write: 'version write',
  publish: 'publish',
  client_delivery: 'client delivery',
}

function duration(value) {
  return Number.isFinite(Number(value)) ? `${Number(value).toLocaleString()} ms` : 'unavailable'
}

export function cadTimingRows(envelope) {
  const timing = envelope?.execution_provenance?.cad_timing
  if (timing?.contract !== 'leaf.cad-timing.v1' || !timing.spans_ms) return []
  const rows = [`CAD total ${duration(timing.total_ms)}`]
  if (Number.isFinite(Number(timing.provider_accounted_ms))) {
    rows.push(`provider accounted ${duration(timing.provider_accounted_ms)}`)
  }
  for (const name of SPAN_ORDER) {
    rows.push(`${LABELS[name]} ${duration(timing.spans_ms[name])}`)
  }
  return rows
}
