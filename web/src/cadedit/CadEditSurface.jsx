/**
 * CadEditSurface — the first REAL slice of the browser editing surface,
 * behind cad_edit.
 *
 * What it does end to end, today, in a browser:
 *   open a .dxf from disk -> parse it inside an isolated engine worker ->
 *   list the parsed entities -> select one -> delete it or move it by a
 *   delta -> the worker re-serializes the edited document to DXF bytes,
 *   re-parses those bytes, and reports the entity count and entity list
 *   FROM THE RE-PARSE -> download the resulting .dxf.
 *
 * What it deliberately does NOT do: no server round trip, no project
 * persistence, no byte-identical round trip, and no edit at all on a
 * document carrying constructs this build can read but not rewrite (it
 * refuses BY NAME rather than silently dropping them). See
 * docs/CAD-EDIT-SURFACE-DESIGN.md.
 *
 * Isolation: the only engine contact is web/src/cad/engineWorker.js's
 * EngineBoundary, unmodified — every message in both directions is
 * schema-validated there, and a malformed one is dropped with a counted
 * receipt instead of reaching this component as an exception. The worker is
 * spawned lazily on the first open, never at mount.
 *
 * Flag: ENV_CAD_EDIT must be the FIRST operand of the `&&` at the ToolCast
 * call site so a flag-off build folds this whole module away (the `enabled`
 * default below is belt-and-braces for a direct caller, not the fence).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { EngineBoundary } from '../cad/engineWorker.js'

import { ENV_CAD_EDIT } from './flag.js'

// Mirrors dxfLineDocument.js's own cap. Checked against File.size BEFORE any
// read, so an oversized file costs a comparison and never a 4 MB+ decode.
const MAX_DOCUMENT_BYTES = 4 * 1024 * 1024

function defaultCreateWorker() {
  // The one legal spawn shape, and the only place this module names the
  // worker module path.
  return new Worker(new URL('./documentWorker.js', import.meta.url), { type: 'module' })
}

function formatPoint(point) {
  return `${point[0]}, ${point[1]}, ${point[2]}`
}

function parseDelta(raw) {
  const n = Number.parseFloat(raw)
  return Number.isFinite(n) ? n : null
}

export default function CadEditSurface({ enabled = ENV_CAD_EDIT, createWorker = defaultCreateWorker }) {
  const boundaryRef = useRef(null)
  const [documentId, setDocumentId] = useState('')
  const [entities, setEntities] = useState([])
  const [entityCount, setEntityCount] = useState(0)
  const [writable, setWritable] = useState(false)
  const [refusal, setRefusal] = useState('')
  const [selectedId, setSelectedId] = useState('')
  const [dx, setDx] = useState('10')
  const [dy, setDy] = useState('0')
  const [status, setStatus] = useState('')
  const [savedBytes, setSavedBytes] = useState(null)
  const [busy, setBusy] = useState(false)

  // Object URL for the edited bytes, revoked on replace/unmount so a long
  // editing session cannot leak one blob per edit.
  const downloadUrl = useMemo(() => {
    if (!savedBytes) return ''
    return URL.createObjectURL(new Blob([savedBytes], { type: 'application/dxf' }))
  }, [savedBytes])

  useEffect(() => () => {
    if (downloadUrl) URL.revokeObjectURL(downloadUrl)
  }, [downloadUrl])

  useEffect(() => () => {
    boundaryRef.current?.terminate()
    boundaryRef.current = null
  }, [])

  const ensureBoundary = useCallback(() => {
    if (boundaryRef.current) return boundaryRef.current
    // The resolved flag is passed EXPLICITLY rather than left to
    // EngineBoundary's env fallback: that fallback reads VITE_CAD_EDIT as
    // 'true', while this surface's build fence reads it as '1'. Passing the
    // already-resolved boolean makes the two spellings irrelevant here.
    const boundary = new EngineBoundary({ flags: { cad_edit: true }, createWorker })
    boundary.onMessage((message) => {
      if (message.type === 'ready') return
      if (message.type === 'documentLoaded') {
        setEntities(message.entities ?? [])
        setEntityCount(message.entityCount ?? 0)
        setWritable(message.writable === true)
        setRefusal(message.writable === true ? '' : (message.refusal ?? 'unknown'))
        setSelectedId('')
        setSavedBytes(null)
        setBusy(false)
        setStatus(`Loaded ${message.documentId}: ${message.entityCount} editable entities.`)
        return
      }
      if (message.type === 'editApplied') {
        setBusy(false)
        if (!message.ok) {
          setStatus(`Edit refused (${message.op}): ${message.reason ?? 'unknown reason'}`)
          return
        }
        setEntities(message.entities ?? [])
        setEntityCount(message.entityCount ?? 0)
        setSavedBytes(message.bytes ?? null)
        setSelectedId((previous) =>
          (message.entities ?? []).some((entity) => entity.id === previous) ? previous : '')
        setStatus(
          `${message.op} applied. Re-parsed from the written bytes: `
          + `${message.entityCount} entities, ${message.byteLength} bytes.`)
        return
      }
      if (message.type === 'error') {
        setBusy(false)
        setEntities([])
        setEntityCount(0)
        setWritable(false)
        setSavedBytes(null)
        setStatus(`Engine refused: ${message.message}`)
      }
    })
    boundary.start()
    boundary.post({ type: 'init' })
    boundaryRef.current = boundary
    return boundary
  }, [createWorker])

  const openFile = useCallback(async (event) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    if (file.size > MAX_DOCUMENT_BYTES) {
      setStatus(`Refused ${file.name}: ${file.size} bytes exceeds the ${MAX_DOCUMENT_BYTES}-byte limit.`)
      return
    }
    setBusy(true)
    setDocumentId(file.name)
    setStatus(`Reading ${file.name}...`)
    let bytes
    try {
      bytes = new Uint8Array(await file.arrayBuffer())
    } catch {
      setBusy(false)
      setStatus(`Could not read ${file.name}.`)
      return
    }
    const boundary = ensureBoundary()
    if (!boundary.post({ type: 'loadDocument', documentId: file.name, bytes })) {
      setBusy(false)
      setStatus(`Could not send ${file.name} to the engine.`)
    }
  }, [ensureBoundary])

  const runEdit = useCallback((op) => {
    if (!selectedId) return
    const payload = { entityId: selectedId }
    if (op === 'move') {
      const deltaX = parseDelta(dx)
      const deltaY = parseDelta(dy)
      if (deltaX === null || deltaY === null) {
        setStatus('Move refused: dx and dy must both be numbers.')
        return
      }
      payload.dx = deltaX
      payload.dy = deltaY
    }
    const boundary = boundaryRef.current
    if (!boundary) {
      setStatus('Edit refused: no document is open.')
      return
    }
    setBusy(true)
    if (!boundary.post({ type: 'applyEdit', op, payload })) {
      setBusy(false)
      setStatus(`Edit refused (${op}): the boundary rejected the message.`)
    }
  }, [dx, dy, selectedId])

  if (!enabled) return null

  const canEdit = writable && selectedId !== '' && !busy

  return (
    <section className="cad-edit-workbench" data-testid="cad-edit-workbench" aria-label="CAD editing surface">
      <h3 className="cad-edit-workbench-title">Edit a DXF drawing</h3>
      <p className="cad-edit-workbench-hint">
        Runs entirely in your browser, inside an isolated engine worker. Nothing is uploaded and
        nothing is saved to the project — download the result to keep it.
      </p>

      <input
        type="file"
        accept=".dxf"
        aria-label="DXF file"
        onChange={openFile}
        disabled={busy}
      />

      {documentId && (
        <p className="cad-edit-workbench-doc">
          {documentId} — <span data-testid="cad-edit-entity-count">{entityCount}</span> entities
        </p>
      )}

      {documentId && !writable && refusal && (
        <p className="cad-edit-workbench-refusal" role="alert">
          Read-only: {refusal}. Editing is disabled so nothing is silently dropped.
        </p>
      )}

      {entities.length > 0 && (
        <ul className="cad-edit-entity-list" data-testid="cad-edit-entity-list">
          {entities.map((entity) => (
            <li key={entity.id}>
              <label>
                <input
                  type="radio"
                  name="cad-edit-entity"
                  value={entity.id}
                  checked={selectedId === entity.id}
                  onChange={() => setSelectedId(entity.id)}
                  disabled={!writable}
                />
                {entity.type} on layer {entity.layer}: ({formatPoint(entity.start)}) to ({formatPoint(entity.end)})
              </label>
            </li>
          ))}
        </ul>
      )}

      {entities.length > 0 && (
        <div className="cad-edit-workbench-ops" role="group" aria-label="Edit operations">
          <button type="button" onClick={() => runEdit('delete')} disabled={!canEdit}>
            Delete selected
          </button>
          <label>
            dx
            <input type="text" inputMode="decimal" value={dx} onChange={(e) => setDx(e.target.value)} aria-label="dx" />
          </label>
          <label>
            dy
            <input type="text" inputMode="decimal" value={dy} onChange={(e) => setDy(e.target.value)} aria-label="dy" />
          </label>
          <button type="button" onClick={() => runEdit('move')} disabled={!canEdit}>
            Move selected
          </button>
        </div>
      )}

      {downloadUrl && (
        <a className="cad-edit-workbench-download" href={downloadUrl} download={documentId || 'edited.dxf'}>
          Download edited DXF
        </a>
      )}

      <p role="status" aria-live="polite">{status}</p>
    </section>
  )
}
