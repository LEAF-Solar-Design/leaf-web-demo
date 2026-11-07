import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { LiveValues, useLiveGraph } from './canvas/GraphView.jsx'
import { createHostProvider } from './host/hostProvider.js'
import { guardSink } from './host/hostResidency.js'
import { canonicalJSON } from './contract.js'
import { fetchLabStatus, formatLabRoute, parseLabRoute } from './labRoute.js'
import { registerLabViews } from './views.js'
import { login, loginWithPopupFromClick, preloadAuthClient, subscribeTokenStored } from '../auth.js'
import './lab.css'

const dormantMessages = {
  off: 'Lab is turned off.',
  'auth-off': 'Lab sign-in service is unavailable.',
  'signed-out': 'Sign in to use Lab.',
  unavailable: 'Lab is unavailable right now.',
}

const EmbeddedCanvas = lazy(() => import('./canvas/LabCanvas.jsx'))

function useHostSource({ enabled, createHostBridge }, onChange) {
  const [provider, setProvider] = useState(null)
  useEffect(() => {
    if (!enabled || !createHostBridge) return undefined
    const sourceProvider = createHostProvider({ framed: true, onChange })
    setProvider(sourceProvider)
    sourceProvider.start()
    const connection = createHostBridge({ onSource: next => sourceProvider.announce(next) })
    void connection.start()
    return () => {
      connection.stop()
      sourceProvider.stop()
    }
  }, [enabled, createHostBridge, onChange])
  return enabled && createHostBridge ? provider : null
}

