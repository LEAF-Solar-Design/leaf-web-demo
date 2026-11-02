// Ephemeral identities are carried by immutable clones and never serialized.
const identities = new WeakMap()
let serial = 0
export function selectionIdentity(node) {
  if (!identities.has(node)) identities.set(node, String(++serial))
  return identities.get(node)
}
export function cloneDocument(value, identitySource = value) {
  if (!value || typeof value !== 'object') return value
  const copy = Array.isArray(value) ? value.map((item, index) => cloneDocument(item, identitySource?.[index] ?? item)) : Object.fromEntries(Object.entries(value).map(([key, item]) => [key, cloneDocument(item, identitySource?.[key] ?? item)]))
  identities.set(copy, selectionIdentity(identitySource && typeof identitySource === 'object' ? identitySource : value))
  return copy
}
export function findNode(document, identity) {
  if (!document || typeof document !== 'object') return null
  if (selectionIdentity(document) === identity) return document
  for (const value of Object.values(document)) {
    if (value && typeof value === 'object') {
      const found = findNode(value, identity)
      if (found) return found
    }
  }
  return null
}
export function setOverride(document, identity, path, value) {
  const next = cloneDocument(document), node = findNode(next, identity)
  if (!node) return document
  let target = node
  const parents = []
  for (const key of path.slice(0, -1)) {
    if (!Object.hasOwn(target, key)) target[key] = {}
    parents.push([target, key]); target = target[key]
  }
  const key = path.at(-1)
  if (value === undefined) delete target[key]
  else target[key] = value
  for (const [parent, field] of parents.reverse()) if (!Object.keys(parent[field]).length) delete parent[field]
  return next
}
export function resetDesign(document) {
  const next = cloneDocument(document)
  for (const key of ['appearance', 'textStyles', 'labels', 'nav']) delete next[key]
  for (const category of next.categories || []) {
    for (const key of ['layout', 'hidden', 'navLabel', 'textStyles']) delete category[key]
    for (const image of category.images) { delete image.display; delete image.captionStyle }
  }
  for (const section of next.resume || []) {
    delete section.layout; delete section.headingStyle
    for (const entry of section.entries) { delete entry.layout; delete entry.textStyles }
  }
  return next
}

export function singleFieldChange(before, after) {
  const changes = []
  const visit = (left, right) => {
    if (!left || !right || typeof left !== 'object' || typeof right !== 'object'
      || Array.isArray(left) !== Array.isArray(right) || Object.keys(left).join() !== Object.keys(right).join()) return false
    for (const key of Object.keys(left)) {
      if (left[key] === right[key]) continue
      if (left[key] && right[key] && typeof left[key] === 'object' && typeof right[key] === 'object') {
        if (!visit(left[key], right[key])) return false
      } else changes.push(`${selectionIdentity(left)}:${key}`)
      if (changes.length > 1) return false
    }
    return true
  }
  return visit(before, after) && changes.length === 1 ? changes[0] : null
}
