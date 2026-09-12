// Browser persistent stage, geometry mount. Touch supports one-pointer drag;
// pinch zoom is not in this slice. projectBoardObjects.js is the per-object
// card follow-up; these cards retain the board's six tile identities.
import { useEffect, useRef, useState } from 'react'
import { boundsOfRects, fitCameraToBounds, screenToWorld, worldToScreen } from './worldSpaceGeometry.js'
import localStore, { CARD_NAMES, validWorldState } from './worldSpaceStore.js'

export const DEFAULT_VIEWPORT = { width: 1200, height: 760 }
export const CARD_SIZE = { width: 320, height: 260 }
export const DEFAULT_POSITIONS = {
  drawing: { x: 0, y: 0 }, versions: { x: 352, y: 0 }, jobs: { x: 704, y: 0 },
  tools: { x: 0, y: 292 }, catalog: { x: 352, y: 292 }, shared: { x: 704, y: 292 },
}
const MINI = { width: 180, height: 114 }
const clamp = (zoom) => Math.max(0.25, Math.min(3, zoom))
const cardRect = (positions, name) => ({ ...positions[name], ...CARD_SIZE })
const allBounds = (positions) => boundsOfRects(CARD_NAMES.map((name) => cardRect(positions, name)))
function fit(bounds, viewport) {
  const fitted = fitCameraToBounds(bounds, viewport, Math.min(48, viewport.width / 4, viewport.height / 4))
  const zoom = clamp(fitted.zoom)
  return { x: viewport.width / 2 - (bounds.x + bounds.width / 2) * zoom,
    y: viewport.height / 2 - (bounds.y + bounds.height / 2) * zoom, zoom }
}

