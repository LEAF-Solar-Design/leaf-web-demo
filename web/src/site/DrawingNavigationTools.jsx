import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { useDrawingObjects } from './DrawingObjectsContext.jsx'
import { useEngineSessionOptional } from '../cadedit/EngineSessionProvider.jsx'
import { useSurfaceFrame } from './SurfaceFrame.jsx'
import { useStudioGround } from './studioGround.js'
import './drawingNavigation.css'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { isCompositionKey } from '../lib/keyComposition.js'

// The index preserves physical containment, not layer membership. Recover
// underlying layers from geometry, then propagate them to physical parents.
function objectLayers(index, entities) {
  const layers = new Map()
  const add = (record, layer) => {
    while (record) {
      if (!layers.has(record.id)) layers.set(record.id, new Set())
      layers.get(record.id).add(layer ?? '0')
      record = index.byId.get(record.physicalParentId)
    }
  }
  const byEngineId = new Map()
  for (const record of index?.records || []) {
    if (record.kind === 'layer') add(record, record.name)
    for (const id of record.engineIds) byEngineId.set(id, record)
  }
  for (const entity of entities || []) add(byEngineId.get(String(entity.id)), entity.layer)
  for (const kind of ['polylines', 'inserts', 'faces3d', 'circles', 'arcs']) {
    for (const entity of index?.intake?.[kind] || []) {
      const handle = String(entity.sourceHandle ?? entity.handle ?? '').replace(/^0x/i, '').toUpperCase()
      add(index.byHandle.get(handle), entity.layer)
    }
  }
  return layers
}

export default function DrawingNavigationTools({ navigationSourceRef, navigation }) {
  const objects = useDrawingObjects()
  const engine = useEngineSessionOptional()
  const frame = useSurfaceFrame()
  const ground = useStudioGround()
  const [query, setQuery] = useState('')
  const [results, setResults] = useState(null)
  const [active, setActive] = useState(0)
  const [message, setMessage] = useState('')
  const inputId = useId(), listId = useId(), pathId = useId()
  const listRef = useRef(null)
  const index = objects?.index
  const layers = useMemo(() => objectLayers(index, engine?.session.entities), [index, engine?.session.entities])
  const latest = useRef(null)
  latest.current = { objects, engine, query, layers }
  useEffect(() => {
    if (!navigationSourceRef) return undefined
    const source = {
      get drawingKey() { return latest.current.objects?.index?.drawingKey ?? null },
      capture: () => {
        const { objects: current, query: text } = latest.current
        return { drawingKey: current?.index?.drawingKey ?? null, focusedId: current?.focusId ?? null,
          selectedHandles: [...(current?.selectedHandles || [])], query: text }
      },
      focus: (id) => latest.current.objects.focus(id),
      parent: () => {
        const current = latest.current.objects
        return current?.index?.byId.get(current.index.byId.get(current.focusId)?.physicalParentId)
      },
      isVisible: (id, visible) => {
        const underlying = latest.current.layers.get(id)
        return !underlying?.size || [...underlying].some((layer) => visible?.[layer] !== false)
      },
      restore: (snapshot) => {
        const { objects: current, engine: sessionContext } = latest.current
        setMessage('')
        if (snapshot.focusedId) {
          const result = current.focus(snapshot.focusedId)
          if (!result.ok) { current.clearFocus(); setMessage(result.reason) }
        } else current?.clearFocus()
        setQuery(snapshot.query); setResults(null)
        const session = sessionContext?.session
        if (!session || current?.index?.drawingKey !== `engine:${session.documentId}`) return false
        const handles = snapshot.selectedHandles
        if (!handles.length) session.actions.selectClear()
        else session.actions.selectReplace([...new Set(handles.flatMap((handle) => current.index.byHandle.get(handle)?.engineIds || []))])
        return true
      },
    }
    navigationSourceRef.current = source
    return () => { if (navigationSourceRef.current === source) navigationSourceRef.current = null }
  }, [navigationSourceRef])
  useEffect(() => {
    navigation?.syncScope()
    setQuery(''); setResults(null); setMessage('')
  }, [index?.drawingKey, navigation?.syncScope])
  useEffect(() => {
    const list = listRef.current
    if (list?.children[active]) list.parentElement.scrollTop = list.children[active].offsetTop
  }, [results, active])

  const choose = (record) => {
    const result = navigation.jump(record.id)
    setResults(null)
    setMessage(result.ok ? '' : result.reason)
  }
  const resolve = () => {
    const result = index.resolve(query)
    setMessage('')
    if (result.status === 'unique') choose(result.matches[0])
    else if (result.status === 'ambiguous') { setResults(result); setActive(0) }
    else { setResults(null); setMessage('No matching object in this drawing.') }
  }
  // S27: Escape in the Find field closes its match list (and is the field's
  // own, never the drawing's or the shell's) through the one owner stack:
  // menu layer, scoped to the field, as the old input handler was.
  const inputRef = useRef(null)
  useEscapeOwner('drawing-find', true, () => setResults(null), { layer: 'menu', scope: inputRef, scoped: true })
  const keyDown = (event) => {
    if (isCompositionKey(event)) return
    if (event.nativeEvent.isComposing) return
    if (event.key === 'Enter') {
      event.preventDefault(); event.stopPropagation()
      if (results) choose(results.matches[active])
      else resolve()
    } else if (results && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      event.preventDefault(); event.stopPropagation()
      setActive((value) => (value + (event.key === 'ArrowDown' ? 1 : results.matches.length - 1)) % results.matches.length)
    }
  }
  if (!ground || frame?.contract?.ground !== 'drawing' || !index) return null
  const focused = index.byId.get(objects.focusId)
  return <div className="drawing-navigation">
    <div className="drawing-find-band" data-nav-find>
    <div className="drawing-find-field">
    <label htmlFor={inputId}>Find in drawing</label>
    {/* Autocomplete is off because queries are specific to the current drawing. */}
    <input ref={inputRef} id={inputId} role="combobox" value={query} autoComplete="off"
      aria-autocomplete="list" aria-expanded={!!results} aria-controls={results ? listId : undefined}
      aria-activedescendant={results ? `${listId}-${active}` : undefined}
      onChange={(event) => { setQuery(event.target.value); setResults(null); setMessage('') }} onKeyDown={keyDown} />
    </div>
    <div className="drawing-find-status">
    {message && <span role="status">{message}</span>}
    {focused && <span className="drawing-focus">
      <span aria-describedby={pathId}>Focus: {focused.name}</span>
      <span id={pathId} hidden>{focused.path}</span>
      <button type="button" onClick={objects.clearFocus} aria-label="Clear focus">Clear</button>
    </span>}
    </div>
    </div>
    {results && <div className="drawing-find-results">
      <ul ref={listRef} id={listId} role="listbox" aria-label="Drawing matches">
        {results.matches.map((record, i) => <li id={`${listId}-${i}`} key={record.id} role="option"
          aria-selected={i === active} onMouseDown={(event) => event.preventDefault()} onClick={() => choose(record)}>{record.path}</li>)}
      </ul>
      {results.truncated && <p>More matches; refine your search</p>}
    </div>}
  </div>
}
