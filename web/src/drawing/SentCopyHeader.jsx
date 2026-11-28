import { useEffect, useRef, useState } from 'react'
import { getDrawingUploadStatus } from '../api.js'
import { useDrawingIdentity } from './DrawingIdentityProvider.jsx'
import { queryDrawingVersionId, sentCopyHeaderLine } from './sentCopyHeader.js'

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
