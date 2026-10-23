import { useState } from 'react'
import { canonicalJSON } from '../contract.js'
import { manifestFor, registry } from './registry.js'
import { connectionStatus } from './portCompat.js'
import { inputValues, pipeId, validateGraph } from './graphDoc.js'

function SettingField({ name, schema, label, required, value, onChange }) {
  const complex = schema.type === 'object' || schema.type === 'array'
  const [raw, setRaw] = useState(complex && value !== undefined ? JSON.stringify(value) : '')
  const [error, setError] = useState('')
  return <label>{label || name}{required ? ' (required)' : ' (optional)'}
    {schema.enum ? <select value={value === undefined ? '' : canonicalJSON(value)} required={required}
      onChange={event => onChange(event.target.value === '' ? undefined : JSON.parse(event.target.value))}>
      <option value="">Choose</option>{schema.enum.map(item => <option key={canonicalJSON(item)} value={canonicalJSON(item)}>{String(item)}</option>)}
    </select> : complex ? <textarea value={raw} required={required} onChange={event => {
      setRaw(event.target.value)
      try { onChange(event.target.value === '' ? undefined : JSON.parse(event.target.value)); event.target.setCustomValidity(''); setError('') }
      catch { event.target.setCustomValidity('Enter valid JSON'); setError('Enter valid JSON') }
    }} /> : schema.type === 'boolean' ? <select value={value === undefined ? '' : String(value)} required={required}
      onChange={event => onChange(event.target.value === '' ? undefined : event.target.value === 'true')}>
      <option value="">Choose</option><option value="true">True</option><option value="false">False</option>
    </select> : <input type={['number', 'integer'].includes(schema.type) ? 'number' : 'text'}
      step={schema.type === 'integer' ? 1 : 'any'} min={schema.minimum} max={schema.maximum}
      minLength={schema.minLength} maxLength={schema.maxLength} required={required} value={value ?? ''}
      onChange={event => onChange(event.target.value === '' ? undefined : ['number', 'integer'].includes(schema.type) ? Number(event.target.value) : event.target.value)} />}
    {schema.description && <small>{schema.description}</small>}{error && <span role="alert">{error}</span>}
  </label>
}

export default function BlockPanel({ block, document, onSettings, onConnect, onDisconnect, entries = registry, upstream = {} }) {
  const manifest = manifestFor(block, entries)
  const [tab, setTab] = useState('Setup')
  const [draft, setDraft] = useState(block.settings)
  const [json, setJson] = useState(canonicalJSON(block.settings))
  const [message, setMessage] = useState('')
  const [test, setTest] = useState(null)
  const [source, setSource] = useState('')
  const [target, setTarget] = useState('')
  const properties = manifest.settings_schema.properties || {}
  const order = [...new Set([...(manifest.ui_schema.order || []).filter(name => Object.hasOwn(properties, name)), ...Object.keys(properties)])]
  const save = value => {
    try { onSettings(value); setDraft(value); setJson(canonicalJSON(value)); setMessage('Settings saved') }
    catch (error) { setMessage(error.message) }
  }
  const candidates = document.blocks.flatMap(node => Object.keys(manifestFor(node, entries).outputs).map(port => ({ node: node.id, port })))
  const options = target ? candidates.filter(from => connectionStatus(document, { from, to: { node: block.id, port: target } }, entries).ok) : []
  return <>
    <div role="tablist" aria-label="Block settings">{['Setup', 'Configure', 'Test', 'Code'].map(name =>
      <button type="button" key={name} id={`block-tab-${name}`} role="tab" aria-selected={tab === name}
        aria-controls="block-tab-panel" onClick={() => { setTab(name); setMessage('') }}>{name}</button>)}</div>
    <div role="tabpanel" id="block-tab-panel" aria-labelledby={`block-tab-${tab}`}>
      {tab === 'Setup' && <form onSubmit={event => { event.preventDefault(); save(draft) }}>
        {order.map(name => <SettingField key={name} name={name} schema={properties[name]} label={manifest.ui_schema.labels?.[name]}
          required={manifest.settings_schema.required?.includes(name)} value={draft[name]} onChange={value => {
            const next = { ...draft }; if (value === undefined) delete next[name]; else next[name] = value
            setDraft(next)
          }} />)}
        {!order.length && <p>No settings required.</p>}<button type="submit">Save settings</button>
      </form>}
      {tab === 'Configure' && <>
        <h3>Source</h3>
        <label>Input<select value={target} onChange={event => { setTarget(event.target.value); setSource('') }}>
          <option value="">Choose input</option>{Object.keys(manifest.inputs).map(port => <option key={port}>{port}</option>)}
        </select></label>
        <label>Source<select value={source} onChange={event => setSource(event.target.value)}>
          <option value="">Choose output</option>{options.map(from => <option key={`${from.node}/${from.port}`} value={JSON.stringify(from)}>{from.node} / {from.port}</option>)}
        </select></label>
        <button type="button" disabled={!source || !target} onClick={() => {
          try { onConnect({ from: JSON.parse(source), to: { node: block.id, port: target } }); setSource(''); setMessage('Pipe connected') }
          catch (error) { setMessage(error.message) }
        }}>Connect pipe</button>
        <p>Only compatible outputs without cycles are offered. Disconnect a pipe to replace it.</p>
        {document.pipes.filter(pipe => pipe.to.node === block.id).map(pipe => <p key={pipeId(pipe)}>
          {pipe.from.node}/{pipe.from.port} → {pipe.to.port} <button type="button" onClick={() => onDisconnect(pipeId(pipe))}>Disconnect {pipe.to.port}</button>
        </p>)}
        {Object.entries(manifest.inputs).map(([name, pin]) => <p key={name}>{name}: {pin.has_default ? `omitted uses default ${JSON.stringify(pin.default)}` : pin.required ? 'pipe required' : 'optional, no declared default'}</p>)}
        <details><summary>Submit settings JSON</summary><p>Paste settings JSON, including a Claude proposal, for validation.</p>
          <label>Settings JSON<textarea value={json} onChange={event => setJson(event.target.value)} /></label>
          <button type="button" onClick={() => { try { save(JSON.parse(json)) } catch (error) { setMessage(error.message) } }}>Apply settings JSON</button>
        </details>
      </>}
      {tab === 'Test' && <>
        <button type="button" onClick={() => setTest(validateGraph(document, entries))}>Validate graph</button>
        {test && <p role="status">{test.message} <span className="leaf-timing">{test.ms.toFixed(2)} ms</span></p>}
        <p>Validation checks wiring and settings. Execution is unavailable here.</p>
        <h3>Resolved inputs</h3><pre>{canonicalJSON(inputValues(document, block, upstream, entries))}</pre>
        <h3>Declared sample output</h3><pre>{canonicalJSON(manifest.sample_output)}</pre>
      </>}
      {tab === 'Code' && <><h3>Code unavailable</h3><p>Code editing lands with SEL-217. Rust blocks run in a separate host process.</p>
        <pre>{canonicalJSON(block.registry)}</pre></>}
    </div>
    {message && <p role="status">{message}</p>}
  </>
}
