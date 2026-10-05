export const NO_IDS = Object.freeze([])

export function withSelection(ids) {
  const unique = [...new Set((ids || []).filter((id) => typeof id === 'string'))]
  const selectedIds = unique.length ? Object.freeze(unique) : NO_IDS
  return { selectedIds, selectedId: selectedIds.length === 1 ? selectedIds[0] : '' }
}

export function surviveSelectionIds(ids, entities) {
  return (ids || []).filter((id) => typeof id === 'string' && (entities || []).some((entity) => entity.id === id))
}

export function replaceIds(nextIds, entities) {
  return withSelection(surviveSelectionIds(nextIds, entities)).selectedIds
}

export function addId(ids, id, entities) {
  const survivors = surviveSelectionIds(ids, entities)
  const nextIds = typeof id === 'string' && (entities || []).some((entity) => entity.id === id) && !survivors.includes(id)
    ? [...survivors, id] : survivors
  if (nextIds.length === ids.length && nextIds.every((candidate, index) => candidate === ids[index])) return ids
  return withSelection(nextIds).selectedIds
}

export function toggleId(ids, id, entities) {
  const survivors = surviveSelectionIds(ids, entities)
  const nextIds = typeof id === 'string' && (entities || []).some((entity) => entity.id === id)
    ? (survivors.includes(id) ? survivors.filter((candidate) => candidate !== id) : [...survivors, id]) : survivors
  if (nextIds.length === ids.length && nextIds.every((candidate, index) => candidate === ids[index])) return ids
  return withSelection(nextIds).selectedIds
}

/**
 * Shift-click range: every selectable id from the anchor to the target,
 * inclusive, in the given list's order (never click order). One O(n) pass,
 * no allocation beyond the result. An absent anchor collapses the range to
 * the target; an absent target, or a list that is not an array, yields
 * NO_IDS. Read-only rows (editable === false) are skipped. Ids keep their
 * original type and are matched by String() identity, so BigInt handles work.
 */
export function rangeIds(entities, anchorId, targetId) {
  if (!Array.isArray(entities)) return NO_IDS
  const anchor = anchorId == null ? null : String(anchorId), target = String(targetId)
  let start = -1, end = -1
  for (let i = 0; i < entities.length; i += 1) {
    const identity = String(entities[i]?.id)
    if (identity === target) end = i
    if (identity === anchor) start = i
  }
  if (end < 0) return NO_IDS
  if (start < 0) start = end
  const from = Math.min(start, end), to = Math.max(start, end), ids = []
  for (let i = from; i <= to; i += 1) if (entities[i] && entities[i].editable !== false) ids.push(entities[i].id)
  return ids.length ? Object.freeze(ids) : NO_IDS
}

/** Touch press-and-hold that enters selection, and how far a finger may drift before it is a scroll. */
export const LONG_PRESS_MS = 500
export const LONG_PRESS_SLOP_PX = 10

const NON_TEXT_INPUTS = new Set(['radio', 'checkbox', 'button', 'submit', 'reset', 'image', 'range', 'color', 'file'])

/** A target that takes typed characters: a text-like input, textarea, select or editable content. */
export function isTypingTarget(node) {
  if (!node || typeof node !== 'object') return false
  if (node.isContentEditable) return true
  const tag = String(node.tagName || '').toLowerCase()
  if (tag === 'textarea' || tag === 'select') return true
  return tag === 'input' && !NON_TEXT_INPUTS.has(String(node.type || 'text').toLowerCase())
}

/** X toggles the focused row: a bare x or X, never repeated, composed, chorded or typed into a field. */
export function isToggleKey(event) {
  if (!event || (event.key !== 'x' && event.key !== 'X')) return false
  if (event.ctrlKey || event.metaKey || event.altKey || event.repeat || event.isComposing) return false
  return !isTypingTarget(event.target)
}

/** What a row click means: Shift is a range (Ctrl or Cmd with it adds the range), Ctrl or Cmd alone toggles, else null. */
export function clickIntent(event) {
  const add = !!(event?.ctrlKey || event?.metaKey)
  if (event?.shiftKey) return add ? 'rangeAdd' : 'range'
  return add ? 'toggle' : null
}

export const EDIT_OP_LABELS = Object.freeze({
  delete: 'Delete', move: 'Move', moveVertex: 'Move vertex', addVertex: 'Add vertex',
  deleteVertex: 'Delete vertex', setLayer: 'Set layer', copy: 'Copy', mirror: 'Mirror',
  rotate: 'Rotate', scale: 'Scale', explode: 'Explode', offset: 'Offset', arrayRect: 'Array',
  arrayPolar: 'Polar array', trim: 'Trim', extend: 'Extend', fillet: 'Fillet', chamfer: 'Chamfer',
  matchprop: 'Match', setColor: 'Color', setLinetype: 'Linetype', setLineweight: 'Lineweight',
  group: 'Group', ungroup: 'Ungroup', createBlock: 'Create Block', cutClip: 'Cut', copyClip: 'Copy',
})

export function multiSelectionRefusal(op, count) {
  return `${EDIT_OP_LABELS[op] || 'This change'} needs one object; ${count} are selected.`
}
