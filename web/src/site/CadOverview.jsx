import { useEffect, useMemo, useRef, useState } from 'react'
import { useDrawingObjects } from './DrawingObjectsContext.jsx'
import { validObjectBounds } from '../lib/drawingObjectIndex.js'
import { aggregateOverview, fromOverviewPoint, overviewBounds, overviewMap, overviewRect } from './cadOverviewMath.js'
import './cadOverview.css'

const WIDTH = 180, HEIGHT = 112
const STORAGE_KEY = 'leaf.cad-overview.collapsed'
const sameRect = (a, b) => a === b || (!!a && !!b && ['x', 'y', 'width', 'height'].every((key) => a[key] === b[key]))
const matchesScene = (api, index) => {
  const scene = api?.getDrawingScene?.()
  return !!index && !!scene?.ready && scene.drawingKey === index.drawingKey && scene.intake === index.intake
}
function initiallyCollapsed() {
  try {
    const saved = sessionStorage.getItem(STORAGE_KEY)
    if (saved === 'true' || saved === 'false') return saved === 'true'
  } catch { /* Storage may be unavailable in a private or embedded session. */ }
  return typeof window !== 'undefined' && window.innerWidth <= 980
}

export default function CadOverview({ viewerRef }) {
  const { index, focusId, selectedHandles = [] } = useDrawingObjects() || {}
  const map = useMemo(() => overviewMap(overviewBounds(index?.records), WIDTH, HEIGHT), [index])
  const context = useMemo(() => aggregateOverview(index?.records, map), [index, map])
  const selected = useMemo(() => [...new Set(selectedHandles.map((handle) =>
    index?.byHandle.get(String(handle).replace(/^0x/i, '').toUpperCase())).filter(Boolean))], [index, selectedHandles])
  const selection = useMemo(() => aggregateOverview(selected, map), [selected, map])
  const focusBounds = index?.byId.get(focusId)?.bounds
  const focusRect = overviewRect(map, focusBounds, 6)
  const [collapsed, setCollapsed] = useState(initiallyCollapsed)
  const [camera, setCamera] = useState({ api: null, ready: false, rect: null })
  const rendered = useRef(camera), latest = useRef(null), subscribed = useRef(null)
  const current = useRef(null), suppressEnterClick = useRef(false), control = useRef(null)
  current.current = { index, map }

  // Ref attachment is notified by App's render. Check after EVERY render,
  // including delayed attachment and replacements on the same ref object.
  useEffect(() => {
    const api = viewerRef?.current
    const update = () => {
      const now = current.current
      const raw = overviewRect(now.map, latest.current?.viewport)
      const rect = raw && Object.fromEntries(Object.entries(raw).map(([key, value]) => [key, Math.round(value * 10) / 10]))
      const next = { api, ready: matchesScene(api, now.index), rect }
      const previous = rendered.current
      if (previous.api !== api || previous.ready !== next.ready || !sameRect(previous.rect, rect)) {
        rendered.current = next
        setCamera(next)
      }
    }
    if (subscribed.current?.api !== api) {
      subscribed.current?.off?.()
      latest.current = null
      const entry = { api, off: null }
      subscribed.current = entry
      entry.off = api?.subscribeCamera?.((snapshot) => {
        if (subscribed.current !== entry) return
        latest.current = snapshot
        update()
      })
    }
    update()
  })
  useEffect(() => () => { subscribed.current?.off?.(); subscribed.current = null }, [])

  const recenter = (center) => {
    const api = viewerRef?.current
    if (!matchesScene(api, index)) {
      const next = { ...rendered.current, ready: false }
      rendered.current = next; setCamera(next)
      return
    }
    if (center && [center.x, center.y].every(Number.isFinite)) api?.setView?.({ center })
  }
  const centerFocus = () => {
    if (validObjectBounds(focusBounds)) recenter({ x: focusBounds.minX / 2 + focusBounds.maxX / 2,
      y: focusBounds.minY / 2 + focusBounds.maxY / 2 })
  }
  const keyDown = (event) => {
    const direction = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] }[event.key]
    if (!direction && event.key !== 'Enter') return
    event.preventDefault(); event.stopPropagation()
    if (event.key === 'Enter') {
      suppressEnterClick.current = true
      if (!event.repeat) centerFocus()
      return
    }
    const snapshot = subscribed.current?.api === viewerRef?.current ? latest.current : null
    const target = snapshot?.pose?.target, viewport = snapshot?.viewport
    if (!validObjectBounds(viewport) || ![target?.[0], target?.[1]].every(Number.isFinite)) return
    recenter({ x: target[0] + direction[0] * (viewport.maxX - viewport.minX) / 4,
      y: target[1] + direction[1] * (viewport.maxY - viewport.minY) / 4 })
  }
  const toggle = () => {
    const next = !collapsed
    try { sessionStorage.setItem(STORAGE_KEY, String(next)) } catch { /* Keep the control usable. */ }
    setCollapsed(next)
  }
  if (!map || camera.api !== viewerRef?.current || !camera.ready || !matchesScene(viewerRef?.current, index)) return null

  return <div className="cad-overview" data-cad-overview data-collapsed={collapsed}
    onClick={(event) => event.stopPropagation()} onPointerDown={(event) => event.stopPropagation()}>
    <button type="button" className="cad-overview-toggle" aria-expanded={!collapsed}
      aria-label={collapsed ? 'Expand drawing overview' : 'Collapse drawing overview'}
      onClick={toggle}>{collapsed ? 'Overview' : 'Collapse overview'}</button>
    {!collapsed && <>
      <button ref={control} type="button" className="cad-overview-map" aria-label="Drawing overview"
        title="Click to pan. Arrow keys pan; Enter centres on focus."
        onKeyDown={keyDown} onKeyUp={(event) => {
          if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); suppressEnterClick.current = false }
        }} onBlur={() => { suppressEnterClick.current = false }}
        onClick={(event) => {
          event.stopPropagation()
          control.current?.focus({ preventScroll: true })
          if (event.detail === 0) { if (!suppressEnterClick.current) centerFocus(); return }
          suppressEnterClick.current = false
          const box = event.currentTarget.querySelector('svg').getBoundingClientRect()
          if (box.width <= 0 || box.height <= 0) return
          recenter(fromOverviewPoint(map, { x: (event.clientX - box.left) * WIDTH / box.width,
            y: (event.clientY - box.top) * HEIGHT / box.height }))
        }}>
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} width={WIDTH} height={HEIGHT} aria-hidden="true">
          <rect className="cad-overview-outline" x={map.x} y={map.y} width={map.width} height={map.height} />
          {context.map(({ key, ...rect }) => <rect key={key} data-overview-context className="cad-overview-context" {...rect} />)}
          {camera.rect && <rect data-overview-viewport className="cad-overview-viewport" {...camera.rect} />}
          {selection.map(({ key, ...rect }) => <rect key={key} data-overview-selected className="cad-overview-selected" {...rect} />)}
          {focusRect && <g data-overview-focus className="cad-overview-focus">
            <rect {...focusRect} />
            <path d={`M${focusRect.x + focusRect.width / 2} ${focusRect.y + focusRect.height / 2 - 4} l4 4 -4 4 -4 -4 Z`} />
          </g>}
        </svg>
      </button>
      <div className="cad-overview-key"><span>{focusRect ? '◇ Focus' : 'No focus'}</span><span>□ {selected.length} selected</span></div>
    </>}
  </div>
}
