import { useState } from 'react'
import { canonicalJSON } from '../contract.js'
import { contract } from '../canvas/registry.js'
import BlockChecks, { CHECKS, checkStatus } from './BlockChecks.jsx'

// Commands are displayed for an operator's terminal; they are never executed here.
const quote = value => `'${String(value).replaceAll("'", "'\\''")}'`
const identity = value => value?.library_digest || value?.crate_digest || value?.digest || null

export function SettingField({ name, schema, label, required, value, onChange }) {
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

function CopyText({ label, text }) {
  const [message, setMessage] = useState('')
  return <div><pre>{text}</pre><button type="button" onClick={async () => {
    try { await navigator.clipboard.writeText(text); setMessage('Copied') }
    catch { setMessage('Copy unavailable; select the text above.') }
  }}>Copy {label}</button>{message && <span role="status">{message}</span>}</div>
}

export function SettingsJSON({ value, onChange, onApply }) {
  return <><p>Paste settings JSON, including a Claude proposal, for validation.</p>
    <label>Settings JSON<textarea value={value} onChange={event => onChange(event.target.value)} /></label>
    <button type="button" onClick={onApply}>Apply settings JSON</button></>
}

export default function CodeTab({ block, manifest, evidence = null, onSettings }) {
  const [mode, setMode] = useState('Built-in')
  const [json, setJson] = useState(canonicalJSON(block.settings || {}))
  const [settings, setSettings] = useState(block.settings || {})
  const [message, setMessage] = useState('')
  const [discarded, setDiscarded] = useState(null)
  const [action, setAction] = useState(null)
  const [prompt, setPrompt] = useState('Size by Kelly as now, but halve it in the last 2 minutes before close.')
  const draft = evidence?.draft
  const draftDigest = identity(draft) || evidence?.draftDigest || evidence?.draft_digest || null
  const hasDraft = Boolean(draftDigest && discarded !== draftDigest)
  const evidenceDigest = evidence?.digest || evidence?.evidence_digest || null
  const checks = hasDraft ? evidence?.checks : null
  const ready = hasDraft && evidenceDigest === draftDigest && CHECKS.every(([key]) => checkStatus(checks?.[key]) === 'pass')
  const budget = manifest.latency_budget_us
  const command = (name, suffix = '') => `python -m leafhft.studio_blocks.cli ${name} --kind ${quote(manifest.id)} --version ${quote(manifest.version.semver)}${suffix}`
  const requestUse = digest => command('request-use', ` --digest ${quote(digest)}`)
  const versions = Array.isArray(evidence?.versions) ? evidence.versions : Object.entries(evidence?.versions || {}).map(([state, version]) => ({ ...version, state }))
  const prior = versions.find(version => version.state === 'prior')
  const properties = manifest.settings_schema.properties || {}
  const order = [...new Set([...(manifest.ui_schema.order || []).filter(name => Object.hasOwn(properties, name)), ...Object.keys(properties)])]
  const saveSettings = value => {
    contract.validate(value, manifest.settings_schema)
    onSettings(value)
    setSettings(value)
    setJson(canonicalJSON(value))
    setMessage('Settings saved')
  }
  const ports = entries => Object.entries(entries).map(([name, pin]) => `${name}: ${Array.isArray(pin.schema.type) ? pin.schema.type.join(' | ') : pin.schema.type} [${pin.unit}], ${pin.nullable ? 'nullable' : 'non-nullable'} (${pin.mode}); schema ${canonicalJSON(pin.schema)}`).join('\n') || '(none)'
  return <section aria-label="Block code">
    <h3>Code</h3>
    <div role="group" aria-label="How it runs">{['Built-in', 'Settings only', 'Custom Rust'].map(name =>
      <button type="button" key={name} aria-pressed={mode === name} onClick={() => setMode(name)}>{name}</button>)}</div>
    <h4>Typed contract</h4>
    <pre>{`in: ${ports(manifest.inputs)}\nout: ${ports(manifest.outputs)}\nbudget: p50 at most ${budget.p50} µs · p99 at most ${budget.p99} µs`}</pre>
    <p>Runs inside the bot (native module through PyO3). Takes effect at the next restart with owner approval.</p>
    <label>Ask Claude<textarea value={prompt} onChange={event => setPrompt(event.target.value)} /></label>
    <CopyText label="Ask Claude prompt" text={`${prompt}\n\nBlock ${manifest.id}@${manifest.version.semver}\n${canonicalJSON(manifest)}`} />
    <p>Copy this prompt to your own Claude session. No model calls or host data uploads happen here.</p>
    <h4>Terminal commands</h4>
    {['export-contract', 'validate-settings', 'new-rust', 'check'].map(name =>
      <CopyText key={name} label={name} text={command(name)} />)}
    {mode === 'Built-in' && <p>Uses the registered built-in implementation.</p>}
    {mode === 'Settings only' && <>
      <details><summary>Settings schema and UI schema</summary><pre>{canonicalJSON(manifest.settings_schema)}</pre><pre>{canonicalJSON(manifest.ui_schema)}</pre></details>
      <form onSubmit={event => {
        event.preventDefault()
        try { saveSettings(settings) } catch (error) { setMessage(error.message) }
      }}>
        {order.map(name => <SettingField key={name} name={name} schema={properties[name]} label={manifest.ui_schema.labels?.[name]}
          required={manifest.settings_schema.required?.includes(name)} value={settings[name]} onChange={value => {
            const next = { ...settings }; if (value === undefined) delete next[name]; else next[name] = value
            setSettings(next)
          }} />)}
        {!order.length && <p>No settings required.</p>}<button type="submit">Save settings</button>
      </form>
      <SettingsJSON value={json} onChange={setJson} onApply={() => {
        try {
          saveSettings(JSON.parse(json))
        } catch (error) { setMessage(error.message) }
      }} />
    </>}
    {mode === 'Custom Rust' && <p>{hasDraft ? `Draft: ${draft?.crate || 'native library'} · ${draftDigest}` : 'No local Rust draft. Awaiting host evidence.'}</p>}
    <BlockChecks checks={checks && { ...checks, speed: checks.speed && { ...checks.speed, budget } }} />
    {!evidence && <p>Awaiting host evidence. No checks have been run in this page.</p>}
    <button type="button" disabled={!ready} onClick={() => setAction({ type: 'use', digest: draftDigest })}>Use as a Change</button>
    <button type="button" disabled={!hasDraft} onClick={() => { setDiscarded(draftDigest); setAction(null); setMessage('Draft discarded') }}>Discard draft</button>
    {action?.type === 'use' && ready && action.digest === draftDigest && <CopyText label="Use as a Change command" text={requestUse(action.digest)} />}
    <h4>Version history</h4>
    {!versions.length && <p>No version history supplied by the host.</p>}
    <ul>{versions.map((version, index) => <li key={`${version.state}-${index}`}>
      {version.version || identity(version)} {version.kind} · {({ active: 'live', pending: 'pending restart', prior: 'previous' })[version.state] || version.state}
      {version.state === 'pending' && <p>Takes effect at the next restart</p>}
    </li>)}</ul>
    {identity(prior) && <><button type="button" onClick={() => setAction({ type: 'undo', digest: identity(prior) })}>Undo at restart</button>
      {action?.type === 'undo' && action.digest === identity(prior) && <CopyText label="prior version digest" text={`Prior version for owner-approved undo at restart: ${action.digest}`} />}</>}
    {message && <p role="status">{message}</p>}
  </section>
}
