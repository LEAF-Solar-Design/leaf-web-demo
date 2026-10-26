import { useEffect, useRef, useState } from 'react'
import { ADDON_LABELS, getAddons, getStatement, setAddonMode } from '../addons.js'
import useExit from '../useExit.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import EscCap from './EscCap.jsx'
import { humanizeError } from '../errorHumanize.js'
import './popovers.css'

const MODES = [['managed', 'Managed'], ['byo', 'Your own'], ['off', 'Off']]

function AddonRow({ orgId, addon, setting }) {
  const { label, description } = ADDON_LABELS[addon]
  const isRunner = addon === 'kashbot_runner'
  const [mode, setMode] = useState(setting?.mode || 'off')
  const [name, setName] = useState(setting?.byo_ref || '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const save = async () => {
    if (busy) return
    setBusy(true)
    setError('')
    setSaved(false)
    try {
      await setAddonMode(orgId, addon, mode, mode === 'byo' ? name : undefined)
      if (isRunner && mode === 'off') setName('')
      setSaved(true)
    } catch (failure) {
      setError(humanizeError(failure))
    } finally {
      setBusy(false)
    }
  }
  return (
    <section className="ca-account" aria-label={label}>
      <div className="ca-account-main"><b>{label}</b></div>
      <p className="ca-copy">{description}</p>
      <div className="ca-choice" role="radiogroup" aria-label={`${label} mode`}>
        {MODES.filter(([value]) => !isRunner || value !== 'managed').map(([value, text]) => (
          <button key={value} type="button" role="radio" aria-checked={mode === value}
            className={`ca-opt${mode === value ? ' on' : ''}`} disabled={busy || (isRunner && value === 'byo' && !name)}
            onClick={() => { setMode(value); setSaved(false) }}>{text}</button>
        ))}
      </div>
      {isRunner && (name
        ? <p className="ca-note">Runner reference: <code>{name}</code></p>
        : <p className="ca-note">Link a runner to use Your own.</p>)}
      {mode === 'byo' && !isRunner && (
        <>
          <label className="ca-field">
            <span className="ca-field-k">Credential name for {label}</span>
            <input type="text" value={name} disabled={busy} autoComplete="off" spellCheck={false} maxLength={128}
              placeholder="credential name, for example my-e2b-key" aria-describedby={`${addon}-help`}
              onChange={(event) => { setName(event.target.value); setSaved(false) }} />
          </label>
          <p className="ca-note" id={`${addon}-help`}>Enter only the credential name. The value is stored in your tenant's credential store, not here.</p>
        </>
      )}
      <button type="button" className="btn ca-act" onClick={save} disabled={busy || (mode === 'byo' && (isRunner || !name.trim()))}>
        {busy ? 'Saving...' : 'Save'}
      </button>
      {saved && <p className="ca-note" role="status">Saved</p>}
      {error && <div className="field-err" role="alert">{error}</div>}
    </section>
  )
}

function AddonsContent({ orgId }) {
  const [attempt, setAttempt] = useState(0)
  const [state, setState] = useState({ loading: true })
  const [statementAttempt, setStatementAttempt] = useState(0)
  const [statementState, setStatementState] = useState({ loading: true })
  useEffect(() => {
    let active = true
    setState({ loading: true })
    getAddons(orgId)
      .then((addons) => { if (active) setState({ addons }) })
      .catch((error) => { if (active) setState({ error: humanizeError(error) }) })
    return () => { active = false }
  }, [orgId, attempt])
  useEffect(() => {
    let active = true
    setStatementState({ loading: true })
    const period = new Date().toISOString().slice(0, 7)
    getStatement(orgId, period)
      .then((statement) => { if (active) setStatementState({ statement }) })
      .catch((error) => { if (active) setStatementState({ error: humanizeError(error) }) })
    return () => { active = false }
  }, [orgId, attempt, statementAttempt])
  if (state.loading) return <p className="ca-copy" role="status">Loading add-ons...</p>
  if (state.error) return (
    <div className="pane-fail">
      <p className="field-err" role="alert">{state.error}</p>
      <button type="button" className="chip-act" onClick={() => setAttempt((value) => value + 1)}>Retry</button>
    </div>
  )
  const lines = (statementState.statement?.lines || []).filter((line) => line.mode === 'managed')
  const totals = statementState.statement?.totals
  return (
    <>
      <div className="ca-account-list">
        {Object.keys(ADDON_LABELS).filter((addon) => Object.hasOwn(state.addons, addon))
          .map((addon) => <AddonRow key={addon} orgId={orgId} addon={addon} setting={state.addons[addon]} />)}
      </div>
      <section aria-label="This month" className="ca-linked">
        <h3 className="ca-kindline">This month</h3>
        {statementState.loading ? <p className="ca-copy">Loading this month...</p> : statementState.error ? (
          <div className="pane-fail">
            <p className="field-err" role="alert">{statementState.error}</p>
            <button type="button" className="chip-act" onClick={() => setStatementAttempt((value) => value + 1)}>Retry</button>
          </div>
        ) : <>
        {statementState.statement?.placeholder && <p className="ca-note">Prices are not final yet.</p>}
        {!lines.length && <p className="ca-copy">No managed usage this month.</p>}
        <table className="ca-copy">
          <thead><tr><th>Add-on</th><th>Cost</th><th>Management fee</th><th>Total</th></tr></thead>
          <tbody>{lines.map((line, index) => (
            <tr key={`${line.addon}-${index}`}>
              <th scope="row">{ADDON_LABELS[line.addon]?.label || line.addon}</th>
              <td>${line.cost_usd}</td><td>${line.fee_usd}</td><td>${line.total_usd}</td>
            </tr>
          ))}</tbody>
          {totals && <tfoot><tr><th scope="row">Totals</th><td>${totals.cost_usd}</td><td>${totals.fee_usd}</td><td>${totals.total_usd}</td></tr></tfoot>}
        </table>
        </>}
      </section>
    </>
  )
}

export default function AddonsPanel({ mock, orgId, open, onToggle }) {
  const rootRef = useRef(null)
  const pop = useExit(open)
  useEscapeOwner('addons', !!open && !mock, () => onToggle(false), { layer: 'menu', scope: rootRef })
  useEffect(() => {
    if (!open || mock) return undefined
    const onDoc = (event) => {
      if (rootRef.current && !rootRef.current.contains(event.target)) onToggle(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open, mock, onToggle])
  if (mock) return null
  return (
    <span className="claude-acct" ref={rootRef}>
      <button type="button" className="claude-trigger" aria-expanded={open} aria-haspopup="dialog" onClick={() => onToggle(!open)}>
        <span className="ca-k">Add-ons</span>
      </button>
      {pop.shown && (
        <div className={`claude-pop${pop.exiting ? ' exit' : ''}`} role="dialog" aria-label="Add-ons"
          style={{ maxHeight: '70vh', overflowY: 'auto' }}>
          <div className="ca-head"><span>Studio Add-ons</span><EscCap onClick={() => onToggle(false)} label="Close add-ons panel" /></div>
          {orgId ? <AddonsContent key={orgId} orgId={orgId} />
            : <p className="ca-copy">Choose a workspace to manage add-ons.</p>}
        </div>
      )}
    </span>
  )
}
