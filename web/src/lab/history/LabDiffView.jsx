const endpoint = value => value ? `${value.node}/${value.port}` : '?'
const scalar = value => value === undefined ? 'unset' : value === null ? 'null' : typeof value === 'object' ? '[setting]' : String(value)
const blockLabel = block => `${block?.id || '?'} (${block?.registry?.id || '?'})`
const registryLabel = registry => `${registry?.id || '?'}@${registry?.version || '?'}`
// Limits apply across all nested-setting changes in one comparison.
const limits = { rows: 64, visits: 256, children: 32, depth: 6, text: 160 }
const childPath = (path, key, array = false) => array ? `${path}[${key}]`
  : /^[A-Za-z_$][\w$]*$/.test(key) ? `${path ? `${path}.` : ''}${key}` : `${path}[${JSON.stringify(key)}]`
function settingRows(before, after, path, id, budget) {
  const rows = []
  const clip = value => {
    const text = String(value)
    if (text.length <= limits.text) return text
    budget.truncated = true
    return `${text.slice(0, limits.text)} [truncated]`
  }
  const valueText = value => value === undefined ? 'unset' : value === null ? 'null'
    : typeof value === 'object' ? Array.isArray(value) ? '[]' : '{}'
      : typeof value === 'string' ? ['null', 'unset'].includes(value) ? JSON.stringify(value) : clip(value) : clip(value)
  const emit = (left, right, name) => {
    if (budget.rows >= limits.rows) { budget.truncated = true; return }
    budget.rows += 1
    rows.push(`Changed setting ${clip(name)} on ${clip(id)}: ${valueText(left)} -> ${valueText(right)}`)
  }
  const object = value => value !== null && typeof value === 'object'
  function visit(left, right, name, depth) {
    if (Object.is(left, right)) return
    if (++budget.visits > limits.visits || depth > limits.depth || budget.rows >= limits.rows) {
      budget.truncated = true
      return
    }
    const leftObject = object(left), rightObject = object(right)
    if (!leftObject && !rightObject) { emit(left, right, name); return }
    // A type change also needs a row when one side has no scalar leaves.
    const typeChanged = (left !== undefined && !leftObject) || (right !== undefined && !rightObject)
      || (leftObject && rightObject && Array.isArray(left) !== Array.isArray(right))
    if (typeChanged) emit(left, right, name)
    const keys = [...new Set([...Object.keys(leftObject ? left : {}), ...Object.keys(rightObject ? right : {})])].sort()
    if (!keys.length) {
      if (!typeChanged && (!leftObject || !rightObject)) emit(left, right, name)
      return
    }
    if (keys.length > limits.children) budget.truncated = true
    for (const key of keys.slice(0, limits.children)) {
      if (budget.rows >= limits.rows || budget.visits >= limits.visits) { budget.truncated = true; break }
      visit(leftObject && Object.hasOwn(left, key) ? left[key] : undefined,
        rightObject && Object.hasOwn(right, key) ? right[key] : undefined,
        childPath(name, key, Array.isArray(left) || Array.isArray(right)), depth + 1)
    }
  }
  visit(before, after, path, 0)
  return rows
}
const blockChanges = (before, after, id, budget) => [
  ...settings(before?.settings, after?.settings, id, budget),
  ...(JSON.stringify(before?.registry) !== JSON.stringify(after?.registry) ? [`Changed registry on ${id}: ${registryLabel(before?.registry)} -> ${registryLabel(after?.registry)}`] : []),
  ...(before?.title !== after?.title ? [`Changed title on ${id}`] : []),
]
function settings(before, after, id, budget) {
  return settingRows(before, after, '', id, budget)
}
function semanticRows(changes, beforeGraph, afterGraph, budget) {
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
        return previous ? blockChanges(previous, block, block.id, budget) : []
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
        return keys.length ? settingRows(before, after, keys.reduce((path, key) => childPath(path, key), ''), id, budget)
          : settings(before, after, id, budget)
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
  const budget = { rows: 0, visits: 0, truncated: false }
  const rows = semanticRows(changes, beforeGraph, afterGraph, budget)
  return <ul aria-label="Revision changes">{rows.map((row, index) => <li key={index}>{row}</li>)}
    {budget.truncated && <li>Setting changes truncated; some paths or values are not shown.</li>}
  </ul>
}
