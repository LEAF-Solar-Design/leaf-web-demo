const endpoint = value => value ? `${value.node}/${value.port}` : '?'
const scalar = value => value === undefined ? 'unset' : value === null ? 'null' : typeof value === 'object' ? '[setting]' : String(value)
const blockLabel = block => `${block?.id || '?'} (${block?.registry?.id || '?'})`
const registryLabel = registry => `${registry?.id || '?'}@${registry?.version || '?'}`
const blockChanges = (before, after, id) => [
  ...settings(before?.settings, after?.settings, id),
  ...(JSON.stringify(before?.registry) !== JSON.stringify(after?.registry) ? [`Changed registry on ${id}: ${registryLabel(before?.registry)} -> ${registryLabel(after?.registry)}`] : []),
  ...(before?.title !== after?.title ? [`Changed title on ${id}`] : []),
]
function settings(before, after, id) {
  return [...new Set([...Object.keys(before || {}), ...Object.keys(after || {})])]
    .filter(key => JSON.stringify(before?.[key]) !== JSON.stringify(after?.[key]))
    .map(key => `Changed setting ${key} on ${id}: ${scalar(before?.[key])} -> ${scalar(after?.[key])}`)
}
function semanticRows(changes, beforeGraph, afterGraph) {
  return changes.flatMap(({ op, path, before, after, blockId }) => {
    const parts = path.split('/').slice(1).map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'))
    const [group, index, field, ...keys] = parts
    if ((group === 'blocks' || group === 'pipes') && parts.length === 1 && (Array.isArray(before) || Array.isArray(after))) {
      const old = before || [], next = after || []
      const identity = item => group === 'blocks' ? item.id : `${endpoint(item.from)}>${endpoint(item.to)}`
      const removed = old.filter(item => !next.some(other => identity(item) === identity(other)))
      const added = next.filter(item => !old.some(other => identity(item) === identity(other)))
      const edits = group === 'blocks' ? next.flatMap(block => {
        const previous = old.find(item => item.id === block.id)
        return previous ? blockChanges(previous, block, block.id) : []
      }) : []
      const rows = [...removed.map(item => group === 'blocks' ? `Removed block ${blockLabel(item)}` : `Removed pipe ${endpoint(item.from)} -> ${endpoint(item.to)}`),
        ...added.map(item => group === 'blocks' ? `Added block ${blockLabel(item)}` : `Added pipe ${endpoint(item.from)} -> ${endpoint(item.to)}`), ...edits]
      return rows.length ? rows : [`${op} ${path}`]
    }
    if (group === 'blocks') {
      if (field === 'registry' || field === 'title') {
        const old = beforeGraph?.blocks?.[index], next = afterGraph?.blocks?.[index]
        const id = next?.id || old?.id || blockId || index
        if (field === 'title') return [`Changed title on ${id}`]
        const oldRegistry = old?.registry || (keys.length ? { [keys[0]]: before } : before)
        const newRegistry = next?.registry || (keys.length ? { [keys[0]]: after } : after)
        return [`Changed registry on ${id}: ${registryLabel(oldRegistry)} -> ${registryLabel(newRegistry)}`]
      }
      if (parts.length === 2 && ['add', 'remove'].includes(op)) return [`${op === 'add' ? 'Added' : 'Removed'} block ${blockLabel(op === 'add' ? after : before)}`]
      if (field === 'settings') {
        const id = afterGraph?.blocks?.[index]?.id || beforeGraph?.blocks?.[index]?.id || blockId || index
        return keys.length ? [`Changed setting ${keys.join('.')} on ${id}: ${scalar(before)} -> ${scalar(after)}`] : settings(before, after, id)
      }
    }
    if (group === 'pipes' && parts.length === 2 && ['add', 'remove'].includes(op)) {
      const pipe = op === 'add' ? after : before
      return [`${op === 'add' ? 'Added' : 'Removed'} pipe ${endpoint(pipe?.from)} -> ${endpoint(pipe?.to)}`]
    }
    if (group === 'outputs' && index !== undefined) return [`Changed output ${index}`]
    if (group === 'settings') return [`Changed setting ${parts.slice(1).join('.')}: ${scalar(before)} -> ${scalar(after)}`]
    return [`${op} ${path}`]
  })
}

export default function LabDiffView({ changes = [], beforeGraph, afterGraph }) {
  return <ul aria-label="Revision changes">{semanticRows(changes, beforeGraph, afterGraph).map((row, index) => <li key={index}>{row}</li>)}</ul>
}
