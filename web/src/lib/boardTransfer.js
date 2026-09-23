export const BOARD_TRANSFER_TYPE = 'application/x-leaf-board-ref'
export const MAX_TRANSFER_CHARS = 1024

const validId = (id) => typeof id === 'string' && id.length > 0 && id.length <= 200
const validSeq = (seq) => Number.isSafeInteger(seq) && seq > 0
const keys = ['kind', 'projectId', 'drawingId', 'versionId', 'seq']
const refuse = (reason, message) => Object.freeze({ ok: false, reason, message })

export function encodeVersionRef({ projectId, drawingId, versionId, seq }) {
  if (![projectId, drawingId, versionId].every(validId) || !validSeq(seq)) return null
  return JSON.stringify({ kind: 'version', projectId, drawingId, versionId, seq })
}

export function resolveVersionTransfer(raw, { projectId, drawingId, versions }) {
  const unreadable = () => refuse('unreadable', 'Drag a version from the Versions card again.')
  if (typeof raw !== 'string' || raw.length > MAX_TRANSFER_CHARS) return unreadable()
  let ref
  try {
    ref = JSON.parse(raw)
  } catch {
    return unreadable()
  }
  if (!ref || Object.getPrototypeOf(ref) !== Object.prototype
    || Object.keys(ref).length !== keys.length
    || !keys.every((key) => Object.prototype.hasOwnProperty.call(ref, key))) return unreadable()
  const name = validSeq(ref.seq) ? `v${ref.seq}` : 'this version'
  if (ref.kind !== 'version') return refuse('unsupported-kind', `Choose ${name} from the Versions card to preview it in the drawing.`)
  if (!drawingId) return refuse('no-drawing', `Open a drawing before previewing ${name}.`)
  if (ref.projectId !== projectId) return refuse('wrong-project', `Choose ${name} from the current project to preview it.`)
  if (ref.drawingId !== drawingId) return refuse('wrong-drawing', `Open the drawing that contains ${name} to preview it.`)
  if (!Array.isArray(versions) || !versions.some((version) => version.version_id === ref.versionId
    && version.seq === ref.seq && version.drawing_id === ref.drawingId)) {
    return refuse('stale-version', `Refresh the Versions card and choose ${name} again.`)
  }
  return Object.freeze({ ok: true, seq: ref.seq, versionId: ref.versionId })
}
