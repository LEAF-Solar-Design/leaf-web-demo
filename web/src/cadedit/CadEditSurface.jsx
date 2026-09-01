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
 * W1 (convergence): the session itself — EngineBoundary construction, worker
 * lifetime, document bytes, the entity list, selection identity, edit
 * dispatch, the save-as-version flow and every busy/error/refusal state —
 * moved to ./engineSession.js, the ONE engine-session store
 * (docs/convergence/ACCEPTANCE.md "Engine-session ownership"). This file is
 * now purely its consumer: it renders the store and owns nothing but form
 * inputs and the download URL. No visual or behavioural change came with
 * that move.
 *
 * Isolation: the only engine contact is web/src/cad/engineWorker.js's
 * EngineBoundary, unmodified — every message both directions is
 * schema-validated there. The worker is spawned lazily on the first open,
 * never at mount, and the ONLY place this repo's web tree names the engine
 * worker path is the one spawn shape the license fence allows (deny rule 3),
 * which stays HERE. The store never names it: it takes the factory as a
 * required injected dependency precisely so the extraction adds no second
 * legal site (docs/CAD-ENGINE-LICENSE-FENCE.md).
 *
 * Flag: ENV_CAD_EDIT must be the FIRST operand of the `&&` at the call site
 * so a flag-off build folds this whole module away — and, transitively,
 * engineSession.js, which nothing else imports.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'

import { useDrawingIdentityOptional } from '../drawing/DrawingIdentityProvider.jsx'

import { ENV_CAD_EDIT } from './flag.js'
import useEngineSession from './engineSession.js'

function defaultCreateWorker() {
  // The one legal spawn shape, and the only place this repo's web tree names
  // the engine worker's path (license fence deny rule 3). It stays at the
  // call site: the engine-session store takes this factory as an argument so
  // there is exactly one such site to bless, not two.
  return new Worker(
    new URL('../../../vendor/acadrust-worker/worker-browser.mjs', import.meta.url),
    { type: 'module' },
  )
}

function fmt(n) {
  return Number.isInteger(n) ? String(n) : n.toFixed(2)
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
  // The surface stays mountable on its own (its own specs, a future embed):
  // no provider means no drawing identity, which is a real state. When there
  // IS one, a drawing switch resets the session — no cross-document bleed.
  const identity = useDrawingIdentityOptional()
  const session = useEngineSession({
    createWorker,
    saveTarget,
    onSaved,
    drawingId: identity?.drawingId ?? null,
  })
  const {
    documentId, entities, entityCount, selectedId, selected, status, savedBytes, busy,
  } = session

  // Form inputs stay here: they are what the operator is typing, not session
  // state the store owes anyone.
  const [vertexIndex, setVertexIndex] = useState('0')
  const [dx, setDx] = useState('10')
  const [dy, setDy] = useState('0')
  const [layerName, setLayerName] = useState('')

  const downloadUrl = useMemo(() => {
    if (!savedBytes) return ''
    return URL.createObjectURL(new Blob([savedBytes], { type: 'application/dxf' }))
  }, [savedBytes])

  useEffect(() => () => {
    if (downloadUrl) URL.revokeObjectURL(downloadUrl)
  }, [downloadUrl])

  const { open, applyEdit } = session.actions
  const openFile = useCallback((event) => {
    const file = event.target.files?.[0]
    // Cleared BEFORE the async read so re-choosing the same file still fires.
    event.target.value = ''
    open(file)
  }, [open])

  const runEdit = useCallback((op) => {
    applyEdit(op, { dx, dy, vertexIndex, layer: layerName })
  }, [applyEdit, dx, dy, layerName, vertexIndex])

  if (!enabled) return null

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
                  onChange={() => session.actions.select(entity.id)}
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
          onClick={session.actions.save}
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
