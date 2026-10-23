import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { fetchLabStatus, formatLabRoute } from './labRoute.js'
import { registerLabViews } from './views.js'
import './lab.css'

const dormantMessages = {
  off: 'Lab is turned off.',
  'auth-off': 'Lab sign-in service is unavailable.',
  'signed-out': 'Sign in to use Lab.',
  unavailable: 'Lab is unavailable right now.',
}

const EmbeddedCanvas = lazy(() => import('./canvas/LabCanvas.jsx'))

function EmbeddedGraph({ hidden, enabled, onReload, createHostBridge }) {
  const bridge = useRef(null)
  const root = useRef(null)
  const restoration = useRef(null)
  const initialReport = useRef(true)
  const actionAt = useRef(null)
  const [opened, setOpened] = useState(!hidden)
  const [graph, setGraph] = useState(null)
  const [restored, setRestored] = useState(null)
  const [rows, setRows] = useState(null)
  const [sourcePick, setSourcePick] = useState('')
  const [inspector, setInspector] = useState(null)
  const [admitted, setAdmitted] = useState(false)
  const [hostProposal, setHostProposal] = useState(null)
  const [reloaded, setReloaded] = useState(false)
  if (!hidden && !opened) setOpened(true)

  useEffect(() => {
    if (!enabled || !createHostBridge) return undefined
    const connection = createHostBridge({
      onAdmitted: () => setAdmitted(true),
      onChange: ({ ready, problems }) => setHostProposal({ ready, problems }),
      onSource: (next) => { setRows(next); setSourcePick('host') },
      onReload: () => { setReloaded(true); setAdmitted(false); onReload(true) },
      onRestore: (draft) => new Promise((resolve) => {
        restoration.current = resolve
        initialReport.current = true
        setOpened(true)
        setRestored((previous) => ({ document: draft.document, revision: (previous?.revision || 0) + 1 }))
      }),
    })
    bridge.current = connection
    void connection.start()
    return () => {
      connection.stop()
      if (bridge.current === connection) bridge.current = null
      restoration.current?.()
      restoration.current = null
      setAdmitted(false)
      setHostProposal(null)
      setRows(null)
      setSourcePick('')
    }
  }, [enabled, onReload, createHostBridge])

  const changed = useCallback((draft) => {
    setGraph(draft)
    if (initialReport.current) {
      initialReport.current = false
      restoration.current?.()
      restoration.current = null
      return
    }
    const triggeredAt = actionAt.current ?? performance.now()
    void bridge.current?.proposal(draft, triggeredAt).catch(() => {})
  }, [])

  useLayoutEffect(() => {
    setInspector(root.current?.querySelector('.lab-inspector') || null)
  }, [graph])

  return <div ref={root} className="lab-graph-view" hidden={hidden}
    onPointerDownCapture={() => { actionAt.current = performance.now() }}
    onKeyDownCapture={() => { actionAt.current = performance.now() }}
    onChangeCapture={() => { actionAt.current = performance.now() }}>
    {opened ? <>
      <section className="lab-graph-status" aria-label="Graph readiness">
        <p role="status">{graph?.ready ? 'Ready' : 'No valid graph'}</p>
        {graph?.problems.length > 0 && <ul>{graph.problems.map((problem, index) => <li key={index}>{problem}</li>)}</ul>}
        {hostProposal && <p role="status" aria-label="Host proposal">{hostProposal.ready ? 'Proposal accepted by the host' : hostProposal.problems[0]}</p>}
        <button type="button" disabled={!enabled || !admitted || !graph?.ready || reloaded || hostProposal?.ready === false || bridge.current?.state?.status === 'too_large'}
          onClick={() => {
            const triggeredAt = performance.now()
            // Publish the current draft before asking the host to sign it.
            void bridge.current?.proposal(graph, triggeredAt).then(() => bridge.current?.run(triggeredAt)).catch(() => {})
          }}>Run graph</button>
      </section>
      <Suspense fallback={<p role="status" aria-label="Loading dependency canvas">Loading dependency canvas…</p>}>
        <EmbeddedCanvas key={restored?.revision || 0} initialDocument={restored?.document} onChange={changed} />
      </Suspense>
      {inspector && createPortal(<section aria-label="Host source">
        <label>Host source<select value={sourcePick} onChange={(event) => setSourcePick(event.target.value)}>
          <option value="">Choose source</option>
          {rows !== null && <option value="host">Dashboard</option>}
        </select></label>
        {sourcePick === 'host' && rows !== null && <pre aria-label="Host source rows">{JSON.stringify(rows, null, 2)}</pre>}
      </section>, inspector)}
    </> : <p data-graph-placeholder>Open Strategy to load the dependency canvas.</p>}
  </div>
}

export function LabWorkbench({ route, hidden = false, status, embedded = false, createHostBridge }) {
  const views = registerLabViews()
  const [liveStatus, setLiveStatus] = useState(null)
  const [hostReloaded, setHostReloaded] = useState(false)
  useEffect(() => {
    if (status !== undefined) return undefined
    let current = true
    fetchLabStatus().then((next) => { if (current) setLiveStatus(next) })
    return () => { current = false }
  }, [status])
  const state = (status === undefined ? liveStatus : status)?.state || 'unavailable'
  const available = state === 'on'
  const activeView = views.some(({ id }) => id === route?.view) ? route.view : 'graph'

  return (
    <section className="lab-workbench" aria-label="Lab" hidden={hidden}>
      <h1>Lab</h1>
      {embedded && hostReloaded && <p role="status">Studio Lab reloaded by the dashboard</p>}
      {!available && <p role="status">{dormantMessages[state] || dormantMessages.unavailable}</p>}
      <nav aria-label="Lab views" hidden={!available}>
        {views.map(({ id, label, path }) => (
          <a key={id} href={embedded ? formatLabRoute({ view: id, params: { ...route?.params, embed: '1' } }) : path}
            aria-current={activeView === id ? 'page' : undefined}>{label}</a>
        ))}
      </nav>
      {views.map(({ id, label, component: View }) => {
        const inactive = hidden || !available || activeView !== id
        return (
          <div key={id} data-lab-view={id} role="region" aria-label={`${label} view`} hidden={inactive}>
            {embedded && id === 'graph' ? <EmbeddedGraph hidden={inactive} enabled={available && !hidden} onReload={setHostReloaded} createHostBridge={createHostBridge} /> : <View hidden={inactive} />}
          </div>
        )
      })}
    </section>
  )
}

export default LabWorkbench
