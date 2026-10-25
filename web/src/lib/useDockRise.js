import { useEffect } from 'react'

export const TABLET_DRAWER_QUERY = '(min-width: 601px) and (max-width: 1023px) and (min-height: 500px)'
export const DOCK_RISE_VAR = '--studio-dock-rise'

// Publishes the command dock's rise (viewport bottom to the dock's top edge) as --studio-dock-rise on the app, on the tablet drawer frame only; fails closed (removes the property) anywhere else; one pending animation frame at most.
export default function useDockRise(appRef, enabled) {
  useEffect(() => {
    if (!enabled || !appRef.current) return undefined
    const app = appRef.current
    let mq
    try {
      mq = window.matchMedia(TABLET_DRAWER_QUERY)
    } catch {
      return undefined
    }

    let frame = null
    let observedDock = null
    let lastValue = null
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)

    function publish() {
      const root = appRef.current
      if (!root) return
      if (!mq.matches) {
        root.style.removeProperty(DOCK_RISE_VAR)
        lastValue = null
        return
      }
      const dock = root.querySelector(':scope > .center-col .bar-dock, .bar-dock')
      if (dock !== observedDock) {
        observer?.disconnect()
        observedDock = dock
        if (dock) observer?.observe(dock)
      }
      if (!dock) {
        root.style.removeProperty(DOCK_RISE_VAR)
        lastValue = null
        return
      }
      const value = `${Math.max(0, Math.min(window.innerHeight, Math.ceil(window.innerHeight - dock.getBoundingClientRect().top)))}px`
      if (value !== lastValue) {
        root.style.setProperty(DOCK_RISE_VAR, value)
        lastValue = value
      }
    }

    function schedule() {
      if (frame !== null) return
      const pending = { id: null }
      frame = pending
      pending.id = requestAnimationFrame(() => {
        frame = null
        publish()
      })
    }

    mq.addEventListener('change', schedule)
    window.addEventListener('resize', schedule)
    schedule()
    return () => {
      mq.removeEventListener('change', schedule)
      window.removeEventListener('resize', schedule)
      observer?.disconnect()
      if (frame !== null) cancelAnimationFrame(frame.id)
      app.style.removeProperty(DOCK_RISE_VAR)
    }
  }, [appRef, enabled])
}