export default function WorldSpaceBoard({ scopeId = 'anonymous', viewport, store = localStore, children }) {
  const view = viewport?.width > 0 && viewport?.height > 0 ? viewport : DEFAULT_VIEWPORT
  const [state, setState] = useState(() => {
    try {
      const saved = store.load(scopeId)
      if (validWorldState(saved)) return { positions: saved.positions, camera: { ...saved.camera, zoom: clamp(saved.camera.zoom) } }
    } catch { /* Injectable stores have the same failure boundary. */ }
    return { positions: DEFAULT_POSITIONS, camera: fit(allBounds(DEFAULT_POSITIONS), view) }
  })
  const current = useRef(state)
  const drag = useRef(null)
  const root = useRef(null)
  const [focusCard, setFocusCard] = useState(null)
  const [reduced, setReduced] = useState(() => globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches || false)
  useEffect(() => {
    const media = globalThis.matchMedia?.('(prefers-reduced-motion: reduce)')
    if (!media) return undefined
    const update = () => setReduced(media.matches)
    update()
    media.addEventListener?.('change', update)
    return () => media.removeEventListener?.('change', update)
  }, [])
  const persist = (next = current.current) => { try { store.save(scopeId, next) } catch { /* Keep working offline. */ } }
  const update = (next, save = false) => {
    current.current = next
    setState(next)
    if (save) persist(next)
  }
  const focus = (name) => {
    setFocusCard(name)
    const next = current.current
    update({ ...next, camera: fit(name ? cardRect(next.positions, name) : allBounds(next.positions), view) }, true)
  }
  const zoomAt = (factor, anchor) => {
    const next = current.current
    const world = screenToWorld(anchor, next.camera)
    const zoom = clamp(next.camera.zoom * factor)
    update({ ...next, camera: { x: anchor.x - world.x * zoom, y: anchor.y - world.y * zoom, zoom } }, true)
  }
  useEffect(() => {
    const node = root.current
    const wheel = (event) => {
      event.preventDefault()
      if (event.target.closest('[data-minimap]')) return
      const rect = node.getBoundingClientRect()
      zoomAt(Math.exp(-Math.max(-1000, Math.min(1000, event.deltaY)) * 0.001), { x: event.clientX - rect.left, y: event.clientY - rect.top })
    }
    node.addEventListener('wheel', wheel, { passive: false })
    return () => node.removeEventListener('wheel', wheel)
  })
  const start = (event, name = null) => {
    if (drag.current || (event.button != null && event.button !== 0)) return
    if (event.target.closest('button, a, input, textarea, select, [data-minimap]')) return
    event.stopPropagation()
    event.currentTarget.focus()
    drag.current = { id: event.pointerId, name, x: event.clientX, y: event.clientY, state: current.current }
    event.currentTarget.setPointerCapture?.(event.pointerId)
  }
  const move = (event) => {
    const held = drag.current
    if (!held || held.id !== event.pointerId) return
    const dx = event.clientX - held.x
    const dy = event.clientY - held.y
    if (held.name) {
      const from = screenToWorld({ x: held.x, y: held.y }, held.state.camera)
      const to = screenToWorld({ x: event.clientX, y: event.clientY }, held.state.camera)
      const position = held.state.positions[held.name]
      update({ ...held.state, positions: { ...held.state.positions, [held.name]: { x: position.x + to.x - from.x, y: position.y + to.y - from.y } } })
    } else update({ ...held.state, camera: { ...held.state.camera, x: held.state.camera.x + dx, y: held.state.camera.y + dy } })
  }
  const end = (event, cancel = false) => {
    const held = drag.current
    if (!held || held.id !== event.pointerId) return
    drag.current = null
    if (cancel) update(held.state)
    else persist()
    event.target.releasePointerCapture?.(event.pointerId)
  }
  const keyDown = (event) => {
    if (event.target.closest('button, a, input, textarea, select')) return
    if (event.key === 'Escape' || event.key === '0') { event.preventDefault(); focus(null); return }
    if (event.target !== event.currentTarget) return
    const pan = { ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40] }[event.key]
    if (pan) {
      event.preventDefault()
      const next = current.current
      update({ ...next, camera: { ...next.camera, x: next.camera.x + pan[0], y: next.camera.y + pan[1] } }, true)
    } else if (['+', '=', '-'].includes(event.key)) {
      event.preventDefault()
      zoomAt(event.key === '-' ? 1 / 1.2 : 1.2, { x: view.width / 2, y: view.height / 2 })
    }
  }
  const miniCamera = fitCameraToBounds(allBounds(state.positions), MINI, 8)
  const topLeft = worldToScreen(screenToWorld({ x: 0, y: 0 }, state.camera), miniCamera)
  return (
    <div ref={root} className="ground-world" tabIndex={0} role="group" aria-label="Spatial project board. Drag to pan, scroll to zoom, 0 to fit all."
      data-focus-card={focusCard || undefined} data-reduced={reduced ? 'true' : 'false'}
      data-camera={JSON.stringify(state.camera)} onKeyDown={keyDown} onBlur={() => persist()}
      onPointerDown={(event) => start(event)} onPointerMove={move} onPointerUp={(event) => end(event)}
      onPointerCancel={(event) => end(event, true)} onLostPointerCapture={(event) => end(event, true)}>
      {children((name, tile) => {
        const screen = worldToScreen(state.positions[name], state.camera)
        return <div key={name} className="ground-world-card" data-card={name} tabIndex={0}
          aria-label={`${name} card. Enter to focus.`}
          style={{ ...CARD_SIZE, transform: `translate(${screen.x}px, ${screen.y}px) scale(${state.camera.zoom})` }}
          onPointerDown={(event) => start(event, name)} onDoubleClick={() => focus(name)}
          onKeyDown={(event) => { if (event.key === 'Enter' && event.target === event.currentTarget) { event.preventDefault(); event.stopPropagation(); focus(name) } }}>
          {tile}
        </div>
      })}
      <div className="ground-world-minimap" data-minimap style={MINI} role="img" aria-label="Board overview"
        onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect()
          const point = screenToWorld({ x: (event.clientX - rect.left) * MINI.width / (rect.width || MINI.width), y: (event.clientY - rect.top) * MINI.height / (rect.height || MINI.height) }, miniCamera)
          const next = current.current
          update({ ...next, camera: { ...next.camera, x: view.width / 2 - point.x * next.camera.zoom, y: view.height / 2 - point.y * next.camera.zoom } }, true)
        }}>
        {CARD_NAMES.map((name) => {
          const point = worldToScreen(state.positions[name], miniCamera)
          return <span key={name} data-minimap-card={name} className="ground-world-marker" style={{ left: point.x, top: point.y, width: CARD_SIZE.width * miniCamera.zoom, height: CARD_SIZE.height * miniCamera.zoom }} />
        })}
        <span data-minimap-viewport className="ground-world-viewport" style={{ left: topLeft.x, top: topLeft.y, width: view.width / state.camera.zoom * miniCamera.zoom, height: view.height / state.camera.zoom * miniCamera.zoom }} />
      </div>
    </div>
  )
}
