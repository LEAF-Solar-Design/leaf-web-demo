export const HEADER_POSITIONS = Object.freeze(['above-tools', 'below-tools'])
export const DEFAULT_HEADER_POSITION = 'above-tools'
export const layoutProfile = profile => profile === 'cad' || profile === 'solar'
export const headerPosition = value => HEADER_POSITIONS.includes(value) ? value : DEFAULT_HEADER_POSITION
const object = value => !!value && typeof value === 'object' && !Array.isArray(value)

export function requestedHeaderPosition(text) {
  const request = String(text || '').trim().toLowerCase()
  if (!request || request.length > 300 || !/\b(header|workspace controls|profile controls)\b/.test(request)
    || !/\b(tools|toolbar|ribbon)\b/.test(request)) return null
  const desired = request.match(/\bshould\s+be\b([\s\S]*)$/)?.[1] ?? request
  const above = /\b(above|before)\b/.test(desired)
  const below = /\b(below|after)\b/.test(desired)
  return above === below ? null : above ? 'above-tools' : 'below-tools'
}

export function headerField(overlay, profile) {
  const toolbar = overlay?.[profile]?.toolbar
  return object(toolbar) && Object.hasOwn(toolbar, 'workspaceHeader')
    ? { present: true, value: toolbar.workspaceHeader } : { present: false }
}

export function patchHeader(overlay, profile, field) {
  if (!object(overlay) || !layoutProfile(profile) || (field.present && !HEADER_POSITIONS.includes(field.value))) {
    throw new Error('The workspace header setting is not valid.')
  }
  const surface = Object.hasOwn(overlay, profile) ? overlay[profile] : {}
  if (!object(surface) || (Object.hasOwn(surface, 'toolbar') && !object(surface.toolbar))) {
    throw new Error('The current workspace settings cannot be changed safely.')
  }
  const toolbar = { ...surface.toolbar }
  if (field.present) toolbar.workspaceHeader = field.value
  else delete toolbar.workspaceHeader
  const nextSurface = { ...surface }
  if (Object.keys(toolbar).length) nextSurface.toolbar = toolbar
  else delete nextSurface.toolbar
  const next = { ...overlay }
  if (Object.keys(nextSurface).length) next[profile] = nextSurface
  else delete next[profile]
  return next
}

export function sameHeaderField(a, b) {
  return a.present === b.present && (!a.present || a.value === b.value)
}

export function confirmedLayout(receipt, readback, submitted) {
  const ordered = value => Array.isArray(value) ? value.map(ordered) : object(value)
    ? Object.fromEntries(Object.keys(value).sort().map(key => [key, ordered(value[key])])) : value
  return typeof receipt?.sha256 === 'string' && /^[a-f0-9]{64}$/.test(receipt.sha256)
    && typeof receipt.authored_at === 'string' && receipt.authored_at.length > 0
    && readback?.source?.sha256 === receipt.sha256
    && JSON.stringify(ordered(readback.surfaces)) === JSON.stringify(ordered(submitted))
}
