import { useEffect, useRef, useState } from 'react'
import { BINDING_FIELDS, FORMATS, PRESETS, RENDERER_VERSION } from './widgetCatalog.js'
import { HEIGHT_BOUNDS } from './useWidgetEditor.js'
import { loadGraphOutputs } from './widgetsClient.js'

const colors = ['auto', 'ink', 'leaf', 'slate', 'loss']
function Select({ label, value, options, onChange }) {
  return <label>{label}<select value={value} onChange={(e) => onChange(e.target.value)}>{options.map((v) => <option key={v} value={v}>{v}</option>)}</select></label>
}
function NumberField({ label, value, min, max, step = 1, onChange, type = 'number' }) {
  const [draft, setDraft] = useState(String(value))
  useEffect(() => { setDraft(String(value)) }, [value])
  function commit() {
    const next = draft.trim() === '' || !Number.isFinite(Number(draft)) ? value : Math.max(min, Math.min(max, Number(draft)))
    setDraft(String(next))
    if (next !== value) onChange(next)
  }
  return <label>{label}<input type={type} value={type === 'range' ? value : draft} min={min} max={max} step={step} onChange={(e) => {
    if (type === 'range') onChange(Number(e.target.value))
    else setDraft(e.target.value)
  }} onBlur={type === 'range' ? undefined : commit} onKeyDown={(e) => {
    if (type !== 'range' && e.key === 'Enter') { e.preventDefault(); commit() }
  }} /></label>
}
function BindingPicker({ element, edit, local, onOutputs, graphOutputs }) {
  const binding = element.binding
  const graphId = binding?.doc_id ?? ''
  const [discovery, setDiscovery] = useState(null)
  const result = discovery?.docId === graphId ? discovery.result : graphOutputs?.has(graphId) ? { ok: true, outputs: graphOutputs.get(graphId) } : null
  const [loading, setLoading] = useState(false)
  const generation = useRef(0)
  useEffect(() => { generation.current += 1; setDiscovery(null); setLoading(false); return () => { generation.current += 1 } }, [element.id, binding?.source, graphId])
  function patch(change) {
    const next = { ...binding, ...change }
    if (next.source === 'kashbot.board') {
      if (next.field === 'winRate') next.window = 'today-utc'
      else delete next.window
    }
    edit({ binding: next })
  }
  async function outputs() {
    const ticket = ++generation.current
    setLoading(true)
    const next = await loadGraphOutputs(graphId)
    if (generation.current !== ticket) return
    setDiscovery({ docId: graphId, result: next }); setLoading(false)
    if (next.ok) onOutputs?.(graphId, next.outputs)
  }
  return <fieldset className="widgets-fields"><legend>Binding</legend>
    <Select label="Binding source" value={binding?.source ?? 'none'} options={['none', 'kashbot.board', 'graph.output']} onChange={(source) => {
      generation.current += 1; setDiscovery(null); setLoading(false)
      if (source === 'none') edit({ binding: undefined })
      else edit({ binding: { source, min_renderer: RENDERER_VERSION, scale: 'identity', tone: 'auto', ...(source === 'kashbot.board' ? { field: 'total' } : { doc_id: graphId, output: '' }) } })
    }} />
    {binding?.source === 'kashbot.board' && <Select label="Board field" value={binding.field} options={BINDING_FIELDS} onChange={(field) => patch({ field })} />}
    {binding?.source === 'graph.output' && <>
      <label>Graph document ID<input value={graphId} onChange={(e) => { generation.current += 1; setDiscovery(null); setLoading(false); patch({ doc_id: e.target.value, output: '' }) }} /></label>
      <button type="button" disabled={local || loading || !graphId} onClick={outputs}>{loading ? 'Loading outputs…' : 'Load outputs'}</button>
      {local && <p>Graph output discovery is available in the authenticated Lab.</p>}
      <label>Graph output<select value={result?.ok && !result.outputs.some((o) => o.name === binding.output) ? '' : binding.output} onChange={(e) => patch({ output: e.target.value })}>
        <option value="">{binding.output && result?.ok && !result.outputs.some((o) => o.name === binding.output) ? 'Output not found' : 'Choose an output'}</option>
        {result?.outputs?.map((output) => <option key={output.name} value={output.name}>{output.name} — {output.type}</option>)}
      </select></label>
      {result?.outputs?.find((o) => o.name === binding.output) && <p>Output type: {result.outputs.find((o) => o.name === binding.output).type}</p>}
      {result && !result.ok && <p role="alert">{String(result.detail)}</p>}
      <label>Output field (optional)<input value={binding.field ?? ''} maxLength={512} onChange={(e) => patch({ field: e.target.value || undefined })} /></label>
    </>}
    {binding && <>
      <Select label="Binding scale" value={binding.scale} options={['identity', 'fraction', 'today-percent', 'open-count']} onChange={(scale) => patch({ scale })} />
      <Select label="Binding tone" value={binding.tone} options={colors} onChange={(tone) => patch({ tone })} />
    </>}
  </fieldset>
}
export default function WidgetInspector({ doc, selectedId, dispatch, local, onOutputs, graphOutputs }) {
  const format = doc.active, surface = doc.formats[format], W = FORMATS[format].w
  const element = surface.els.find((e) => e.id === selectedId)
  const [resetAt, setResetAt] = useState(null)
  useEffect(() => { setResetAt(null) }, [format])
  useEffect(() => { if (resetAt === null) return; const timer = setTimeout(() => setResetAt(null), 3000); return () => clearTimeout(timer) }, [resetAt])
  const surfaceEdit = (patch) => dispatch({ type: 'surface', patch })
  const edit = (patch) => dispatch({ type: 'edit', patch })
  const check = (label, value, onChange) => <label>{label}<input type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} /></label>
  return <aside className="widgets-fields" aria-label="Inspector">
    {!element ? <>
      <h3>Surface</h3><p>{W} × {surface.h} points</p>
      {HEIGHT_BOUNDS[format] && <NumberField label="Height" type="range" value={surface.h} min={HEIGHT_BOUNDS[format][0]} max={HEIGHT_BOUNDS[format][1]} onChange={(h) => surfaceEdit({ h })} />}
      <NumberField label="Corners" type="range" value={surface.radius} min={0} max={Math.floor(Math.min(W, surface.h) / 2)} onChange={(radius) => surfaceEdit({ radius })} />
      <Select label="Background" value={surface.bg} options={['black', 'blur', 'none']} onChange={(bg) => surfaceEdit({ bg })} />
      {FORMATS[format].accessory && check('Preview one-color', surface.vibrant, (vibrant) => surfaceEdit({ vibrant }))}
      {check('Include this format', surface.ship, (ship) => surfaceEdit({ ship }))}
      {['la', 'diExp'].includes(format) && surface.h > 160 && <p>iOS cuts off content below 160 points.</p>}
      {format === 'lockInline' && <p>Inline widgets support one line of text.</p>}
      <button type="button" onClick={() => { if (resetAt !== null && Date.now() - resetAt < 3000) { dispatch({ type: 'reset' }); setResetAt(null) } else setResetAt(Date.now()) }}>{resetAt !== null ? 'Click again to reset' : 'Reset this format'}</button>
    </> : <>
      <h3>{PRESETS[element.preset]?.n ?? element.kind}</h3>
      <div className="widget-geometry">{['x', 'y', 'w', 'h'].map((key) => <NumberField key={`${element.id}:${key}`} label={key.toUpperCase()} value={element[key]}
        min={key === 'w' ? Math.min(6, W - element.x) : key === 'h' ? Math.min(element.preset === 'divider' ? 1 : 6, surface.h - element.y) : 0}
        max={key === 'x' ? W - element.w : key === 'y' ? surface.h - element.h : key === 'w' ? W - element.x : surface.h - element.y} onChange={(value) => edit({ [key]: value })} />)}</div>
      <NumberField label="Scale" type="range" value={element.scale} min={0.4} max={2} step={0.02} onChange={(scale) => edit({ scale })} />
      <NumberField label="Weight" value={element.weight} min={400} max={900} step={100} onChange={(weight) => edit({ weight: Math.round(weight / 100) * 100 })} />
      <Select label="Color" value={element.color} options={colors} onChange={(color) => edit({ color })} />
      <Select label="Alignment" value={element.align} options={['left', 'center', 'right']} onChange={(align) => edit({ align })} />
      {PRESETS[element.preset]?.col && <NumberField label="Rows" value={element.maxRows ?? 3} min={1} max={3} onChange={(maxRows) => edit({ maxRows: Math.round(maxRows) })} />}
      {element.arrows !== undefined && check('Arrows', element.arrows, (arrows) => edit({ arrows }))}
      {element.showPct !== undefined && check('Show %', element.showPct, (showPct) => edit({ showPct }))}
      {element.count !== undefined && <NumberField label="Entries" value={element.count} min={1} max={['history', 'historyList'].includes(element.preset) ? 4 : 10} onChange={(count) => edit({ count: Math.round(count) })} />}
      {element.opacity !== undefined && <NumberField label="Opacity" type="range" value={element.opacity} min={0} max={1} step={0.05} onChange={(opacity) => edit({ opacity })} />}
      {element.tracking !== undefined && <NumberField label="Spacing" value={element.tracking} min={0} max={20} onChange={(tracking) => edit({ tracking })} />}
      {element.kind === 'text' && (!element.preset || element.preset === 'text') && <label>Text<input value={element.text ?? ''} maxLength={40} onChange={(e) => edit({ text: e.target.value.slice(0, 40) })} /></label>}
      {check('Visible', element.visible, (visible) => edit({ visible }))}
      <BindingPicker key={element.id} element={element} edit={edit} local={local} onOutputs={onOutputs} graphOutputs={graphOutputs} />
      <div className="widget-toolbar"><button type="button" disabled={surface.els.length >= 32} onClick={() => dispatch({ type: 'duplicate' })}>Duplicate</button><button type="button" onClick={() => dispatch({ type: 'delete' })}>Remove</button></div>
    </>}
  </aside>
}
