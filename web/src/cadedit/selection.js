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
  if (typeof id !== 'string' || !(entities || []).some((entity) => entity.id === id) || ids.includes(id)) return ids
  return withSelection([...ids, id]).selectedIds
}

export function toggleId(ids, id, entities) {
  if (typeof id !== 'string' || !(entities || []).some((entity) => entity.id === id)) return ids
  return withSelection(ids.includes(id) ? ids.filter((candidate) => candidate !== id) : [...ids, id]).selectedIds
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
