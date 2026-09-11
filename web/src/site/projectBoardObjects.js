// Stable source identities for the future world-space board, without a store.
const KIND_ORDER = ['drawing', 'version', 'job', 'tool', 'family']

function identity(value) {
  if (typeof value === 'string' && value.trim()) return value
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return null
}

export function deriveProjectBoardObjects({
  drawing = null,
  workspace = null,
  catalog = null,
} = {}) {
  const records = new Map()
  function add(kind, source, sourceId) {
    if (sourceId === null) return
    const key = `${kind}:${encodeURIComponent(sourceId)}`
    if (!records.has(key)) records.set(key, { key, kind, sourceId, source })
  }
  function addAll(kind, sources, getId) {
    if (!Array.isArray(sources)) return
    for (const source of sources) add(kind, source, getId(source))
  }

  add('drawing', drawing, identity(drawing?.drawing_id) ?? identity(drawing?.id))
  addAll('version', workspace?.drawing_versions, source => identity(source?.version_id))
  addAll('job', workspace?.jobs, source => identity(source?.job_id))
  addAll('tool', workspace?.built_tools, source => identity(source?.tool_id) ?? identity(source?.name))
  addAll('family', catalog?.families, source => identity(source?.family_id))

  return [...records.values()].sort((a, b) => {
    const kindOrder = KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind)
    return kindOrder || (a.sourceId < b.sourceId ? -1 : a.sourceId > b.sourceId ? 1 : 0)
  })
}
