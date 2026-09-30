import { useEffect, useId, useRef, useState } from 'react'
import './drawingCameraControls.css'

const DRAWING_CAMERA_REASONS = Object.freeze({
  viewport: 'Pan is unavailable until the drawing viewer has a visible viewport.',
})
const DIRECTIONS = [['North', 0, 1], ['South', 0, -1], ['East', 1, 0], ['West', -1, 0]]
function usable(snapshot) {
  const p = snapshot?.pose, b = snapshot?.viewport
  return !!p && !!b && [p.target?.[0], p.target?.[1], p.target?.[2], p.zoom, b.minX, b.maxX, b.minY, b.maxY].every(Number.isFinite)
    && p.zoom > 0 && b.maxX > b.minX && b.maxY > b.minY
    && Number.isFinite(b.maxX - b.minX) && Number.isFinite(b.maxY - b.minY)
}
const coordinate = (n) => n.toFixed(2).replace(/^-0\.00$/, '0.00')

export default function DrawingCameraControls({ viewerRef, announce }) {
  const subscription = useRef(null)
  const camera = useRef(null)
  const [ready, setReady] = useState(false)
  const reasonId = useId()
  // App rerenders after delayed Viewer attachment. Snapshots update readiness,
  // but only a different imperative handle replaces the subscription.
  useEffect(() => {
    const api = viewerRef?.current
    if (subscription.current?.api === api) return
    subscription.current?.off?.()
    camera.current = null
    setReady(false)
    const owner = { api, off: null }
    subscription.current = owner
    owner.off = api?.subscribeCamera?.((snapshot) => {
      if (subscription.current !== owner) return
      camera.current = usable(snapshot) ? snapshot : null
      setReady(!!camera.current && typeof api.setView === 'function')
    })
  })
  useEffect(() => () => {
    const previous = subscription.current
    subscription.current = null
    previous?.off?.()
    camera.current = null
  }, [])

  const pan = (dx, dy) => {
    const api = subscription.current?.api, snapshot = camera.current
    if (api !== viewerRef?.current || !usable(snapshot) || typeof api?.setView !== 'function') return
    const { pose, viewport } = snapshot
    const center = {
      x: pose.target[0] + dx * (viewport.maxX - viewport.minX) / 2,
      y: pose.target[1] + dy * (viewport.maxY - viewport.minY) / 2,
    }
    if (!Number.isFinite(center.x) || !Number.isFinite(center.y)) return
    if (api.setView({ center }) !== true) return
    // Camera publication is frame-coalesced. Accumulate rapid key presses
    // locally until the next snapshot arrives, without overwriting a sync one.
    if (camera.current === snapshot) camera.current = { ...snapshot, pose: { ...pose, target: [center.x, center.y, pose.target[2]] } }
    announce?.(`View centre ${coordinate(center.x)}, ${coordinate(center.y)} drawing units`)
  }
  return (
    <div className="drawing-camera-controls" role="group" aria-label="Pan drawing">
      <div className="drawing-camera-directions">
        {DIRECTIONS.map(([name, dx, dy]) => (
          <button key={name} type="button" aria-disabled={!ready || undefined}
            aria-describedby={!ready ? reasonId : undefined} onClick={() => pan(dx, dy)}>{name}</button>
        ))}
      </div>
      {!ready && <p id={reasonId}>{DRAWING_CAMERA_REASONS.viewport}</p>}
    </div>
  )
}
