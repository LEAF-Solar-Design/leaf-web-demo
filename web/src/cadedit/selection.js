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
