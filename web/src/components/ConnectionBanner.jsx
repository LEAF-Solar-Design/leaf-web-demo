import { useLayoutEffect, useRef } from 'react'
import useOnline from '../lib/useOnline.js'

// NT2: an ongoing condition clears itself. The frame supplies the shell's
// header selector because its slots live at different depths in each scene.
export default function ConnectionBanner({ headerSelector = null, cast }) {
  const online = useOnline()
  const bannerRef = useRef(null)

  useLayoutEffect(() => {
    if (online || !headerSelector) return undefined
    const header = document.querySelector(headerSelector)
    const banner = bannerRef.current
    if (!header || !banner) return undefined
    const dock = () => {
      const rect = header.getBoundingClientRect()
      banner.style.top = `${rect.bottom}px`
      banner.style.left = `${rect.left}px`
      banner.style.right = `${window.innerWidth - rect.right}px`
    }
    dock()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(dock)
    observer?.observe(header)
    window.addEventListener('resize', dock)
    window.addEventListener('scroll', dock, true)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', dock)
      window.removeEventListener('scroll', dock, true)
    }
  }, [online, headerSelector])

  if (online) return null
  return (
    <div
      ref={bannerRef}
      className="connection-banner"
      data-testid="connection-banner"
      data-cast={cast}
      role="status"
      style={{
        position: headerSelector ? 'fixed' : undefined,
        top: 0, left: 0, right: 0, zIndex: 35,
        display: 'flex', alignItems: 'center', gap: 9,
        margin: 0, padding: '8px 16px', borderBottom: '1px solid var(--border)',
        background: 'color-mix(in oklab, var(--status-warning) 7%, var(--sheet))',
        color: 'var(--muted)', fontSize: '12.5px',
        transition: 'none', transform: 'none', filter: 'none',
      }}
    >
      <span className="dot square" aria-hidden="true" style={{ width: 6, height: 6, borderRadius: '1.5px', background: 'var(--status-warning)', flex: '0 0 auto' }} />
      <span><b style={{ color: 'var(--status-warning)', fontWeight: 500 }}>Offline.</b> Leaf Automation reconnects on its own.</span>
    </div>
  )
}