function EmbeddedGraph({ hidden, enabled, onReload, createHostBridge, onHostSource, onBridge, route }) {
  const bridge = useRef(null)
  const root = useRef(null)
  const restoration = useRef(null)
  const initialReport = useRef(true)
  const initialProposalPending = useRef(true)
  const latestDraft = useRef(null)
  const lastProposalDocument = useRef(null)
  const admittedRef = useRef(false)
  const actionAt = useRef(null)
  const [opened, setOpened] = useState(!hidden)
  const [revision, setRevision] = useState(0)
  const sourceChanged = useCallback(() => setRevision(value => value + 1), [])
  const [emptyProvider] = useState(() => createHostProvider({ framed: true }))
  const retainedProvider = useRef(emptyProvider)
  const connect = useCallback(({ onSource }) => {
    const connection = createHostBridge({
      onAdmitted: () => { admittedRef.current = true; setAdmitted(true) },
      onChange: ({ ready, problems }) => setHostProposal({ ready, problems }),
      onSource,
      onReload: () => { setReloaded(true); admittedRef.current = false; setAdmitted(false); onReload(true) },
      onRestore: (draft) => new Promise((resolve) => {
        restoration.current = resolve
        initialReport.current = true
        initialProposalPending.current = false
        latestDraft.current = null
        setOpened(true)
        live.onChange(null)
        setRestored((previous) => ({ document: draft.document, revision: (previous?.revision || 0) + 1 }))
      }),
    })
    bridge.current = connection
    lastProposalDocument.current = null
    onBridge(connection)
    return {
      start: () => connection.start(),
      stop: () => {
        connection.stop()
        if (bridge.current === connection) bridge.current = null
        onBridge(null)
        restoration.current?.()
        restoration.current = null
        admittedRef.current = false
        setAdmitted(false)
        setHostProposal(null)
      },
    }
  }, [onReload, createHostBridge, onBridge])
  const source = useHostSource({ enabled, createHostBridge: createHostBridge ? connect : null }, sourceChanged)
  if (source) retainedProvider.current = source
  const provider = source || retainedProvider.current
  const live = useLiveGraph({ provider, revision })
  const graph = live.draft
  const [restored, setRestored] = useState(null)
  const [admitted, setAdmitted] = useState(false)
  const [hostProposal, setHostProposal] = useState(null)
  const [reloaded, setReloaded] = useState(false)
  if (!hidden && !opened) setOpened(true)

  useEffect(() => {
    // Value-only announcements keep the provider identity but must refresh widgets.
    onHostSource({ provider: source, revision })
  }, [source, revision, onHostSource])

  const propose = useCallback((draft, triggeredAt = actionAt.current ?? performance.now()) => {
    try {
      return guardSink('proposal', next => {
        const pending = bridge.current?.proposal(next, triggeredAt)
        if (admittedRef.current) lastProposalDocument.current = canonicalJSON(next.document)
        return pending
      })(draft)
    } catch {
      setHostProposal({ ready: false, problems: ['Proposal refused: host data cannot leave the runtime'] })
      return Promise.resolve(false)
    }
  }, [])

  const changed = useCallback((draft) => {
    latestDraft.current = draft
    live.onChange(draft)
    if (initialReport.current) {
      initialReport.current = false
      // A host restore is already known to the host; its report is not an edit.
      if (restoration.current) lastProposalDocument.current = canonicalJSON(draft.document)
      restoration.current?.()
      restoration.current = null
      return
    }
    void propose(draft)?.catch(() => {})
  }, [live.onChange, propose])

  useEffect(() => {
    // Only the automatic initial proposal waits for readiness and admission.
    if (initialProposalPending.current && admitted && live.ready && graph && latestDraft.current === graph) {
      initialProposalPending.current = false
      if (lastProposalDocument.current !== canonicalJSON(graph.document)) {
        void propose(latestDraft.current)?.catch(() => {})
      }
    }
  }, [admitted, live.ready, graph, propose])

  return <div ref={root} className="lab-graph-view" hidden={hidden}
    onPointerDownCapture={() => { actionAt.current = performance.now() }}
    onKeyDownCapture={() => { actionAt.current = performance.now() }}
    onChangeCapture={() => { actionAt.current = performance.now() }}>
    {opened ? <>
      <section className="lab-graph-status" aria-label="Graph readiness">
        <p role="status">{live.ready ? 'Ready' : 'No valid graph'}</p>
        {live.problems.length > 0 && <ul>{live.problems.map((problem, index) => <li key={index}>{problem}</li>)}</ul>}
        {hostProposal && <p role="status" aria-label="Host proposal">{hostProposal.ready ? 'Proposal accepted by the host' : hostProposal.problems[0]}</p>}
        <button type="button" disabled={!enabled || !admitted || !live.ready || reloaded || hostProposal?.ready === false || bridge.current?.state?.status === 'too_large'}
          onClick={() => {
            const triggeredAt = performance.now()
            // Ensure the host has the current draft before asking it to sign.
            void propose(graph, triggeredAt)?.then(sent => sent !== false && bridge.current?.run(triggeredAt)).catch(() => {})
          }}>Run graph</button>
        <a href={formatLabRoute({ view: 'test-trade', params: { ...route?.params, embed: '1' } })}
          onClick={(event) => {
            if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
            event.preventDefault()
            window.history.pushState(null, '', event.currentTarget.href)
            window.dispatchEvent(new PopStateEvent('popstate'))
          }}>Test trade evidence</a>
      </section>
      <LiveValues values={live.values} provider={provider} />
      <Suspense fallback={<p role="status" aria-label="Loading dependency canvas">Loading dependency canvas…</p>}>
        <EmbeddedCanvas key={restored?.revision || 0} initialDocument={restored?.document} entries={live.entries}
          measurements={live.measurements} upstream={live.upstream} onChange={changed} />
      </Suspense>
    </> : <p data-graph-placeholder>Open Strategy to load the dependency canvas.</p>}
  </div>
}

