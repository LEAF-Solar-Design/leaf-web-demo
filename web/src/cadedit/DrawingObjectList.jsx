import { useEffect, useId, useLayoutEffect, useRef } from 'react'
import { hexHandle } from './engineIntake.js'
import { clickIntent, isToggleKey, LONG_PRESS_MS, LONG_PRESS_SLOP_PX, rangeIds } from './selection.js'
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

// Selection has no checkbox column: the radio selects only its row, Shift
// click selects a range from the anchor in session.entities order, Ctrl or
// Cmd click toggles one row in or out, X toggles the row holding keyboard
// focus, and a 500 ms touch press enters selection (later taps toggle until
// the selection is empty again).
export default function DrawingObjectList({ session, announce }) {
  const objects = useDrawingObjects()
  const prefix = useId()
  const keyboardFocus = useRef(null)
  const anchor = useRef(null)
  const press = useRef(null)
  const lastPointer = useRef(null)
  const swallowClick = useRef(false)
  const touchSelecting = useRef(false)
  const live = useRef(null)
  useLayoutEffect(() => {
    const node = keyboardFocus.current
    // Moving a keyed row during a reorder can drop DOM focus to the body.
    if (node?.isConnected && document.activeElement === document.body) node.focus()
  })
  useEffect(() => () => { if (press.current) clearTimeout(press.current.timer) }, [])
  const index = objects?.index?.drawingKey === `engine:${session.documentId}` ? objects.index : null
  const selected = new Set((session.selectedIds || []).map(String))
  live.current = { selected }
  const cancelPress = () => {
    if (press.current) clearTimeout(press.current.timer)
    press.current = null
  }
  const toggleRow = (entity, identity) => {
    anchor.current = identity
    session.actions.selectToggle(entity.id)
  }
  // The anchor is the last row selected alone or toggled; failing that, the
  // newest visible selected row; failing that, the clicked row itself.
  const anchorFor = (identity) => {
    const visible = new Set(session.entities.map((entity) => String(entity.id)))
    if (anchor.current !== null && visible.has(anchor.current)) return anchor.current
    const ids = session.selectedIds || []
    for (let i = ids.length - 1; i >= 0; i -= 1) if (visible.has(String(ids[i]))) return String(ids[i])
    return identity
  }
  const selectRange = (identity, add) => {
    const from = anchorFor(identity)
    anchor.current = from
    const ids = rangeIds(session.entities, from, identity)
    session.actions.selectReplace(add ? [...(session.selectedIds || []), ...ids] : ids)
  }
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
              const pointer = lastPointer.current
              lastPointer.current = null
              if (event.target.closest('[data-object-action]')) return
              if (swallowClick.current) {
                // The release that ends a long press is not a second tap.
                swallowClick.current = false
                event.preventDefault()
                return
              }
              // Touch selection ends once the selection is empty again.
              if (selected.size === 0) touchSelecting.current = false
              const intent = clickIntent(event)
              const touchTap = !intent && pointer === 'touch' && touchSelecting.current
              if (!intent && !touchTap) return
              event.preventDefault()
              if (readOnly) return
              if (intent === 'range' || intent === 'rangeAdd') selectRange(identity, intent === 'rangeAdd')
              else toggleRow(entity, identity)
            }}
            onKeyDown={(event) => {
              swallowClick.current = false
              if (!isToggleKey(event.nativeEvent)) return
              event.preventDefault()
              if (readOnly) announce?.(DRAWING_OBJECT_REASONS.readOnly)
              else toggleRow(entity, identity)
            }}
            onPointerDown={(event) => {
              lastPointer.current = event.pointerType || null
              swallowClick.current = false
              cancelPress()
              if (event.pointerType !== 'touch' || event.target.closest('[data-object-action]')) return
              const timer = setTimeout(() => {
                press.current = null
                swallowClick.current = true
                if (readOnly) { announce?.(DRAWING_OBJECT_REASONS.readOnly); return }
                touchSelecting.current = true
                anchor.current = identity
                if (!live.current.selected.has(identity)) session.actions.selectToggle(entity.id)
              }, LONG_PRESS_MS)
              press.current = { timer, x: event.clientX, y: event.clientY }
            }}
            onPointerMove={(event) => {
              const start = press.current
              if (start && Math.hypot(event.clientX - start.x, event.clientY - start.y) > LONG_PRESS_SLOP_PX) cancelPress()
            }}
            onPointerUp={cancelPress}
            onPointerCancel={cancelPress}
            onContextMenu={(event) => { if (swallowClick.current) event.preventDefault() }}
          >
            {record && <><strong>{record.name}</strong><span className="drawing-object-path">{record.path}</span></>}
            <label className="drawing-object-radio">
              <input type="radio" name="cad-edit-entity" value={identity}
                checked={selected.size === 1 && inSelection}
                aria-disabled={readOnly || undefined}
                aria-describedby={readOnly ? selectionReasonId : undefined}
                onChange={(event) => {
                  // The row's click handler runs first and cancels any click it took over.
                  if (readOnly || modified(event.nativeEvent) || event.nativeEvent.defaultPrevented) return
                  anchor.current = identity
                  session.actions.selectReplace([entity.id])
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
