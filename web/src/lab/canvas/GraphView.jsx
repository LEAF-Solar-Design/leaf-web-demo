import { lazy, memo, Suspense, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { registry } from './registry.js'
import { pipeId, validateGraph } from './graphDoc.js'
import { authHeaders, config } from '../../api.js'
import { fetchWithBudget } from '../../fetchBudget.js'
import { createHostProvider } from '../host/hostProvider.js'
import { evidenceFor } from '../code/hostEvidence.js'
import { validateResidency } from '../host/hostResidency.js'
import { createStudioSources, evaluateGraph, runtimeValue, sourceAge } from '../runtime/index.js'
import LabSaveBar from '../history/LabSaveBar.jsx'
import { labArtifactsClient } from '../history/labArtifactsClient.js'
import AfterSavePanel from '../history/AfterSavePanel.jsx'
import { createAfterSaveClient } from '../history/afterSaveClient.js'
import { canonicalJSON } from '../contract.js'
import { labValueStore, terminalState, useBlockValue, useRun, useRunStatus } from '../runs/valueStore.js'
import { openRunStream, submitRun } from '../runs/runStream.js'

// Both mounts pass the values returned by useLiveGraph to LiveValues. Keep
// their display context out of the values and the proposal document itself.
const displayContexts = new WeakMap()

const wallTime = () => Date.now()
const tick = () => performance.now()
const summary = value => value.error ? `error: ${value.error.code}`
  : value.data === null ? 'null' : Array.isArray(value.data) ? `${value.data.length} rows`
    : typeof value.data === 'number' ? String(value.data) : 'text'
const seriesOf = data => Array.isArray(data) ? data.map(item => typeof item === 'number' ? item
  : item && typeof item === 'object' ? Object.values(item).find(Number.isFinite) : undefined) : []

export function useLiveGraph({ provider, registry: base = registry, now = wallTime, clock = tick, revision, artifacts = labArtifactsClient }) {
  const [draft, onChange] = useState(null)
  const [currentTime, setNow] = useState(() => now())
  const [evaluation, setEvaluation] = useState({ result: null, problems: [], count: 0, times: [] })
  const signature = JSON.stringify(provider.entries())
  // The provider is immutable; mounts forward its constructor onChange as a
  // revision so value-only updates also refresh the observed pins and results.
  const entries = useMemo(() => ({ ...base, ...provider.entries() }), [base, provider, signature, revision])
  const validation = useMemo(() => draft ? validateGraph(draft.document, entries) : null, [draft, entries])
  const residency = useMemo(() => draft ? validateResidency(draft.document, entries)
    : { ok: true, problems: [], tainted: [] }, [draft, entries])
  useEffect(() => {
    const publish = update => setEvaluation(previous => ({ ...update(previous), draft, entries }))
    if (!draft) {
      publish(previous => ({ ...previous, result: null, problems: [] }))
      return
    }
    try {
      if (!residency.ok) {
        publish(previous => ({ ...previous, result: null, problems: [] }))
        return
      }
      if (!validation.ready) {
        publish(previous => ({ ...previous, result: null, problems: [] }))
        return
      }
      const result = evaluateGraph(draft.document, entries, { sources: provider.sources(draft.document, entries), now: clock })
      publish(previous => ({ result, problems: [], count: previous.count + 1,
        times: [...previous.times, result.timing.duration_ms].slice(-20) }))
    } catch {
      publish(previous => ({ ...previous, result: null, problems: ['Graph evaluation failed'] }))
    }
  }, [draft, entries, validation, residency, provider, revision, clock])
  const { count, times } = evaluation
  const current = evaluation.draft === draft && evaluation.entries === entries
  const result = current ? evaluation.result : null
  const sorted = [...times].sort((a, b) => a - b)
  const percentile = rank => sorted.length ? sorted[Math.ceil(sorted.length * rank) - 1] * 1000 : 0
  const latency = { p50_us: percentile(.5), p99_us: percentile(.99) }
  const measurements = { blocks: {}, pipes: {} }
  const upstream = {}
  const values = []
  const card = (id, port, title, value) => {
    const series = seriesOf(value.data)
    const kind = value.error || value.data === null ? 'empty'
      : typeof value.data === 'string' || typeof value.data === 'boolean' || Number.isFinite(value.data) ? 'value'
        : Array.isArray(value.data) ? series.length > 0 && series.every(Number.isFinite) ? 'chart' : 'rows' : 'empty'
    const display = typeof value.data === 'boolean' ? String(value.data) : value.data
    return { id, port, title, kind, display: kind === 'value' ? display : summary(value), series,
      as_of: value.as_of, age_ms: sourceAge(value, Math.max(currentTime, now())), stale: value.stale, simulated: value.simulated,
      truncated: value.truncated, host_tainted: value.host_tainted, error: value.error, ...latency }
  }
  if (result) {
    for (const [id, outputs] of Object.entries(result.values)) {
      const block = draft.document.blocks.find(item => item.id === id)
      const title = entries[`${block.registry.id}@${block.registry.version}`]?.title || id
      for (const [port, value] of Object.entries(outputs)) {
        upstream[`${id}/${port}`] = value.data
        measurements.blocks[id] = { value: summary(value), count, ...latency }
        values.push(card(id, port, title, value))
      }
    }
    for (const pipe of draft.document.pipes) {
      const value = result.values[pipe.from.node]?.[pipe.from.port]
      if (value) measurements.pipes[pipeId(pipe)] = { value: summary(value), count, ...latency }
    }
    for (const [name, value] of Object.entries(result.outputs)) values.push(card(`output:${name}`, name, name, value))
  }
  const hasValues = values.length > 0
  useEffect(() => {
    labValueStore.setLocal(values, { times, count, measurements })
  }, [result, currentTime, count])
  useEffect(() => {
    if (!hasValues) return undefined
    const timer = setInterval(() => setNow(now()), 1000)
    return () => clearInterval(timer)
  }, [hasValues, now])
  const problems = [...(validation?.problems || []), ...residency.problems, ...(current ? evaluation.problems : [])]
  displayContexts.set(values, { document: draft?.document, ready: Boolean(validation?.ready && residency.ok && !problems.length), artifacts })
  return { entries, draft, result, problems, ready: Boolean(validation?.ready && residency.ok && !problems.length),
    measurements, upstream, badges: values, values, onChange }
}

export function LiveValues({ values, provider, saveBar = true, runId = 'local', store = labValueStore, renderProbe }) {
  const state = provider.state
  const context = displayContexts.get(values)
  return <>
    {context && <AfterSaveDisplay provider={provider} context={context} saveBar={saveBar} />}
    <p data-sources-state>{!state.framed ? 'Studio sources: jobs, capabilities'
      : state.reason === 'Sample' ? 'Dashboard sources: Sample (none announced within 2 seconds)'
        : `Dashboard sources: ${state.sources.join(', ')}`}</p>
    <section aria-label="Values">
      {[...new Set(values.map(value => value.id))].map(id => <StoredBlockValues key={id} id={id} runId={runId}
        store={store} templates={values.filter(value => value.id === id)} renderProbe={renderProbe} />)}
    </section>
  </>
}

const StoredBlockValues = memo(function StoredBlockValues({ id, runId, store, templates, renderProbe }) {
  renderProbe?.('block:' + id)
  const block = useBlockValue(store, runId, id)
  const values = Object.entries(block?.value || {}).map(([port, raw]) => {
    if (runId === 'local') return raw
    const template = templates.find(value => value.port === port) || templates[0]
    const value = raw && typeof raw === 'object' && 'data' in raw ? raw : { data: raw }
    const series = seriesOf(value.data)
    const kind = value.error || block.error || value.data === null ? 'empty'
      : Array.isArray(value.data) ? series.length && series.every(Number.isFinite) ? 'chart' : 'rows'
        : ['number', 'string', 'boolean'].includes(typeof value.data) ? 'value' : 'empty'
    return { ...template, port, kind, series, display: block.error ? `error: ${block.error}`
      : kind === 'value' ? String(value.data) : summary(value),
    as_of: value.as_of, age_ms: Number.isFinite(value.as_of) ? Math.max(0, Date.now() - value.as_of) : null, stale: value.stale, simulated: value.simulated,
    truncated: value.truncated, host_tainted: value.host_tainted }
  })
  return <>
    {block?.error && !values.length && <article data-value-kind="empty"><h3>{templates[0]?.title || id}</h3><p>error: {block.error}</p></article>}
    {values.map(value => {
        const low = Math.min(...value.series)
        const high = Math.max(...value.series)
        return <article key={`${value.id}/${value.port}`} data-value-kind={value.kind}>
          <h3>{value.title}</h3>
          {value.kind === 'chart' ? <>
            <svg width="240" height="60" viewBox="0 0 240 60" aria-label={`${value.title} chart`}>
              <polyline fill="none" stroke="currentColor" points={value.series.map((number, index) =>
                `${index * 240 / Math.max(1, value.series.length - 1)},${high === low ? 30 : 60 - (number - low) * 60 / (high - low)}`).join(' ')} />
            </svg><p>{new Intl.NumberFormat().format(value.series.at(-1))}</p>
          </> : <p>{typeof value.display === 'number' ? new Intl.NumberFormat().format(value.display) : value.display}</p>}
          <span data-badge>{value.age_ms === null ? 'age unknown' : `as of ${Math.floor(value.age_ms / 1000)}s ago`}</span>
          {['stale', 'simulated', 'truncated'].map(badge => value[badge] && <span data-badge key={badge}>{badge}</span>)}
          {value.host_tainted && <span data-badge>host data</span>}
          <span data-badge>p50 {value.p50_us / 1000} ms · p99 {value.p99_us / 1000} ms</span>
        </article>
      })}
  </>
})

function AfterSaveDisplay({ provider, context, saveBar }) {
  const { artifacts, document, ready } = context
  const client = useMemo(() => createAfterSaveClient({ provider, artifacts }), [provider, artifacts])
  const [, refresh] = useState(0)
  useEffect(() => client.subscribe(() => refresh(value => value + 1)), [client])
  const view = client.read()
  const pending = ['pending', 'running'].includes(view.status)
  useEffect(() => {
    if (!pending) return undefined
    const timer = setInterval(() => refresh(value => value + 1), 1000)
    return () => clearInterval(timer)
  }, [pending])
  return <>
    {saveBar && <LabSaveBar document={document} ready={ready} client={artifacts} onTakeTheirs={graph => artifacts.takeGraph(graph)} />}
    <AfterSavePanel view={view} document={document} />
  </>
}

const loadLabCanvas = () => import('./LabCanvas.jsx')
let LabCanvas = lazy(loadLabCanvas)

// Inject before mounting. A fresh lazy boundary lets tests observe imports even
// when another test has already loaded the module.
export function setGraphCanvasLoader(loader = loadLabCanvas) {
  LabCanvas = lazy(loader)
}

export default function GraphView({ hidden = false, initialDocument, entries, onRun, provider: supplied, revision, operator = false, client = labArtifactsClient, renderProbe }) {
  renderProbe?.('graph')
  const [restored, setRestored] = useState(() => ({ graph: client.state.restoredGraph, key: client.state.restoredKey }))
  const [opened, setOpened] = useState(!hidden)
  const [localRevision, setRevision] = useState(0)
  const [local] = useState(() => createHostProvider({ framed: false, operator, onChange: () => setRevision(value => value + 1) }))
  const provider = supplied || local
  const live = useLiveGraph({ provider, registry: entries || registry, revision: supplied ? revision : localRevision, artifacts: client })
  const [, refreshHead] = useState(0)
  const [runMessage, setRunMessage] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const submissionPending = useRef(false)
  const streams = useRef(new Set())
  const mounted = useRef(false)
  const latest = useRef(null)
  latest.current = { document: live.draft?.document, head: client.state.head, docId: client.currentDocId() }
  useEffect(() => client.subscribe(() => refreshHead(value => value + 1)), [client])
  useEffect(() => {
    mounted.current = true
    const resumeOnFocus = () => streams.current.forEach(stream => {
      if (stream.authExpired && !stream.focusResumed && authHeaders().Authorization !== stream.expiredAuthorization) {
        stream.focusResumed = true
        labValueStore.resume(stream.runId)
      }
    })
    window.addEventListener('focus', resumeOnFocus)
    return () => {
      mounted.current = false
      window.removeEventListener('focus', resumeOnFocus)
      streams.current.forEach(stream => { stream.stop(); stream.unsubscribe() }); streams.current.clear()
    }
  }, [])
  const runSnapshot = () => labValueStore.getRuns().filter(run => run.id !== 'local' && run.doc_id === client.currentDocId()
    && run.graph_revision === client.state.head).map(run => run.id).join(',')
  const matchingRuns = useSyncExternalStore(labValueStore.subscribeRuns, runSnapshot)
  const valueRunId = matchingRuns.split(',')[0] || 'local'
  const activeRunStatus = useRunStatus(labValueStore, valueRunId)
  const localTiming = useRun(labValueStore, 'local')?.timing
  async function runSaved() {
    if (submissionPending.current || !client.state.headLoaded || client.state.head == null || !live.ready) return
    if (!labValueStore.canAddRun()) { setRunMessage('Too many active runs. Wait for a run to finish.'); return }
    submissionPending.current = true
    setSubmitting(true); setRunMessage('')
    const { docId, head, document } = latest.current
    try {
      const saved = await client.revision(docId, head)
      if (!mounted.current) return
      if (!saved.ok) {
        setRunMessage(saved.status === 401 ? 'Sign in again' : 'Saved revision unavailable'); return
      }
      if (latest.current.docId !== docId || latest.current.head !== head || !saved.graph
        || canonicalJSON(saved.graph) !== canonicalJSON(document)
        || canonicalJSON(saved.graph) !== canonicalJSON(latest.current.document)) {
        setRunMessage('Save before running'); return
      }
      if (!labValueStore.canAddRun()) { setRunMessage('Too many active runs. Wait for a run to finish.'); return }
      const result = await submitRun({ docId, graphRevision: head })
      if (!mounted.current) return
      if (!result.ok) {
        setRunMessage(result.status === 401 ? 'Sign in again' : result.status === 409 ? 'Run queue full'
          : typeof result.detail === 'string' ? result.detail : result.detail?.code || 'Run unavailable')
        return
      }
      labValueStore.upsertRun(result.runId, { state: result.state, graph_revision: head, doc_id: docId })
      const stream = openRunStream({ runIds: [result.runId], store: labValueStore })
      stream.runId = result.runId
      const stop = stream.stop
      stream.stop = () => { labValueStore.clearStreamControl(result.runId); stop() }
      labValueStore.setStreamControl(result.runId, stream)
      stream.unsubscribe = labValueStore.subscribeRun(result.runId, () => {
        const run = labValueStore.getRun(result.runId)
        const authExpired = run?.transport.status === 'auth_expired'
        if (authExpired && !stream.authExpired) {
          stream.expiredAuthorization = authHeaders().Authorization
          stream.focusResumed = false
        }
        stream.authExpired = authExpired
        if (!run || (terminalState(run.state) && ['finished', 'missing'].includes(run.transport.status))) {
          labValueStore.clearStreamControl(result.runId)
          stream.unsubscribe(); streams.current.delete(stream)
        }
      })
      streams.current.add(stream)
    } catch { if (mounted.current) setRunMessage('Run unavailable') }
    finally { submissionPending.current = false; if (mounted.current) setSubmitting(false) }
  }
  useEffect(() => {
    if (hidden || !live.ready) return undefined
    let generation = -1
    const hydrate = () => {
      if (generation === client.currentGeneration()) return
      generation = client.currentGeneration()
      void client.head(client.currentDocId())
    }
    const unsubscribe = client.subscribe(hydrate)
    hydrate()
    return unsubscribe
  }, [client, hidden, live.ready])
  useEffect(() => client.subscribe(() => {
    if (client.state.restoredKey !== restored.key) {
      live.onChange(null)
      setRestored({ graph: client.state.restoredGraph, key: client.state.restoredKey })
    }
  }), [client, restored.key, live.onChange])
  const validDocument = live.ready ? live.draft.document : null
  const pollers = useRef(new Map())
  const [sources] = useState(() => createStudioSources({
    fetchImpl: (path, init) => fetchWithBudget(globalThis.fetch, `${config.apiBase}${path}`, {
      ...init, headers: { ...init.headers, accept: 'application/json', 'X-Tenant-Id': config.tenant, ...authHeaders() },
    }),
  }))
  const ids = [...new Set((live.draft?.document.blocks || []).map(block => block.registry.id)
    .filter(id => ['studio-jobs', 'studio-capabilities'].includes(id)))].sort().join(',')
  useEffect(() => { if (!supplied) { provider.start(); return () => provider.stop() } }, [provider, supplied])
  useEffect(() => {
    if (supplied) {
      pollers.current.forEach(poller => poller.dispose())
      pollers.current.clear()
      return
    }
    const wanted = ids ? ids.split(',') : []
    for (const [id, poller] of pollers.current) if (!wanted.includes(id)) { poller.dispose(); pollers.current.delete(id) }
    for (const id of wanted) if (!pollers.current.has(id)) {
      const deliver = value => {
        try {
          provider.setStudioValue(id, value?.error || !Array.isArray(value?.data)
            ? runtimeValue([], { host_tainted: true, error: { code: value?.error?.code || 'source-failed' } })
            : value)
        } catch {
          provider.setStudioValue(id, runtimeValue([], { host_tainted: true, error: { code: 'source-refused' } }))
        }
      }
      const poller = sources.poll(id, { onValue: deliver, onError: () => {
        deliver(runtimeValue([], { host_tainted: true, error: { code: 'source-failed' } }))
      } })
      pollers.current.set(id, poller)
      void poller.start()
    }
  }, [ids, provider, sources, supplied])
  useEffect(() => () => { pollers.current.forEach(poller => poller.dispose()); pollers.current.clear() }, [])
  // Remember the first reveal without an effect-triggered render. Keep the editor
  // mounted afterwards so navigating between views preserves the draft.
  if (!hidden && !opened) setOpened(true)
  return (
    <div className="lab-graph-view" hidden={hidden}>
      {opened ? <>
        <section className="lab-graph-status" aria-label="Graph readiness">
          <p role="status">{validDocument ? 'Ready' : 'No valid graph'}</p>
          {live.problems.length > 0 && <ul>{live.problems.map((problem, index) => <li key={index}>{problem}</li>)}</ul>}
          <button type="button" disabled={!validDocument || (!onRun && (!client.state.headLoaded || client.state.head == null || submitting))}
            onClick={() => { if (validDocument) { if (onRun) onRun(validDocument); else void runSaved() } }}>
            {onRun || client.state.head == null ? 'Run graph' : `Run saved revision r${client.state.head}`}</button>
          {runMessage && <p role="status">{runMessage}</p>}
          {activeRunStatus.endsWith('/auth_expired') && <p>Sign in again, then resume <button type="button" onClick={() => labValueStore.resume(valueRunId)}>Resume</button></p>}
        </section>
        <LabSaveBar document={live.draft?.document} ready={live.ready} client={client} onTakeTheirs={graph => client.takeGraph(graph)} />
        <LiveValues values={live.values} provider={provider} saveBar={false} runId={onRun ? 'local' : valueRunId} renderProbe={renderProbe} />
        <Suspense fallback={<p role="status" aria-label="Loading dependency canvas">Loading dependency canvas…</p>}>
          <LabCanvas key={restored.key} initialDocument={restored.graph || initialDocument} entries={live.entries} measurements={localTiming?.measurements || live.measurements} upstream={live.upstream} onChange={live.onChange} codeEvidence={block => evidenceFor(provider, block)} />
        </Suspense>
      </> : <p data-graph-placeholder>Open Strategy to load the dependency canvas.</p>}
    </div>
  )
}
