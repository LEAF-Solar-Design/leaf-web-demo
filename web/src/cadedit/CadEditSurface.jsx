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
 * (docs/convergence/ACCEPTANCE.md "Engine-session ownership").
 *
 * W4d (Slice A): the ONE call of that store moved out of here into
 * ./EngineSessionProvider.jsx, together with the one legal worker spawn, so
 * the ribbon's Modify group can consume the same session. This file is now
 * purely a CONSUMER: it renders the store through context and owns nothing
 * but the download URL. It names no worker path and constructs no boundary
 * (license fence deny rule 3; engineOwnership.test.js counts both shapes).
 * The operator's inputs (dx, dy, vertex, layer) are the provider's ONE
 * record, shared with the ribbon.
 *
 * Isolation: the only engine contact is web/src/cad/engineWorker.js's
 * EngineBoundary, unmodified — every message both directions is
 * schema-validated there. The worker is spawned lazily on the first open,
 * never at mount.
 *
 * Flag: ENV_CAD_EDIT must be the FIRST operand of the `&&` at the call site
 * so a flag-off build folds this whole module away — and, transitively, the
 * provider and engineSession.js, which nothing else imports.
 */
import { useCallback, useEffect, useMemo } from 'react'

import { ENV_CAD_EDIT } from './flag.js'
import { DEFAULT_EDIT_INPUTS, useEngineSessionOptional } from './EngineSessionProvider.jsx'
import LiveRegion from '../components/LiveRegion.jsx'

function fmt(n) {
  return Number.isInteger(n) ? String(n) : n.toFixed(2)
}

const noop = () => {}

export default function CadEditSurface({
  enabled = ENV_CAD_EDIT,
  // The engine attribution NOTICE, served by the capability contract at
  // runtime (the client tree may not name the engine — license fence).
  notice = '',
}) {
  const engine = useEngineSessionOptional()
  const session = engine?.session ?? null
  const inputs = engine?.inputs ?? DEFAULT_EDIT_INPUTS
  const setInput = engine?.setInput ?? noop
  const canSave = !!engine?.canSave
  const {
    documentId = '', entities = [], entityCount = 0, selectedId = '', selected = null,
    status = '', savedBytes = null, busy = false,
  } = session ?? {}

  const downloadUrl = useMemo(() => {
    if (!savedBytes) return ''
    return URL.createObjectURL(new Blob([savedBytes], { type: 'application/dxf' }))
  }, [savedBytes])

  useEffect(() => () => {
    if (downloadUrl) URL.revokeObjectURL(downloadUrl)
  }, [downloadUrl])

  const open = session?.actions.open
  const applyEdit = session?.actions.applyEdit
  const openFile = useCallback((event) => {
    const file = event.target.files?.[0]
    // Cleared BEFORE the async read so re-choosing the same file still fires.
    event.target.value = ''
    open?.(file)
  }, [open])

  const runEdit = useCallback((op) => {
    applyEdit?.(op, inputs)
  }, [applyEdit, inputs])

  if (!enabled) return null
  if (!session) {
    // A consumer outside the ONE mount is a wiring bug, never a silent
    // second session.
    throw new Error('CadEditSurface renders only inside EngineSessionProvider (the ONE engine-session mount)')
  }

  const canEdit = selected !== null && selected.editable !== false && !busy

  return (
    <section className="cad-edit-workbench" data-testid="cad-edit-workbench" aria-label="CAD editing surface">
      <h3 className="cad-edit-workbench-title">Edit a DXF drawing</h3>
      <p className="cad-edit-workbench-hint">
        {canSave
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
            <input type="text" inputMode="decimal" value={inputs.dx} onChange={(e) => setInput('dx', e.target.value)} aria-label="dx" />
          </label>
          <label>
            dy
            <input type="text" inputMode="decimal" value={inputs.dy} onChange={(e) => setInput('dy', e.target.value)} aria-label="dy" />
          </label>
          <button type="button" onClick={() => runEdit('move')} disabled={!canEdit}>
            Move selected
          </button>
          <label>
            vertex
            <input
              type="text"
              inputMode="numeric"
              value={inputs.vertexIndex}
              onChange={(e) => setInput('vertexIndex', e.target.value)}
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
              value={inputs.layer}
              onChange={(e) => setInput('layer', e.target.value)}
              aria-label="layer name"
            />
          </label>
          <button type="button" onClick={() => runEdit('setLayer')} disabled={!canEdit}>
            Set layer
          </button>
        </div>
      )}

      {savedBytes && canSave && (
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

      <LiveRegion as="p" role="status">{status}</LiveRegion>

      {notice && (
        <p className="cad-edit-workbench-notice" data-testid="cad-edit-engine-notice">
          {notice}
        </p>
      )}
    </section>
  )
}
