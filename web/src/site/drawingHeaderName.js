// The server intake is the fallback. A committed hand import owns the visible
// drawing name only while the viewer holds that same engine document.
export function drawingHeaderName(shown, activeIntake, engineDocument) {
  const imported = engineDocument?.documentOrigin === 'import'
    && activeIntake?.documentId === engineDocument.documentId
    ? engineDocument.documentId : null
  const name = imported || shown?.dwg
  return name ? name.split(/[\\/]/).pop().replace(/\.(dwg|dxf)$/i, '') : null
}
