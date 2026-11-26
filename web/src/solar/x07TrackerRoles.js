export const trackerFields = ['block', 'layer', 'tracker_type', 'modules_per_instance']

export function validTrackers(rows) {
  if (!Array.isArray(rows)) return false
  const pairs = new Set(), counts = new Map()
  for (const row of rows) {
    if (!row || Object.keys(row).length !== trackerFields.length || !trackerFields.every(key => Object.hasOwn(row, key))) return false
    if (![row.block, row.layer].every(value => typeof value === 'string' && value.length && value.length <= 512)
      || typeof row.tracker_type !== 'string' || !row.tracker_type.length || row.tracker_type.length > 64 || /[^A-Za-z0-9_-]/.test(row.tracker_type)
      || !Number.isSafeInteger(row.modules_per_instance) || row.modules_per_instance < 1 || row.modules_per_instance > 1000000) return false
    const key = JSON.stringify([row.block, row.layer])
    if (pairs.has(key) || (counts.has(row.tracker_type) && counts.get(row.tracker_type) !== row.modules_per_instance)) return false
    pairs.add(key)
    counts.set(row.tracker_type, row.modules_per_instance)
  }
  return true
}

export function trackerValues(rows) {
  return rows?.map(row => trackerFields.map(key => row[key]))
}
