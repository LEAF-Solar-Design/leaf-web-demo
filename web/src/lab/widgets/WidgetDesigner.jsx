import { memo, useEffect, useRef, useState } from 'react'
import WidgetCard from './WidgetCard.jsx'
import WidgetInspector from './WidgetInspector.jsx'
import WidgetLayers from './WidgetLayers.jsx'
import { FORMATS, PRESETS } from './widgetCatalog.js'
import { createWidgetDoc, CUTOUTS, HANDLES, supportsPreset } from './useWidgetEditor.js'

const Card = memo(WidgetCard)
export const FORMAT_NAMES = { la: 'Lock screen', diExp: 'Island expanded', diCompact: 'Island compact', diMin: 'Island minimal', standby: 'StandBy', lockRect: 'Rectangular', lockCirc: 'Circular', lockInline: 'Inline', homeS: 'Small', homeM: 'Medium', homeL: 'Large', watchStack: 'Smart Stack', watchCorner: 'Complication' }
export default function WidgetDesigner({ state, dispatch, local, hidden = false, invalid = false, onOutputs, graphOutputs }) {
  const { doc, selectedId, guides } = state, f = doc.formats[doc.active], W = FORMATS[doc.active].w
  const selected = f.els.find((e) => e.id === selectedId), cutout = CUTOUTS[doc.active]
  const surfaceRef = useRef(null), drag = useRef(null), lastTree = useRef(null)
  const rootRef = useRef(null), removalFocus = useRef(null)
  function designerDispatch(action) {
    if (action.type === 'delete' && !state.transaction) {
      const index = f.els.findIndex((element) => element.id === (action.id ?? selectedId))
      if (index >= 0) removalFocus.current = f.els[index + 1]?.id ?? f.els[index - 1]?.id ?? 'add'
    }
    dispatch(action)
  }
  useEffect(() => {
    if (removalFocus.current === null) return
    const target = [...rootRef.current.querySelectorAll('[data-layer-id]')].find((row) => row.dataset.layerId === removalFocus.current)
    const control = target ?? rootRef.current.querySelector('[data-add-control]')
    control?.focus()
    removalFocus.current = null
  }, [f.els])
  if (!lastTree.current) lastTree.current = createWidgetDoc()
  const [preset, setPreset] = useState('text')
  const allowedPreset = supportsPreset(doc.active, preset) ? preset : 'text'
  if (!invalid) lastTree.current = doc
  const paintDoc = Object.hasOwn(lastTree.current.formats, doc.active) ? lastTree.current : createWidgetDoc()
  useEffect(() => { drag.current = null }, [doc.active])
  useEffect(() => () => { drag.current = null }, [dispatch])
  useEffect(() => {
    if (hidden) { if (drag.current) { drag.current = null; dispatch({ type: 'cancel' }) } return }
    function keydown(event) {
      if (!surfaceRef.current?.isConnected || surfaceRef.current.closest('[hidden]')) return
      if (event.target.closest?.('input, textarea, select, [contenteditable="true"]')) return
      if (drag.current) {
        if (event.key === 'Escape') { drag.current = null; dispatch({ type: 'cancel' }); event.preventDefault() }
        else if (event.ctrlKey || event.metaKey || event.key.startsWith('Arrow') || ['Delete', 'Backspace'].includes(event.key)) event.preventDefault()
        return
      }
      const modifier = event.ctrlKey || event.metaKey, key = event.key.toLowerCase()
      if (modifier && key === 'z') { event.preventDefault(); dispatch({ type: event.shiftKey ? 'redo' : 'undo' }); return }
      if (modifier && key === 'd' && selectedId) { event.preventDefault(); dispatch({ type: 'duplicate' }); return }
      if (event.key === 'Escape') { dispatch({ type: 'select', id: null }); return }
      if (!selectedId) return
      const step = event.shiftKey ? 8 : 1, delta = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[event.key]
      if (delta) { event.preventDefault(); dispatch({ type: 'nudge', dx: delta[0], dy: delta[1], time: Date.now() }) }
      else if (['Delete', 'Backspace'].includes(event.key)) { event.preventDefault(); dispatch({ type: 'delete' }) }
    }
    document.addEventListener('keydown', keydown)
    return () => document.removeEventListener('keydown', keydown)
  }, [dispatch, selectedId, hidden])
  function start(event, element, handle = 'move') {
    if (hidden || drag.current) return
    if (event.button != null && event.button !== 0) return
    const bounds = surfaceRef.current.getBoundingClientRect()
    drag.current = { id: element.id, original: element, handle, x: event.clientX, y: event.clientY, zoom: bounds.width / W || 1, pointerId: event.pointerId }
    dispatch({ type: 'begin', id: element.id })
    surfaceRef.current.focus({ preventScroll: true })
    surfaceRef.current.setPointerCapture?.(event.pointerId)
    event.preventDefault(); event.stopPropagation()
  }
  function move(event) {
    const gesture = drag.current
    if (!gesture || gesture.pointerId !== event.pointerId) return
    dispatch({ type: gesture.handle === 'move' ? 'move' : 'resize', id: gesture.id, original: gesture.original, handle: gesture.handle,
      dx: (event.clientX - gesture.x) / gesture.zoom, dy: (event.clientY - gesture.y) / gesture.zoom, alt: event.altKey })
  }
  function end(event, cancel = false) {
    if (!drag.current || drag.current.pointerId !== event.pointerId) return
    drag.current = null
    if (surfaceRef.current.hasPointerCapture?.(event.pointerId)) surfaceRef.current.releasePointerCapture(event.pointerId)
    dispatch({ type: cancel ? 'cancel' : 'end' })
  }
  return <div ref={rootRef} className="widget-designer" hidden={hidden}>
    <div className="widget-formats" aria-label="Widget formats">{Object.keys(FORMATS).map((id) => <button type="button" key={id} aria-pressed={doc.active === id} onClick={() => dispatch({ type: 'format', format: id })}>{FORMAT_NAMES[id]}</button>)}</div>
    <div className="widget-toolbar">
      <button type="button" data-add-control disabled={f.els.length >= 32} onClick={() => dispatch({ type: 'add', preset: 'text' })}>Add text</button>
      <label>Element preset<select value={allowedPreset} onChange={(e) => setPreset(e.target.value)}>{Object.entries(PRESETS).filter(([id]) => supportsPreset(doc.active, id)).map(([id, p]) => <option key={id} value={id}>{p.n}</option>)}</select></label>
      <button type="button" disabled={f.els.length >= 32} onClick={() => dispatch({ type: 'add', preset: allowedPreset })}>Add element</button>
      <button type="button" disabled={!state.past.length} onClick={() => dispatch({ type: 'undo' })}>Undo</button>
      <button type="button" disabled={!state.future.length} onClick={() => dispatch({ type: 'redo' })}>Redo</button>
      <button type="button" onClick={() => dispatch({ type: 'select', id: null })}>Edit surface</button>
    </div>
    <p>{f.els.length} of 32 elements.</p>
    <div className="widget-designer-layout">
      <WidgetLayers elements={f.els} selectedId={selectedId} dispatch={designerDispatch} />
      <section aria-label="Preview" className="widget-designer-preview">
        {!f.els.length && <p>Empty. Add an element to start your preview.</p>}
        <div ref={surfaceRef} tabIndex={0} aria-label="Widget canvas" className="widget-designer-surface" data-testid="widget-surface" style={{ maxWidth: W, aspectRatio: `${W} / ${f.h}`, borderRadius: f.radius }}
          onPointerDown={() => dispatch({ type: 'select', id: null })} onPointerMove={move} onPointerUp={end} onPointerCancel={(e) => end(e, true)} onLostPointerCapture={(e) => end(e, true)}>
          <Card doc={paintDoc} format={doc.active} label="Widget preview" />
          <svg className="widget-interaction" viewBox={`0 0 ${W} ${f.h}`} aria-label="Widget editing surface">
            {f.els.filter((e) => e.visible).map((element) => <rect key={element.id} data-element-id={element.id} x={element.x} y={element.y} width={element.w} height={element.h} fill="transparent" onPointerDown={(e) => start(e, element)} />)}
            {cutout && <rect className="widget-cutout" x={cutout.x} y={cutout.y} width={cutout.w} height={cutout.h} rx={18} />}
            {guides.x != null && <line className="widget-guide" x1={guides.x} x2={guides.x} y1={0} y2={f.h} />}
            {guides.y != null && <line className="widget-guide" y1={guides.y} y2={guides.y} x1={0} x2={W} />}
            {selected?.visible && <g>
              <rect className="widget-selection" x={selected.x} y={selected.y} width={selected.w} height={selected.h} />
              {(selected.preset === 'divider' ? ['w', 'e'] : HANDLES).map((handle) => {
                const x = selected.x + (handle.includes('w') ? 0 : handle.includes('e') ? selected.w : selected.w / 2)
                const y = selected.y + (handle.includes('n') ? 0 : handle.includes('s') ? selected.h : selected.h / 2)
                return <rect key={handle} className="widget-handle" data-handle={handle} aria-label={`Resize ${handle}`} x={x - 3} y={y - 3} width={6} height={6} style={{ cursor: `${handle}-resize` }} onPointerDown={(e) => start(e, selected, handle)} />
              })}
            </g>}
          </svg>
        </div>
        {selected && <p>{selected.w} × {selected.h} points at {selected.x}, {selected.y}</p>}
      </section>
      <WidgetInspector doc={doc} selectedId={selectedId} dispatch={designerDispatch} local={local} onOutputs={onOutputs} graphOutputs={graphOutputs} />
    </div>
  </div>
}
