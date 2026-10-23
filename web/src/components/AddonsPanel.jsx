import { useEffect, useRef, useState } from 'react'
import { ADDON_LABELS, getAddons, getStatement, setAddonMode } from '../addons.js'
import useExit from '../useExit.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import EscCap from './EscCap.jsx'
import './popovers.css'

const MODES = [['managed', 'Managed'], ['byo', 'Your own'], ['off', 'Off']]

function AddonRow({ orgId, addon, setting }) {
  const { label, description } = ADDON_LABELS[addon]
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
      setSaved(true)
    } catch (failure) {
      setError(failure.message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <section className="ca-account" aria-label={label}>
      <div className="ca-account-main"><b>{label}</b></div>
      <p className="ca-copy">{description}</p>
      <div className="ca-choice" role="radiogroup" aria-label={`${label} mode`}>
        {MODES.map(([value, text]) => (
          <button key={value} type="button" role="radio" aria-checked={mode === value}
            className={`ca-opt${mode === value ? ' on' : ''}`} disabled={busy}
            onClick={() => { setMode(value); setSaved(false) }}>{text}</button>
        ))}
      </div>
      {mode === 'byo' && (
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
      <button type="button" className="btn ca-act" onClick={save} disabled={busy || (mode === 'byo' && !name.trim())}>
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
  useEffect(() => {
    let active = true
    setState({ loading: true })
    const period = new Date().toISOString().slice(0, 7)
    Promise.all([getAddons(orgId), getStatement(orgId, period)])
      .then(([addons, statement]) => { if (active) setState({ addons, statement }) })
      .catch((error) => { if (active) setState({ error: error.message }) })
    return () => { active = false }
  }, [orgId, attempt])
  if (state.loading) return <p className="ca-copy" role="status">Loading add-ons...</p>
  if (state.error) return (
    <div className="pane-fail">
      <p className="field-err" role="alert">{state.error}</p>
      <button type="button" className="chip-act" onClick={() => setAttempt((value) => value + 1)}>Retry</button>
    </div>
  )
  const lines = (state.statement?.lines || []).filter((line) => line.mode === 'managed')
  const totals = state.statement?.totals
  return (
    <>
      <div className="ca-account-list">
        {Object.keys(ADDON_LABELS).map((addon) => <AddonRow key={addon} orgId={orgId} addon={addon} setting={state.addons?.[addon]} />)}
      </div>
      <section aria-label="This month" className="ca-linked">
        <h3 className="ca-kindline">This month</h3>
        {state.statement?.placeholder && <p className="ca-note">Prices are placeholders while pricing is set</p>}
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
          <AddonsContent key={orgId} orgId={orgId} />
        </div>
      )}
    </span>
  )
}
