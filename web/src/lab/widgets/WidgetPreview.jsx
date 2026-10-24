import { useEffect, useMemo, useState } from 'react'
import { resolveValues } from './widgetsClient.js'
import { renderWidget } from './widgetRender.js'
import { MAX_TEXT_LENGTH } from './widgetDoc.js'
import { FORMATS } from './widgetCatalog.js'
import { kashbotSamplePack } from '../host/kashbotSamplePack.js'
import './widgets.css'

const defaultClient = { resolveValues }

// rowsOf() omits simulation metadata, so read the provider's runtime envelopes.
function hostBoard(host, field) {
  if (!host) return { board: { rows: kashbotSamplePack.rows['studio-jobs'] }, sample: true }
  try {
    const entries = host.entries(), manifest = entries['host-source@1.0.0']
    if (!manifest) return { board: {}, sample: true }
    const blocks = host.state.sources.map((source, index) => ({ id: `preview-${index}`,
      registry: { id: manifest.id, version: manifest.version.semver, code_sha256: manifest.version.code_sha256 }, settings: { source } }))
    const value = Object.values(host.sources({ blocks }, entries)).filter((value) =>
      value.data.some((row) => Object.hasOwn(row, field) || Array.isArray(row.rows) && row.rows.some((item) => Object.hasOwn(item, field)))).at(-1)
    return { board: Object.assign({}, ...(value?.data ?? [])), sample: !value,
      host_tainted: value?.host_tainted, simulated: value?.simulated,
      stale: value?.stale, as_of: value?.as_of ?? null }
  } catch { return { board: {}, sample: true } }
}

