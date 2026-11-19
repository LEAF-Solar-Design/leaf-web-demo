// The server intake is the fallback. A committed hand import owns the visible
// drawing name only while the viewer holds that same engine document.
export function drawingHeaderSource(shown, activeIntake, engineDocument) {
  const imported = engineDocument?.documentOrigin === 'import'
    && activeIntake?.documentId === engineDocument.documentId
    ? engineDocument.documentId : null
  const name = imported || shown?.dwg
  return name ? name.split(/[\\/]/).pop() || null : null
}

export function drawingHeaderName(shown, activeIntake, engineDocument) {
  return drawingHeaderSource(shown, activeIntake, engineDocument)?.replace(/\.(dwg|dxf)$/i, '') ?? null
}

export function editedDrawingFileName(name) {
  const stem = String(name ?? '').split(/[\\/]/).pop().replace(/\.(dwg|dxf)$/i, '').trim()
  if (stem.length > 120 || !/^[\p{L}\p{N} ._()-]+$/u.test(stem)) return 'edited.dxf'
  return stem + '.dxf'
}
