import { lazy, Suspense, useState } from 'react'

const LabCanvas = lazy(() => import('./LabCanvas.jsx'))

export default function GraphView({ hidden = false, initialDocument, entries, onRun }) {
  const [opened, setOpened] = useState(!hidden)
  const [graph, setGraph] = useState(null)
  const validDocument = graph?.ready ? graph.document : null
  // Remember the first reveal without an effect-triggered render. Keep the editor
  // mounted afterwards so navigating between views preserves the draft.
  if (!hidden && !opened) setOpened(true)
  return (
    <div className="lab-graph-view" hidden={hidden}>
      {opened && <>
        <section className="lab-graph-status" aria-label="Graph readiness">
          <p role="status">{validDocument ? 'Ready' : 'No valid graph'}</p>
          {graph?.problems.length > 0 && <ul>{graph.problems.map((problem, index) => <li key={index}>{problem}</li>)}</ul>}
          <button type="button" disabled={!validDocument || !onRun}
            title={onRun ? undefined : 'Execution is unavailable here.'}
            onClick={() => { if (validDocument) onRun?.(validDocument) }}>Run graph</button>
        </section>
        <Suspense fallback={<p role="status" aria-label="Loading dependency canvas">Loading dependency canvas…</p>}>
          <LabCanvas initialDocument={initialDocument} entries={entries} onChange={setGraph} />
        </Suspense>
      </>}
    </div>
  )
}
