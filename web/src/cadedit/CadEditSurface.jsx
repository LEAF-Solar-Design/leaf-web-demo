/**
 * CadEditSurface — the browser editing surface, behind cad_edit (card F-3).
 *
 * What it does end to end, in a browser, against the REAL compiled CAD
 * engine (the rev-pinned MPL-2.0 wasm behind the license fence's worker
 * boundary):
 *   open a .dxf from disk -> parse the WHOLE document inside the isolated
 *   engine worker -> list every entity truthfully (editable kinds — LINE,
 *   LWPOLYLINE, classic POLYLINE — plus an honest OTHER bucket that
 *   round-trips untouched) -> select one -> delete it, translate it, move /
 *   add / delete a single vertex, or reassign its layer -> the worker
 *   re-serializes, RE-PARSES the written bytes, and reports the state a
 *   reader of those bytes would actually see -> download the resulting .dxf.
 *
 * Isolation: the only engine contact is web/src/cad/engineWorker.js's
 * EngineBoundary, unmodified — every message both directions is
 * schema-validated there. The worker is spawned lazily on the first open,
 * never at mount, and the ONLY place this module names the worker path is
 * the one spawn shape the license fence allows (deny rule 3).
 *
 * Persistence deliberately stays out of this slice (the save-as-new-version
 * leg is the card's second PR): nothing is uploaded, nothing touches the
 * project, the download is the only output.
 *
 * Flag: ENV_CAD_EDIT must be the FIRST operand of the `&&` at the call site
 * so a flag-off build folds this whole module away.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { EngineBoundary } from '../cad/engineWorker.js'

import { ENV_CAD_EDIT } from './flag.js'

// Mirrors the worker's own bound. Checked against File.size BEFORE any read,
// so an oversized file costs a comparison, never a decode.
const MAX_DOCUMENT_BYTES = 16 * 1024 * 1024

function defaultCreateWorker() {
  // The one legal spawn shape, and the only place this module names the
  // engine worker's path (license fence deny rule 3).
  return new Worker(
    new URL('../../../vendor/acadrust-worker/worker-browser.mjs', import.meta.url),
    { type: 'module' },
  )
}

function fmt(n) {
  return Number.isInteger(n) ? String(n) : n.toFixed(2)
}

function parseDelta(raw) {
  const n = Number.parseFloat(raw)
  return Number.isFinite(n) ? n : null
}

async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

export default function CadEditSurface({
  enabled = ENV_CAD_EDIT,
  createWorker = defaultCreateWorker,
  // Card F-3 persistence leg: when the studio supplies a live drawing
  // target, edited bytes can be saved as a NEW VERSION through the same
  // versioned-control chain every write uses. Absent target = download-only
  // (the demo/site shell), stated honestly in the hint below.
  saveTarget = null, // { drawingId, headVersion, capability?, save(bytes, parent, digest) }
  onSaved = null,
  // The engine attribution NOTICE, served by the capability contract at
  // runtime (the client tree may not name the engine — license fence).
  notice = '',
}) {
  const boundaryRef = useRef(null)
  const [documentId, setDocumentId] = useState('')
  const [entities, setEntities] = useState([])
  const [entityCount, setEntityCount] = useState(0)
  const [selectedId, setSelectedId] = useState('')
  const [vertexIndex, setVertexIndex] = useState('0')
  const [dx, setDx] = useState('10')
  const [dy, setDy] = useState('0')
  const [layerName, setLayerName] = useState('')
  const [status, setStatus] = useState('')
  const [savedBytes, setSavedBytes] = useState(null)
  const [busy, setBusy] = useState(false)

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
    const boundary = new EngineBoundary({ flags: { cad_edit: true }, createWorker })
    boundary.onMessage((message) => {
      if (message.type === 'ready') return
      if (message.type === 'documentLoaded') {
        setEntities(message.entities ?? [])
        setEntityCount(message.entityCount ?? 0)
        setSelectedId('')
        setSavedBytes(null)
        setBusy(false)
        const others = (message.unsupported ?? []).length
        setStatus(
          `Loaded ${message.documentId}: ${message.entityCount} entities`
          + (others ? ` (${others} preserved as read-only kinds).` : '.'))
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
    if (op === 'moveVertex' || op === 'addVertex' || op === 'deleteVertex') {
      const vi = Number.parseInt(vertexIndex, 10)
      if (!Number.isInteger(vi) || vi < 0) {
        setStatus(`${op} refused: vertex must be a non-negative integer.`)
        return
      }
      payload.vertexIndex = vi
      if (op === 'moveVertex') {
        const deltaX = parseDelta(dx)
        const deltaY = parseDelta(dy)
        if (deltaX === null || deltaY === null) {
          setStatus('Move vertex refused: dx and dy must both be numbers.')
          return
        }
        payload.dx = deltaX
        payload.dy = deltaY
      }
      if (op === 'addVertex') {
        const x = parseDelta(dx)
        const y = parseDelta(dy)
        if (x === null || y === null) {
          setStatus('Add vertex refused: x and y must both be numbers.')
          return
        }
        payload.x = x
        payload.y = y
      }
    }
    if (op === 'setLayer') {
      const trimmed = layerName.trim()
      if (!trimmed) {
        setStatus('Set layer refused: enter a layer name.')
        return
      }
      payload.layer = trimmed
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
  }, [dx, dy, layerName, selectedId, vertexIndex])

  // The persistence leg: post the EXACT edited bytes with a client-computed
  // digest; the server recomputes, parses, and compare-and-sets against the
  // head. A 409 (head moved) reads back as a plain instruction to refresh.
  const saveToProject = useCallback(async () => {
    if (!savedBytes || !saveTarget || busy) return
    setBusy(true)
    setStatus('Saving to the project as a new version...')
    try {
      const digest = await sha256Hex(savedBytes)
      const receipt = await saveTarget.save(savedBytes, saveTarget.headVersion, digest)
      setBusy(false)
      const nv = receipt?.new_version?.version ?? receipt?.head
      setStatus(
        `Saved as version ${nv} (parent ${receipt?.new_version?.parent}), `
        + `digest ${String(receipt?.source_sha256 || digest).slice(0, 12)}…, `
        + `engine cost $${receipt?.cost?.engine_usd ?? 0}.`)
      onSaved?.(receipt)
    } catch (error) {
      setBusy(false)
      setStatus(error?.status === 409
        ? `Save refused: ${error.message}`
        : `Save failed: ${error?.message || error}`)
    }
  }, [busy, onSaved, saveTarget, savedBytes])

  if (!enabled) return null

  const selected = entities.find((entity) => entity.id === selectedId) || null
  const canEdit = selected !== null && selected.editable !== false && !busy

  return (
    <section className="cad-edit-workbench" data-testid="cad-edit-workbench" aria-label="CAD editing surface">
      <h3 className="cad-edit-workbench-title">Edit a DXF drawing</h3>
      <p className="cad-edit-workbench-hint">
        {saveTarget
          ? 'Edits run entirely in your browser, inside the isolated engine worker. Nothing leaves it until you explicitly save a new version to the project or download the result.'
          : 'Runs entirely in your browser, inside the isolated engine worker. Nothing is uploaded and nothing is saved to the project — download the result to keep it.'}
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
                  disabled={entity.editable === false}
                />
                {entity.type} on layer {entity.layer}
                {' '}· {entity.vertices?.length ?? 0} vertices{entity.closed ? ' · closed' : ''}
                {entity.editable === false ? ' · read-only' : ''}
                {entity.vertices?.length > 0 && (
                  <span className="cad-edit-entity-verts">
                    {' '}({entity.vertices.slice(0, 2).map((v) => `${fmt(v[0])},${fmt(v[1])}`).join(' → ')}
                    {entity.vertices.length > 2 ? ' …' : ''})
                  </span>
                )}
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
          <label>
            vertex
            <input
              type="text"
              inputMode="numeric"
              value={vertexIndex}
              onChange={(e) => setVertexIndex(e.target.value)}
              aria-label="vertex index"
            />
          </label>
          <button type="button" onClick={() => runEdit('moveVertex')} disabled={!canEdit}>
            Move vertex by dx,dy
          </button>
          <button type="button" onClick={() => runEdit('addVertex')} disabled={!canEdit}>
            Add vertex after (at dx,dy)
          </button>
          <button type="button" onClick={() => runEdit('deleteVertex')} disabled={!canEdit}>
            Delete vertex
          </button>
          <label>
            layer
            <input
              type="text"
              value={layerName}
              onChange={(e) => setLayerName(e.target.value)}
              aria-label="layer name"
            />
          </label>
          <button type="button" onClick={() => runEdit('setLayer')} disabled={!canEdit}>
            Set layer
          </button>
        </div>
      )}

      {savedBytes && saveTarget && (
        <button
          type="button"
          className="cad-edit-workbench-save"
          data-testid="cad-edit-save-version"
          onClick={saveToProject}
          disabled={busy}
        >
          Save to project as new version
        </button>
      )}

      {downloadUrl && (
        <a className="cad-edit-workbench-download" href={downloadUrl} download={documentId || 'edited.dxf'}>
          Download edited DXF
        </a>
      )}

      <p role="status" aria-live="polite">{status}</p>

      {notice && (
        <p className="cad-edit-workbench-notice" data-testid="cad-edit-engine-notice">
          {notice}
        </p>
      )}
    </section>
  )
}
