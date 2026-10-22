import { lazy, Suspense, useEffect, useState } from 'react'

const LabCanvas = lazy(() => import('./LabCanvas.jsx'))

export default function GraphView({ hidden = false }) {
  const [opened, setOpened] = useState(!hidden)
  useEffect(() => { if (!hidden) setOpened(true) }, [hidden])
  return (
    <div className="lab-graph-view" hidden={hidden}>
      {opened && <Suspense fallback={<p role="status">Loading dependency canvas…</p>}><LabCanvas /></Suspense>}
    </div>
  )
}
