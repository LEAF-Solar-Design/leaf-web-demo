import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import LabWorkbench from './LabWorkbench.jsx'
import { isLabEmbedded, parseLabRoute } from './labRoute.js'
import { useLabSurfaceStatus } from './useLabSurfaceStatus.js'
import { NO_OCCLUDERS, useGroundWindow, windowStyle } from '../site/groundWindow.js'

const loadLabHostBridge = () => import('./embed/hostBridge.js')
let hostBridgeLoader = loadLabHostBridge

// Inject before mounting, as with the graph canvas loader.
export function setLabHostBridgeLoader(loader = loadLabHostBridge) {
  hostBridgeLoader = loader
}

export default function LabGround({ active, mock = false, studioShell = false, occluders = NO_OCCLUDERS }) {
  const ref = useRef(null)
  const [hostBridge, setHostBridge] = useState(null)
  const route = typeof window === 'undefined' ? null : parseLabRoute(window.location.pathname, window.location.search)
  const embedded = isLabEmbedded(route)
  // Lab has no product frame to measure; use the ground host and occluders.
  const win = useGroundWindow(active && !embedded, true, ref, occluders)
  const status = useLabSurfaceStatus(mock)
  useEffect(() => {
    if (!embedded || !active || status?.state !== 'on') return undefined
    let current = true
    hostBridgeLoader().then((module) => { if (current) setHostBridge(module) }).catch(() => {})
    return () => { current = false }
  }, [embedded, active, status?.state])
  const content = <section ref={ref} data-ground="lab" data-leaf-theme="dark" data-lab-embed-root={embedded ? '' : undefined} hidden={!active} aria-label="Lab workspace"
    style={{ position: embedded ? 'fixed' : 'absolute', inset: 0, minWidth: 0, overflow: 'hidden', pointerEvents: 'none' }}
    data-studio-shell={studioShell ? 'cockpit' : undefined}>
    {embedded && active && <style>{'body > :not([data-lab-embed-root]) { display: none !important; }'}</style>}
    <div data-measured={Boolean(win)} style={{ position: 'absolute', overflow: 'auto', pointerEvents: 'auto',
      ...(embedded ? { inset: 0 } : win ? windowStyle(win) : { visibility: 'hidden' }) }}>
      <LabWorkbench route={route} hidden={!active} status={status} embedded={embedded} createHostBridge={hostBridge?.createLabHostBridge} />
    </div>
  </section>
  // The frame owns its whole viewport. Portaling also keeps dormant/sign-in
  // states visible without retaining the console's header, rails or drawers.
  return embedded ? createPortal(content, document.body) : content
}
