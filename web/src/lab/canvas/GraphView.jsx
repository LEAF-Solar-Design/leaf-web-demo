import { lazy, Suspense, useState } from 'react'

const LabCanvas = lazy(() => import('./LabCanvas.jsx'))

export default function GraphView({ hidden = false }) {
  const [opened, setOpened] = useState(!hidden)
  // Remember the first reveal without an effect-triggered render. Keep the editor
  // mounted afterwards so navigating between views preserves the draft.
  if (!hidden && !opened) setOpened(true)
  return (
    <div className="lab-graph-view" hidden={hidden}>
      {opened && <Suspense fallback={<p role="status" aria-label="Loading dependency canvas">Loading dependency canvas…</p>}><LabCanvas /></Suspense>}
    </div>
  )
}