export function LabWorkbench({ route, hidden = false, status, embedded = false, createHostBridge }) {
  const views = registerLabViews()
  const [liveStatus, setLiveStatus] = useState(null)
  const [signingIn, setSigningIn] = useState(false)
  const [signInMessage, setSignInMessage] = useState(null)
  const authPreloaded = useRef(false)
  const [hostReloaded, setHostReloaded] = useState(false)
  const [hostSource, setHostSource] = useState(null)
  const [bridge, setBridge] = useState(null)
  const [viewRoute, setViewRoute] = useState(route)
  useEffect(() => { setViewRoute(route) }, [route])
  useEffect(() => {
    const navigate = () => setViewRoute(parseLabRoute(window.location.pathname, window.location.search))
    window.addEventListener('popstate', navigate)
    return () => window.removeEventListener('popstate', navigate)
  }, [])
  useEffect(() => {
    if (status !== undefined) return undefined
    let current = true
    let generation = 0
    const refresh = () => {
      const probe = ++generation
      fetchLabStatus().then((next) => { if (current && probe === generation) setLiveStatus(next) })
    }
    const unsubscribe = subscribeTokenStored(refresh)
    refresh()
    return () => { current = false; unsubscribe() }
  }, [status])
  const state = (status === undefined ? liveStatus : status)?.state || 'unavailable'
  useEffect(() => {
    if (embedded && state === 'signed-out' && !authPreloaded.current) {
      authPreloaded.current = true
      preloadAuthClient()
    }
  }, [embedded, state])
  const signIn = () => {
    if (!embedded) { void login(); return }
    const pending = loginWithPopupFromClick()
    setSigningIn(true)
    setSignInMessage(null)
    void pending.then((result) => {
      setSigningIn(false)
      if (!result.ok) setSignInMessage(result.reason === 'cancelled'
        ? 'Sign-in was cancelled.'
        : 'The sign-in window was blocked or could not finish. Allow pop-ups for this site, then try again.')
    })
  }
  const available = state === 'on'
  const activeView = views.some(({ id }) => id === viewRoute?.view) ? viewRoute.view : 'graph'

  return (
    <section className="lab-workbench" aria-label="Lab" hidden={hidden}>
      <h1>Lab</h1>
      {embedded && hostReloaded && <p role="status">Studio Lab reloaded by the dashboard</p>}
      {!available && <p role="status">{dormantMessages[state] || dormantMessages.unavailable}</p>}
      {state === 'signed-out' && <>
        <button type="button" disabled={signingIn} onClick={signIn}>{signingIn ? 'Signing in' : 'Sign in'}</button>
        {signInMessage && <p role="status">{signInMessage}</p>}
      </>}
      <nav aria-label="Lab views" hidden={!available}>
        {views.map(({ id, label, path }) => (
          <a key={id} href={embedded ? formatLabRoute({ view: id, params: { ...route?.params, embed: '1' } }) : path}
            onClick={(event) => {
              if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
              event.preventDefault()
              window.history.pushState(null, '', event.currentTarget.href)
              setViewRoute({ ...viewRoute, view: id })
            }}
            aria-current={activeView === id ? 'page' : undefined}>{label}</a>
        ))}
      </nav>
      {views.map(({ id, label, component: View }) => {
        const inactive = hidden || !available || activeView !== id
        return (
          <div key={id} data-lab-view={id} role="region" aria-label={`${label} view`} hidden={inactive}>
            {embedded && id === 'graph' ? <EmbeddedGraph hidden={inactive} enabled={available && !hidden} onReload={setHostReloaded} createHostBridge={createHostBridge} onHostSource={setHostSource} onBridge={setBridge} route={viewRoute} />
              : id === 'widgets' ? <View hidden={inactive} host={embedded && available && !hidden && createHostBridge ? hostSource?.provider ?? null : null} requestPublication={embedded && bridge ? (request) => bridge.publishWidget(request) : undefined} />
                : id === 'test-trade' ? <View hidden={inactive} provider={embedded && available && !hidden && createHostBridge ? hostSource?.provider ?? null : null} revision={hostSource?.revision} requestTestTrade={embedded && bridge ? () => bridge.requestTestTrade() : undefined} />
                  : id === 'graph' ? <View hidden={inactive} route={viewRoute} /> : <View hidden={inactive} />}
          </div>
        )
      })}
    </section>
  )
}

export default LabWorkbench
