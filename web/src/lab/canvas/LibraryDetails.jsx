import { useEffect, useRef } from 'react'
import { placement } from '../runtime/placement.js'
import { sentence } from '../honesty.js'
import { entryMeta } from './libraryCategories.js'

const typeOf = schema => Array.isArray(schema?.type) ? schema.type.join(' | ') : schema?.type || 'any'
export default function LibraryDetails({ manifest, onClose }) {
  const close = useRef(null)
  useEffect(() => {
    const previous = globalThis.document.activeElement
    close.current?.focus()
    return () => { if (previous?.isConnected) previous.focus() }
  }, [])
  const target = placement(manifest)
  return <dialog open className="lab-inspector" aria-label={`About ${manifest.title}`}
    style={{ position: 'fixed', insetInlineStart: 'auto', insetInlineEnd: 0, top: 0, margin: 0,
      width: 'min(28rem, 100%)', maxHeight: '100dvh', boxSizing: 'border-box', zIndex: 10 }}
    onCancel={event => { event.preventDefault(); onClose() }}
    onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); onClose() } }}>
    <button type="button" ref={close} onClick={onClose}>Close details</button>
    <h2>{manifest.title}</h2><p>{entryMeta(manifest).idVersion}</p>
    <p>{manifest.description || 'No description declared.'}</p>
    {['inputs', 'outputs'].map(direction => <section key={direction} aria-label={direction === 'inputs' ? 'Inputs' : 'Outputs'}>
      <h3>{direction === 'inputs' ? 'Inputs' : 'Outputs'}</h3>
      {Object.entries(manifest[direction]).map(([name, port]) => <p key={name}>
        {name}: {typeOf(port.schema)} · {port.unit} · {port.mode}
      </p>)}
      {!Object.keys(manifest[direction]).length && <p>None declared.</p>}
    </section>)}
    <h3>Settings</h3>
    {Object.entries(manifest.settings_schema?.properties || {}).map(([name, schema]) => <p key={name}>
      {name}: {typeOf(schema)}
      {Object.entries(schema).filter(([key]) => ['minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'minLength', 'maxLength', 'minItems', 'maxItems', 'enum'].includes(key))
        .map(([key, value]) => <span key={key}> · {key}: {JSON.stringify(value)}</span>)}
      {Object.hasOwn(schema, 'const') && <span> · Set by registration. Operator only.</span>}
    </p>)}
    {!Object.keys(manifest.settings_schema?.properties || {}).length && <p>No settings required.</p>}
    <h3>Budget, not a measurement</h3>
    <p>{manifest.latency_budget_us ? `p50 ${manifest.latency_budget_us.p50} µs · p99 ${manifest.latency_budget_us.p99} µs` : 'No budget declared.'}</p>
    <h3>Declared sample, not a measurement</h3>
    <h4>Input</h4><pre>{JSON.stringify(manifest.sample_input, null, 2)}</pre>
    <h4>Output</h4><pre>{JSON.stringify(manifest.sample_output, null, 2)}</pre>
    {entryMeta(manifest).placeholder && <p>{sentence('placeholder_block')}</p>}
    {target === 'browser' && <p>{sentence('browser_only_block')}</p>}
    {target === 'bot' && <p>{sentence('runs_on_bot')}</p>}
  </dialog>
}
