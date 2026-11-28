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

const compareRank = (a, b) => (a < b ? -1 : a > b ? 1 : 0)

// R2-14: the version a project opens on when nothing selected one. Exactly one
// live version wins outright; otherwise the newest (created_at, then seq) wins,
// and a tie for newest selects nothing rather than guess. Pure and O(n).
export function defaultDrawingVersionId({ workspace, openProjectId }) {
  const projectId = workspace?.project?.project_id ?? workspace?.project?.id
  if (projectId == null || projectId !== openProjectId) return null

  let selected = null
  let best = null
  let tied = false
  for (const row of workspace?.drawing_versions ?? []) {
    if (row == null || row.deleted_at != null || typeof row.version_id !== 'string' || !row.version_id) continue
    const created = Date.parse(row.created_at)
    const seq = Number(row.seq)
    const rank = [Number.isFinite(created) ? created : -Infinity, Number.isFinite(seq) ? seq : -Infinity]
    const order = best == null ? 1 : compareRank(rank[0], best[0]) || compareRank(rank[1], best[1])
    if (order > 0) {
      selected = row.version_id
      best = rank
      tied = false
    } else if (order === 0) tied = true
  }
  return tied ? null : selected
}
