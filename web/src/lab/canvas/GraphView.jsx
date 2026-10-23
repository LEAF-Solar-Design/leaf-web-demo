import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { registry } from './registry.js'
import { pipeId } from './graphDoc.js'
import { createHostProvider } from '../host/hostProvider.js'
import { validateResidency } from '../host/hostResidency.js'
import { createStudioSources, evaluateGraph, runtimeValue, sourceAge } from '../runtime/index.js'

const wallTime = () => Date.now()
const tick = () => performance.now()
const summary = value => value.error ? `error: ${value.error.code}`
  : value.data === null ? 'null' : Array.isArray(value.data) ? `${value.data.length} rows`
    : typeof value.data === 'number' ? String(value.data) : 'text'
const seriesOf = data => Array.isArray(data) ? data.map(item => typeof item === 'number' ? item
  : item && typeof item === 'object' ? Object.values(item).find(Number.isFinite) : undefined) : []

export function useLiveGraph({ provider, registry: base = registry, now = wallTime, clock = tick, revision }) {
  const [draft, onChange] = useState(null)
  const [evaluation, setEvaluation] = useState({ result: null, problems: [], count: 0, times: [] })
  const signature = JSON.stringify(provider.entries())
  // The provider is immutable; mounts forward its constructor onChange as a
  // revision so value-only updates also refresh the observed pins and results.
  const entries = useMemo(() => ({ ...base, ...provider.entries() }), [base, provider, signature, revision])
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
      if (!draft.ready) {
        publish(previous => ({ ...previous, result: null, problems: [] }))
        return
      }
      const result = evaluateGraph(draft.document, entries, { sources: provider.sources(draft.document, entries), now: clock })
      publish(previous => ({ result, problems: [], count: previous.count + 1,
        times: [...previous.times, result.timing.duration_ms].slice(-20) }))
    } catch {
      publish(previous => ({ ...previous, result: null, problems: ['Graph evaluation failed'] }))
    }
  }, [draft, entries, residency, provider, revision, clock])
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
      : typeof value.data === 'string' || Number.isFinite(value.data) ? 'value'
        : Array.isArray(value.data) ? series.length > 0 && series.every(Number.isFinite) ? 'chart' : 'rows' : 'empty'
    return { id, port, title, kind, display: kind === 'value' ? value.data : summary(value), series,
      as_of: value.as_of, age_ms: sourceAge(value, now()), stale: value.stale, simulated: value.simulated,
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
  const problems = [...(draft?.problems || []), ...residency.problems, ...(current ? evaluation.problems : [])]
  return { entries, draft, result, problems, ready: Boolean(draft?.ready && residency.ok && !problems.length),
    measurements, upstream, badges: values, values, onChange }
}

export function LiveValues({ values, provider }) {
  const state = provider.state
  return <>
    <p data-sources-state>{!state.framed ? 'Studio sources: jobs, capabilities'
      : state.reason === 'Sample' ? 'Dashboard sources: Sample (none announced within 2 seconds)'
        : `Dashboard sources: ${state.sources.join(', ')}`}</p>
    <section aria-label="Values">
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
    </section>
    <p role="note">Save and history: not yet available</p>
  </>
}

const loadLabCanvas = () => import('./LabCanvas.jsx')
let LabCanvas = lazy(loadLabCanvas)

// Inject before mounting. A fresh lazy boundary lets tests observe imports even
// when another test has already loaded the module.
export function setGraphCanvasLoader(loader = loadLabCanvas) {
  LabCanvas = lazy(loader)
}

export default function GraphView({ hidden = false, initialDocument, entries, onRun, provider: supplied, operator = false }) {
  const [opened, setOpened] = useState(!hidden)
  const [revision, setRevision] = useState(0)
  const [local] = useState(() => createHostProvider({ framed: false, operator, onChange: () => setRevision(value => value + 1) }))
  const provider = supplied || local
  const live = useLiveGraph({ provider, registry: entries || registry, revision })
  const validDocument = live.ready ? live.draft.document : null
  const pollers = useRef(new Map())
  const [sources] = useState(() => createStudioSources())
  const ids = [...new Set((live.draft?.document.blocks || []).map(block => block.registry.id)
    .filter(id => ['studio-jobs', 'studio-capabilities'].includes(id)))].sort().join(',')
  useEffect(() => { if (!supplied) { provider.start(); return () => provider.stop() } }, [provider, supplied])
  useEffect(() => {
    if (supplied) return
    const wanted = ids ? ids.split(',') : []
    for (const [id, poller] of pollers.current) if (!wanted.includes(id)) { poller.dispose(); pollers.current.delete(id) }
    for (const id of wanted) if (!pollers.current.has(id)) {
      const deliver = value => {
        try {
          provider.setStudioValue(id, value?.error || !Array.isArray(value?.data)
            ? runtimeValue([], { host_tainted: true, error: { code: value?.error?.code || 'source-failed' } })
            : value)
        } catch { /* Keep provider refusals out of the poller. */ }
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
          <button type="button" disabled={!validDocument || !onRun}
            title={onRun ? undefined : 'Execution is unavailable here.'}
            onClick={() => { if (validDocument) onRun?.(validDocument) }}>Run graph</button>
        </section>
        <LiveValues values={live.values} provider={provider} />
        <Suspense fallback={<p role="status" aria-label="Loading dependency canvas">Loading dependency canvas…</p>}>
          <LabCanvas initialDocument={initialDocument} entries={live.entries} measurements={live.measurements} upstream={live.upstream} onChange={live.onChange} />
        </Suspense>
      </> : <p data-graph-placeholder>Open Strategy to load the dependency canvas.</p>}
    </div>
  )
}