export function WidgetPreview({ docId, doc, format = doc.active, host = null, client = defaultClient, now = Date.now, intervalMs = 5000 }) {
  const period = Math.max(5000, Number(intervalMs) || 5000), surface = doc.formats[format]
  const bindingKey = JSON.stringify(surface.els.filter((element) => element.binding?.source === 'graph.output').map((element) => element.binding))
  const graphBindings = useMemo(() => JSON.parse(bindingKey), [bindingKey])
  const [poll, setPoll] = useState({ values: [], failed: false })
  const [clock, setClock] = useState(() => now())
  useEffect(() => {
    let disposed = false, inFlight = false, controller = null, timer = null, lastStarted = -Infinity
    let wasVisible = document.visibilityState === 'visible', resumePending = false
    setPoll({ values: [], failed: false })
    function schedule() {
      clearTimeout(timer)
      const delay = docId && graphBindings.length ? Math.max(0, lastStarted + period - now()) : period
      if (!disposed && document.visibilityState === 'visible') timer = setTimeout(tick, delay)
    }
    async function tick(immediate = false) {
      if (disposed || document.visibilityState !== 'visible') return
      setClock(now())
      if (inFlight) return
      if (!docId || !graphBindings.length || (!immediate && now() - lastStarted < period)) { schedule(); return }
      inFlight = true; lastStarted = now(); controller = new AbortController()
      const active = controller
      schedule()
      try {
        const result = await client.resolveValues(docId, graphBindings, { signal: active.signal,
          // The client owns a timeout signal. Join cancellation at the fetch seam.
          fetch: (url, options) => {
            options.signal?.addEventListener('abort', () => active.abort(), { once: true })
            return globalThis.fetch(url, { ...options, signal: active.signal })
          } })
        if (disposed) return
        if (result?.ok === false || !Array.isArray(result?.values)) throw new Error()
        const time = now()
        setClock(time)
        setPoll((previous) => ({ failed: false, values: graphBindings.map((_, index) => {
          const entry = result.values[index]
          return entry?.status === 'resolved' ? { value: entry.value, time, resolved: true, stale: false }
            : previous.values[index]?.resolved ? { ...previous.values[index], stale: true } : { resolved: false }
        }) }))
      } catch { if (!disposed) setPoll((previous) => ({ ...previous, failed: true })) }
      finally {
        inFlight = false
        if (!disposed && resumePending && document.visibilityState === 'visible') { resumePending = false; tick(true) }
        else if (!disposed && document.visibilityState === 'visible') {
          if (now() - lastStarted >= period) tick()
          else schedule()
        }
      }
    }
    function visibility() {
      const visible = document.visibilityState === 'visible'
      if (visible === wasVisible) return
      clearTimeout(timer)
      if (visible && !wasVisible) {
        if (inFlight) { resumePending = true; schedule() }
        else tick(true)
      }
      wasVisible = visible
    }
    document.addEventListener('visibilitychange', visibility)
    tick(true)
    return () => { disposed = true; clearTimeout(timer); controller?.abort(); document.removeEventListener('visibilitychange', visibility) }
  }, [docId, format, graphBindings, client, now, period])
  let graphIndex = 0
  const elements = surface.els.map((element) => {
    const graph = element.binding?.source === 'graph.output', entry = graph ? poll.values[graphIndex++] : null
    const fromHost = element.binding?.source === 'kashbot.board'
    const local = fromHost ? hostBoard(host, element.binding.field) : { board: {}, sample: true }
    let freshness = graph ? !entry?.resolved ? 'sample' : entry.stale || clock - entry.time > 3 * period ? 'stale' : 'live'
      : fromHost && !local.sample ? local.simulated ? 'simulated' : local.stale || local.as_of === null || clock - local.as_of > 60000 ? 'stale' : 'live' : 'sample'
    let painted = element, truncated = false
    if (graph) {
      const value = entry?.resolved ? entry.value : element.text ?? 'Sample'
      // Server field selection is complete; never reinterpret it as a board field.
      painted = { ...element, kind: 'text', text: typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value ?? 'Not reported') }
      if (entry?.resolved && painted.text.length > MAX_TEXT_LENGTH) {
        painted.text = painted.text.slice(0, MAX_TEXT_LENGTH - 1) + '…'
        truncated = true
      }
      delete painted.binding; delete painted.preset
    }
    let nodes
    try {
      nodes = renderWidget({ ...doc, active: format, formats: { [format]: { ...surface, els: [painted] } } }, { format, board: fromHost ? local.board : {} }).nodes
      if (fromHost && local.sample && element.text) nodes = nodes.map((node) => node.kind === 'text' ? { ...node, text: element.text } : node)
    } catch {
      nodes = [{ ...element, kind: 'text', text: element.text ?? 'Sample', fontSize: element.h * 0.8, color: '--leaf-color-ink' }]
      freshness = 'sample'; truncated = false
    }
    return { element, nodes, freshness, truncated, tainted: fromHost && local.host_tainted === true }
  }).filter(({ element }) => element.visible)
  return <section className="widget-preview" aria-label="Live widget preview">
    <h3>Live widget preview</h3>
    {poll.failed && <p role="alert">Values unavailable; showing the last known values</p>}
    <svg role="img" aria-label="Widget values" viewBox={`0 0 ${FORMATS[format].w} ${surface.h}`} style={{ width: '100%', maxWidth: FORMATS[format].w, background: '#09090b', borderRadius: surface.radius }}>
      {elements.map(({ element, nodes, freshness, truncated, tainted }) => <g key={element.id} data-element-id={element.id} data-freshness={freshness} data-truncated={String(truncated)} data-host-tainted={String(tainted)}>
        <title>{`${element.id}: ${freshness}${truncated ? ' (truncated)' : ''}`}</title>
        {nodes.map((node) => <svg key={node.id} x={node.x} y={node.y} width={node.w} height={node.h} viewBox={node.viewBox ?? `0 0 ${node.w} ${node.h}`} overflow="hidden" opacity={node.opacity}>
          {node.kind === 'text' ? <text x={node.align === 'right' ? node.w : node.align === 'center' ? node.w / 2 : 0} y={node.h / 2} dominantBaseline="central" textAnchor={node.align === 'right' ? 'end' : node.align === 'center' ? 'middle' : 'start'} fill={`var(${node.color}, #ffffff)`} fontSize={node.fontSize} fontWeight={node.weight}>{node.text}</text>
            : node.kind === 'box' ? <rect width={node.w} height={node.h} fill={`var(${node.color}, #86efac)`} />
              : <path d={node.path} fill={node.stroke ? 'none' : `var(${node.color}, #86efac)`} stroke={node.stroke ? `var(${node.color}, #86efac)` : undefined} strokeWidth={2 * node.scale} />}
        </svg>)}
      </g>)}
    </svg>
    <ul aria-label="Element freshness">{elements.map(({ element, freshness, truncated }) => <li key={element.id}>{element.id}: {freshness}{truncated ? ' (truncated)' : ''}</li>)}</ul>
  </section>
}

export default WidgetPreview
