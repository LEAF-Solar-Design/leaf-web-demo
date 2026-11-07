import { useEffect, useState } from 'react'

export default function GroundSkeleton({ label, delayMs = 200 }) {
  const [visible, setVisible] = useState(false)
  useEffect(() => {
    const timer = setTimeout(() => setVisible(true), delayMs)
    return () => clearTimeout(timer)
  }, [delayMs])

  if (!visible) return null
  return <div className="ground-skeleton" role="status" aria-live="polite" data-dp-case="ground-skeleton">
    <span style={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden', clip: 'rect(0 0 0 0)' }}>Loading {label}</span>
    <div className="skeleton-row" aria-hidden="true" />
    <div className="skeleton-row" aria-hidden="true" />
    <div className="skeleton-row" aria-hidden="true" />
  </div>
}
