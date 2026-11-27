import { FINGERPRINT_SCHEMA, fingerprintLine } from '../leafPlatform/sendCopy.js'

export function sentCopyHeaderLine({ status, workspace, drawingId }) {
  const fingerprint = status?.fingerprint
  if (status?.status !== 'ready' || status.tenant_kind !== 'account'
    || !fingerprint || typeof fingerprint !== 'object' || Array.isArray(fingerprint)
    || fingerprint.schema !== FINGERPRINT_SCHEMA || fingerprint.status !== 'ok'
    || typeof status.content_sha256 !== 'string' || status.content_sha256 !== fingerprint.sha256) return null

  const artifact = workspace?.drawing_artifacts?.find((row) =>
    typeof row.drawing_id === 'string' && typeof drawingId === 'string'
    && row.drawing_id.toLowerCase() === drawingId.toLowerCase())
  const name = typeof artifact?.name === 'string' && artifact.name.trim() ? artifact.name : status.filename
  if (typeof name !== 'string' || !name.trim()) return null
  return fingerprintLine(fingerprint, name)
}

export function queryDrawingVersionId({ workspace, openProjectId, drawingId }) {
  const projectId = workspace?.project?.project_id ?? workspace?.project?.id
  if (projectId == null || projectId !== openProjectId || typeof drawingId !== 'string' || !drawingId.trim()) return null

  let selected = null
  let highestSeq = -Infinity
  for (const row of workspace?.drawing_versions ?? []) {
    if (row.deleted_at != null || typeof row.drawing_id !== 'string'
      || row.drawing_id.toLowerCase() !== drawingId.toLowerCase() || typeof row.version_id !== 'string') continue
    const seq = Number(row.seq)
    if (Number.isFinite(seq) && seq > highestSeq) {
      selected = row.version_id
      highestSeq = seq
    }
  }
  return selected
}
