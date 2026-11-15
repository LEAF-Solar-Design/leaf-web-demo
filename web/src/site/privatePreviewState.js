// Small versioned chart-state messages, not a durable persistence adapter.
const fields = ['averaging','groups','range','reference','scale','series','timeAxis','version']
export function copyChartState(value) {
  if (!value || typeof value !== 'object' || Object.keys(value).sort().join(',') !== fields.join(',') || value.version !== 1) throw new Error('invalid_chart_state')
  if (!['all','2026'].includes(value.range) || !['Log','Linear'].includes(value.scale) ||
      !['2023-01-01','2026-02-01'].includes(value.reference) || ![28,56,84,'Recorded'].includes(value.averaging) ||
      !['expanded2026','even'].includes(value.timeAxis)) throw new Error('invalid_chart_state')
  for (const key of ['series','groups']) if (!Array.isArray(value[key]) || value[key].length > 256 ||
      value[key].some(v => typeof v !== 'string' || !v || v.length > 200) || new Set(value[key]).size !== value[key].length) throw new Error('invalid_chart_state')
  const copied = { version:1, range:value.range, scale:value.scale, reference:value.reference,
    averaging:value.averaging, timeAxis:value.timeAxis, series:[...value.series], groups:[...value.groups] }
  if (new TextEncoder().encode(JSON.stringify(copied)).length > 16384) throw new Error('invalid_chart_state')
  return copied
}
// Self-contained: serialized into an opaque frame, no imports or parent API.
function bridge(identity, initial) {
  let state = initial
  const copy = value => JSON.parse(JSON.stringify(value))
  const notify = () => dispatchEvent(new CustomEvent('leaf-preview-state', { detail:copy(state) }))
  Object.defineProperty(window, 'leafPreviewState', { configurable:false, writable:false, value:Object.freeze({
    get:() => copy(state),
    set:value => { const raw = JSON.stringify(value); if (raw.length > 16384) throw new Error('state_too_large'); state = JSON.parse(raw); parent.postMessage({ ...identity, protocol:'leaf.chart-state.v1', type:'change', state:copy(state) }, '*') },
  }) })
  addEventListener('message', event => {
    const m = event.data
    if (event.source !== parent || !m || m.protocol !== 'leaf.chart-state.v1' || m.type !== 'restore' ||
        m.nonce !== identity.nonce || m.jobId !== identity.jobId || m.artifactId !== identity.artifactId) return
    const raw = JSON.stringify(m.state)
    if (raw.length > 16384) return
    state = JSON.parse(raw); notify()
  })
  // Preview links must not leave this sandbox. No blanket origin privilege is
  // granted to emulate history or to follow external links.
  addEventListener('click', event => { if (event.target instanceof Element && event.target.closest('a[href]')) event.preventDefault() }, true)
  addEventListener('submit', event => event.preventDefault(), true)
}
export function attachChartStateBridge(html, identity, initial) {
  const state = initial == null ? null : copyChartState(initial)
  const doc = new DOMParser().parseFromString(html, 'text/html')
  const script = doc.createElement('script')
  const safe = value => JSON.stringify(value).replace(/</g, () => String.fromCharCode(92) + 'u003c')
  script.textContent = `(${bridge.toString()})(${safe(identity)},${safe(state)});`
  doc.head.append(script)
  return '<!doctype html>' + doc.documentElement.outerHTML
}
