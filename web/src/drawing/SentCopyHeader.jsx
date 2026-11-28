import { useEffect, useRef, useState } from 'react'
import { getDrawingUploadStatus } from '../api.js'
import { useDrawingIdentity } from './DrawingIdentityProvider.jsx'
import { defaultDrawingVersionId, queryDrawingVersionId, sentCopyHeaderLine } from './sentCopyHeader.js'

export function useLandOnQueryDrawing({ mock, openProjectId, workspace, canonicalVersionId, selectCanonicalVersion }) {
  const { origin, source } = useDrawingIdentity()
  const landed = useRef(new Set())
  useEffect(() => {
    if (origin !== 'query' || mock || canonicalVersionId != null) return
    const key = JSON.stringify([openProjectId, source])
    if (landed.current.has(key)) return
    const versionId = queryDrawingVersionId({ workspace, openProjectId, drawingId: source })
    if (versionId == null) return
    landed.current.add(key)
    selectCanonicalVersion(versionId)
  }, [origin, source, mock, openProjectId, workspace, canonicalVersionId, selectCanonicalVersion])
}

// R2-14: a project that opens with no version selected lands on its default
// version once per open, so the drawing loads without a click. It never
// replaces a selection (the user's, a revision binding's or the ?drawing=
// landing's), defers while the ?drawing= query resolves to a version here, and
// stays quiet after the user clears a selection in the same open.
export function useLandOnDefaultDrawingVersion({ mock, openProjectId, workspace, canonicalVersionId, selectCanonicalVersion }) {
  const { origin, source } = useDrawingIdentity()
  const settled = useRef({ projectId: null, done: false })
  useEffect(() => {
    if (settled.current.projectId !== openProjectId) settled.current = { projectId: openProjectId, done: false }
    if (mock || openProjectId == null || settled.current.done) return
    if (canonicalVersionId != null) { settled.current.done = true; return }
    if (origin === 'query' && queryDrawingVersionId({ workspace, openProjectId, drawingId: source }) != null) return
    const versionId = defaultDrawingVersionId({ workspace, openProjectId })
    if (versionId == null) return
    settled.current.done = true
    selectCanonicalVersion(versionId)
  }, [origin, source, mock, openProjectId, workspace, canonicalVersionId, selectCanonicalVersion])
}

export default function SentCopyLine({ mock, shownDrawingId, workspace, load = getDrawingUploadStatus }) {
  const { origin, source } = useDrawingIdentity()
  const requests = useRef(new Map())
  const [result, setResult] = useState(null)
  const enabled = origin === 'query' && !mock && typeof source === 'string'
    && typeof shownDrawingId === 'string' && shownDrawingId.toLowerCase() === source.toLowerCase()

  useEffect(() => {
    if (!enabled) return
    let active = true
    if (!requests.current.has(source)) {
      requests.current.set(source, Promise.resolve().then(() => load(source)))
    }
    requests.current.get(source).then(
      (status) => { if (active) setResult({ source, status }) },
      () => { if (active) setResult({ source, status: null }) },
    )
    return () => { active = false }
  }, [enabled, source, load])

  const line = enabled && result?.source === source
    ? sentCopyHeaderLine({ status: result.status, workspace, drawingId: source }) : null
  return line ? <div className="sent-copy-line" data-testid="sent-copy-line" title={line}>{line}</div> : null
}
