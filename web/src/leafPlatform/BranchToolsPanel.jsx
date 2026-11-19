import React, { useEffect, useRef, useState } from 'react'
import { buildFormModel, describeDecision, formArgs } from './branchTools.js'

const fieldStyle = { boxSizing: 'border-box', width: '100%', minWidth: 0, minHeight: 44,
  padding: '9px 12px', border: '1px solid var(--leaf-ink-muted)', borderRadius: 'var(--leaf-radius-control)',
  background: 'var(--leaf-surface)', color: 'var(--leaf-ink)', font: 'inherit' }

function ToolForm({ bridge, tool, busy, onBusy }) {
  const [model] = useState(() => buildFormModel(tool.params ?? tool.manifest?.params))
  const [values, setValues] = useState(() => Object.fromEntries(model.map((field) => [field.name, field.initial])))
  const [outcome, setOutcome] = useState(null)
  const [fieldError, setFieldError] = useState(null)
  const inFlight = useRef(false)
  const active = useRef(true)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  async function run(event) {
    event.preventDefault()
    if (inFlight.current || busy) return
    setFieldError(null)
    setOutcome(null)
    let args
    try { args = formArgs(model, values) } catch (error) {
      if (error.field) setFieldError(error)
      else setOutcome({ reason: error.message })
      return
    }
    const operation = {}
    inFlight.current = operation
    onBusy(operation)
    try {
      const result = await bridge.runTool({ name: tool.name, version: tool.version, packageDigest: tool.packageDigest, args })
      if (active.current) setOutcome(result.decision === 'accept'
        ? { result: JSON.stringify(result.result, null, 2), truncated: result.resultTruncated }
        : { reason: result.reason })
    } catch (error) {
      if (active.current) setOutcome({ reason: error.message })
    } finally {
      inFlight.current = false
      onBusy((current) => current === operation ? false : current)
    }
  }
  function update(field, value) { setValues((previous) => ({ ...previous, [field.name]: value })) }
  return <article>
    <h3>{tool.name} <small>{tool.version}</small></h3>
    <p>{describeDecision(tool.decision)}</p>
    <form onSubmit={run} style={{ display: 'grid', gap: 14 }}>
      {model.map((field) => <label key={field.name}>
        {field.label}
        {field.type === 'boolean' ? <input type="checkbox" checked={values[field.name]} disabled={busy} onChange={(event) => update(field, event.target.checked)} />
          : field.type === 'enum' ? <select value={field.options.findIndex((option) => Object.is(option, values[field.name]))} disabled={busy}
            onChange={(event) => update(field, field.options[Number(event.target.value)])}>
            {field.options.map((option, index) => <option key={index} value={index}>{JSON.stringify(option)}</option>)}
          </select>
            : field.type === 'json' ? <textarea style={fieldStyle} value={values[field.name]} disabled={busy} required={field.required} onChange={(event) => update(field, event.target.value)} />
              : <input type={['integer', 'number'].includes(field.type) ? 'number' : 'text'} step={field.type === 'integer' ? '1' : 'any'}
                style={fieldStyle} value={values[field.name]} required={field.required} disabled={busy} onChange={(event) => update(field, event.target.value)} />}
        {fieldError?.field === field.name && <span role="alert">{fieldError.message}</span>}
      </label>)}
      <button type="submit" disabled={busy || !tool.runnable || tool.decision !== 'accept'}>Run {tool.name}</button>
    </form>
    {outcome && <div role="status">{outcome.reason ? describeDecision(outcome.reason) : <>
      <pre style={{ maxHeight: 320, overflow: 'auto' }}>{outcome.result}</pre>{outcome.truncated && <p>The result was truncated by the host.</p>}
    </>}</div>}
  </article>
}

export default function BranchToolsPanel({ bridge, working, setWorking }) {
  const [tools, setTools] = useState([])
  const [error, setError] = useState(null)
  const [localWorking, setLocalWorking] = useState(false)
  const busy = working ?? localWorking
  const setBusy = setWorking ?? setLocalWorking
  const [loadRequested, setLoadRequested] = useState(true)
  const [attempt, setAttempt] = useState(0)
  const active = useRef(false)
  const listing = useRef(false)
  useEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [bridge])
  useEffect(() => {
    if (busy || !loadRequested || listing.current) return
    const operation = {}
    listing.current = operation
    setLoadRequested(false)
    setBusy(operation)
    setError(null)
    setTools([])
    Promise.resolve().then(() => bridge.listTools()).then((result) => {
      if (active.current) setTools(result.tools)
    }).catch((failure) => { if (active.current) setError(failure.message) })
      .finally(() => {
        listing.current = false
        setBusy((current) => current === operation ? false : current)
      })
  }, [bridge, busy, loadRequested, setBusy])
  return <section aria-label="Branch tools">
    <h2>Branch tools</h2>
    <button type="button" disabled={busy || loadRequested} onClick={() => { setAttempt((value) => value + 1); setLoadRequested(true) }}>Refresh tools</button>
    {error && <p role="status">{describeDecision(error)}</p>}
    {busy && <p role="status">Waiting for AutoCAD…</p>}
    {!busy && !error && tools.length === 0 && <p>No tools are installed.</p>}
    {tools.map((tool) => <ToolForm key={`${attempt}:${tool.name}:${tool.version}:${tool.packageDigest}`} bridge={bridge} tool={tool} busy={busy} onBusy={setBusy} />)}
  </section>
}
