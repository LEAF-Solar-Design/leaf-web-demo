import { useId, useLayoutEffect, useRef } from 'react'
import { hexHandle } from './engineIntake.js'
import { validObjectBounds } from '../lib/drawingObjectIndex.js'
import { useDrawingObjects } from '../site/DrawingObjectsContext.jsx'
import './drawingObjectList.css'

const DRAWING_OBJECT_REASONS = Object.freeze({
  readOnly: 'This object is read-only and cannot be selected for editing.',
  missing: 'Focus is unavailable because this object has no navigation record for this drawing.',
  bounds: 'Focus is unavailable because this object has no drawing bounds.',
  refused: 'Focus is unavailable because the viewer could not frame this object.',
})
const fmt = (n) => Number.isInteger(n) ? String(n) : n.toFixed(2)
const modified = (event) => event.shiftKey || event.ctrlKey || event.metaKey

export default function DrawingObjectList({ session, announce }) {
  const objects = useDrawingObjects()
  const prefix = useId()
  const keyboardFocus = useRef(null)
  useLayoutEffect(() => {
    const node = keyboardFocus.current
    // Moving a keyed row during a reorder can drop DOM focus to the body.
    if (node?.isConnected && document.activeElement === document.body) node.focus()
  })
  const index = objects?.index?.drawingKey === `engine:${session.documentId}` ? objects.index : null
  const selected = new Set((session.selectedIds || []).map(String))
  return (
    // A separate form owner keeps the legacy radios with the same name in
    // their own native group; both lists still reflect the one selection.
    <form onSubmit={(event) => event.preventDefault()}>
    <ul className="drawing-object-list" aria-label="Drawing objects"
      onFocusCapture={(event) => { keyboardFocus.current = event.target }}
      onBlurCapture={(event) => { if (event.relatedTarget) keyboardFocus.current = null }}>
      {session.entities.map((entity) => {
        const identity = String(entity.id)
        const record = index?.byHandle.get(hexHandle(entity.id))
        const readOnly = entity.editable === false
        const inSelection = selected.has(identity)
        const focused = !!record && objects?.focusId === record.id
        const selectionReasonId = `${prefix}-${identity}-selection`
        const focusReasonId = `${prefix}-${identity}-focus`
        const canFocus = !!record && validObjectBounds(record.bounds) && typeof objects?.focus === 'function'
        const focusObject = () => {
          if (!canFocus) {
            announce?.(record ? DRAWING_OBJECT_REASONS.bounds : DRAWING_OBJECT_REASONS.missing)
            return
          }
          const result = objects.focus(record.id)
          if (result?.ok) announce?.(`Focused ${record.name}`)
          else announce?.(result?.reason ? `Focus unavailable: ${result.reason}` : DRAWING_OBJECT_REASONS.refused)
        }
        return (
          <li key={identity} data-object-id={identity}
            data-selected={inSelection ? 'true' : undefined}
            data-focused={focused ? 'true' : undefined}
            onClick={(event) => {
              if (event.target.closest('[data-object-action]') || !modified(event) || readOnly) return
              event.preventDefault()
              session.actions.selectToggle(entity.id)
            }}
          >
            {record && <><strong>{record.name}</strong><span className="drawing-object-path">{record.path}</span></>}
            <label className="drawing-object-radio">
              <input type="radio" name="cad-edit-entity" value={identity}
                checked={selected.size === 1 && inSelection}
                aria-disabled={readOnly || undefined}
                aria-describedby={readOnly ? selectionReasonId : undefined}
                onChange={(event) => {
                  if (!readOnly && !modified(event.nativeEvent)) session.actions.selectReplace([entity.id])
                }}
              />
              Select only: {entity.type} on layer {entity.layer}
              {' '}· {entity.vertices?.length ?? 0} vertices{entity.closed ? ' · closed' : ''}
              {entity.vertices?.length > 0 && (
                <span className="cad-edit-entity-verts">
                  {' '}({entity.vertices.slice(0, 2).map((v) => `${fmt(v[0])},${fmt(v[1])}`).join(' → ')}
                  {entity.vertices.length > 2 ? ' …' : ''})
                </span>
              )}
            </label>
            <div className="drawing-object-actions">
              <label data-object-action="selection">
                <input type="checkbox" checked={inSelection}
                  aria-disabled={readOnly || undefined}
                  aria-describedby={readOnly ? selectionReasonId : undefined}
                  onChange={() => { if (!readOnly) session.actions.selectToggle(entity.id) }}
                />
                Add to selection
              </label>
              <button type="button" data-object-action="focus" onClick={focusObject}
                aria-disabled={!canFocus || undefined} aria-describedby={!canFocus ? focusReasonId : undefined}
              >Focus</button>
            </div>
            <span className="drawing-object-state">
              {inSelection && <span>Selected</span>}
              {focused && <span>Focused</span>}
              {readOnly && <span>Read-only</span>}
            </span>
            {readOnly && <span id={selectionReasonId} className="drawing-object-reason">{DRAWING_OBJECT_REASONS.readOnly}</span>}
            {!canFocus && <span id={focusReasonId} className="drawing-object-reason">{record ? DRAWING_OBJECT_REASONS.bounds : DRAWING_OBJECT_REASONS.missing}</span>}
          </li>
        )
      })}
    </ul>
    </form>
  )
}
