import { useRef } from 'react'
import LabWorkbench from './LabWorkbench.jsx'
import { parseLabRoute } from './labRoute.js'
import { useLabSurfaceStatus } from './useLabSurfaceStatus.js'
import { NO_OCCLUDERS, useGroundWindow, windowStyle } from '../site/groundWindow.js'

export default function LabGround({ active, mock = false, studioShell = false, occluders = NO_OCCLUDERS }) {
  const ref = useRef(null)
  // Lab has no product frame to measure; use the ground host and occluders.
  const win = useGroundWindow(active, true, ref, occluders)
  const status = useLabSurfaceStatus(mock)
  const route = typeof window === 'undefined' ? null : parseLabRoute(window.location.pathname, window.location.search)
  return <section ref={ref} data-ground="lab" hidden={!active} aria-label="Lab workspace"
    style={{ position: 'absolute', inset: 0, minWidth: 0, overflow: 'hidden', pointerEvents: 'none' }}
    data-studio-shell={studioShell ? 'cockpit' : undefined}>
    <div data-measured={Boolean(win)} style={{ position: 'absolute', overflow: 'auto', pointerEvents: 'auto',
      ...(win ? windowStyle(win) : { visibility: 'hidden' }) }}>
      <LabWorkbench route={route} hidden={!active} status={status} />
    </div>
  </section>
}
