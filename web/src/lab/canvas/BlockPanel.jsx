import { useEffect, useRef, useState } from 'react'
import { canonicalJSON } from '../contract.js'
import { manifestFor, registry } from './registry.js'
import { connectionStatus } from './portCompat.js'
import { pipeId, validateGraph } from './graphDoc.js'
import CodeTab, { SettingField, SettingsJSON } from '../code/CodeTab.jsx'
import { placement } from '../runtime/placement.js'
import { sentence } from '../honesty.js'

export default function BlockPanel({ block, document, onSettings, onConnect, onDisconnect, entries = registry, upstream = {}, evidence = null, codeEvidence }) {
  const manifest = manifestFor(block, entries)
  const targetPlacement = placement(manifest)
  const [tab, setTab] = useState('Setup')
  const [draft, setDraft] = useState(block.settings)
  const [json, setJson] = useState(canonicalJSON(block.settings))
  const [message, setMessage] = useState('')
  const [test, setTest] = useState(null)
  const [source, setSource] = useState('')
  const [target, setTarget] = useState('')
  const settingsKey = `${block.id}:${canonicalJSON(block.settings)}`
  const previousSettings = useRef(settingsKey)
  const dirty = useRef(false)
  useEffect(() => {
    if (previousSettings.current === settingsKey) return
    previousSettings.current = settingsKey
    setDraft(block.settings)
    setJson(canonicalJSON(block.settings))
    setMessage(dirty.current ? 'Newer block settings replaced unsaved local edits.' : '')
    dirty.current = false
  }, [settingsKey, block.settings])
  const properties = manifest.settings_schema.properties || {}
  const order = [...new Set([...(manifest.ui_schema.order || []).filter(name => Object.hasOwn(properties, name)), ...Object.keys(properties)])]
  const applySettings = value => { onSettings(value); setDraft(value); setJson(canonicalJSON(value)); dirty.current = false }
  const save = value => {
    try { applySettings(value); setMessage('Settings saved') }
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
          required={manifest.settings_schema.required?.includes(name)} value={draft[name]} onEdit={() => { dirty.current = true }} onChange={value => {
            const next = { ...draft }; if (value === undefined) delete next[name]; else next[name] = value
            setDraft(next)
            dirty.current = true
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
        <details><summary>Submit settings JSON</summary>
          <SettingsJSON value={json} onChange={value => { setJson(value); dirty.current = true }} onApply={() => { try { save(JSON.parse(json)) } catch (error) { setMessage(error.message) } }} />
        </details>
      </>}
      {tab === 'Test' && <>
        <button type="button" onClick={() => setTest(validateGraph(document, entries))}>Validate graph</button>
        {test && <p role="status">{test.message} <span className="leaf-timing">{test.ms.toFixed(2)} ms</span></p>}
        <p>{({ here: 'Runs here as you edit and on the Lab server when you press Run.',
          browser: sentence('browser_only_block'), server: 'Runs on the Lab server when you press Run.',
          bot: sentence('runs_on_bot'), host: 'Reads from the host.' })[targetPlacement]}</p>
        <h3>Data out</h3>
        {Object.keys(manifest.outputs).map(port => <div key={port}>
          <p>{port}</p><pre>{upstream[`${block.id}/${port}`] != null ? canonicalJSON(upstream[`${block.id}/${port}`])
            : targetPlacement === 'bot' ? sentence('runs_on_bot') : 'No value'}</pre>
        </div>)}
        <h3>Declared sample, not a measurement</h3><pre>{canonicalJSON(manifest.sample_output)}</pre>
        {manifest.version.code_sha256 === '0'.repeat(64) && <p className="lab-measurement">{sentence('placeholder_block')}</p>}
      </>}
      <div hidden={tab !== 'Code'}><CodeTab key={block.id} block={block} manifest={manifest} evidence={codeEvidence ? codeEvidence(block) : evidence} onSettings={applySettings} /></div>
    </div>
    {message && <p role="status">{message}</p>}
  </>
}
