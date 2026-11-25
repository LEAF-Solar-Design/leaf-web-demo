export function plotLayerNames(intake) {
  const blocked = new Map()
  for (const layers of [intake?.hidden_layers, intake?.nonplot_layers]) {
    if (Array.isArray(layers)) {
      for (const layer of layers) {
        if (typeof layer === 'string') blocked.set(layer.toUpperCase(), layer)
      }
    }
  }
  const names = new Set()
  if (blocked.size) {
    for (const rows of [intake.inserts, intake.texts, intake.polylines]) {
      if (!Array.isArray(rows)) continue
      for (const row of rows) {
        const name = typeof row?.layer === 'string' && blocked.get(row.layer.toUpperCase())
        if (name) names.add(name)
      }
    }
  }
  return [...names]
}
