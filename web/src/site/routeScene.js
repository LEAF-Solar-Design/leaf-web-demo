export function sceneForPath(path) {
  if (
    path === '/ty'
    || path.startsWith('/ty/')
    || path === '/app'
    || path.startsWith('/app/')
  ) return 'app'
  if (path === '/sheets' || path.startsWith('/sheets/')) return 'sheets'
  if (path === '/try') return 'tool'
  return 'site'
}
