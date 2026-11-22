// The server adopts only an ACCOUNT UPLOAD into a project: a canonical UUID or
// the legacy `u-<10 lowercase hex>` receipt (platform/store.py
// is_account_upload_source_id). The built-in sample drawing has neither, so the
// import refuses it with a validation error. This mirror lets the shell say so
// before the click instead of after a failed request; the server stays the
// authority.
const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const LEGACY_ACCOUNT_UPLOAD = /^u-[0-9a-f]{10}$/
export function isAccountUploadDrawingId(drawingId) {
  return typeof drawingId === 'string' && (CANONICAL_UUID.test(drawingId) || LEGACY_ACCOUNT_UPLOAD.test(drawingId))
}

export function mountedDrawingSource({ drawingState, requestedDrawingId, name } = {}) {
  const drawingId = drawingState?.drawing_id
  const version = drawingState?.head
  if (typeof drawingId !== 'string' || !drawingId.trim() || drawingId !== requestedDrawingId
    || !Number.isInteger(version) || version <= 0) return null
  return Object.freeze({ drawingId, version, name: typeof name === 'string' ? name.trim() || null : null })
}

export function drawingAttachKey(projectId, source) {
  return `b3:${projectId}:${source.drawingId}:${source.version}`
}

export async function attachDrawingToProject({ projectId, source, importUpload }) {
  // The server owns tenant and org checks for this account drawing.
  const { drawingId, version, name } = source
  return importUpload(projectId, { drawingId, version, name }, { idempotencyKey: drawingAttachKey(projectId, source) })
}

export async function createProjectFromDrawing({ name, source, createProject, importUpload }) {
  // One create; retries only attach the captured source with the same key.
  const project = await createProject(name)
  if (!project) return null
  const projectId = project.project_id ?? project.id
  if (!projectId) throw new Error('The workspace service returned a project without an id.')
  if (source === null) return { project, attached: false, reason: 'unattachable' }
  const retry = () => attachDrawingToProject({ projectId, source, importUpload })
  try {
    const drawingVersion = await retry()
    return { project, attached: true, drawingVersion }
  } catch (error) {
    return { project, attached: false, reason: 'attach-failed', error, retry }
  }
}
