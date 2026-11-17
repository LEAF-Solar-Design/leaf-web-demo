import { useEffect, useId, useRef, useState } from 'react'

const valid = row => row && typeof row.block === 'string' && row.block && typeof row.layer === 'string'
  && row.layer && Number.isSafeInteger(row.count) && row.count >= 0
  && Array.isArray(row.design_window ?? row.insertion_bounds)
  && (row.design_window ?? row.insertion_bounds).length === 4
  && (row.design_window ?? row.insertion_bounds).every(Number.isFinite)
  && (row.design_window ?? row.insertion_bounds)[0] <= (row.design_window ?? row.insertion_bounds)[2]
  && (row.design_window ?? row.insertion_bounds)[1] <= (row.design_window ?? row.insertion_bounds)[3]
const identity = row => JSON.stringify([row.block, row.layer])

// Data and persistence are injected. A resolved callback confirms once; rejection
// preserves the chosen set for retry. Cancellation supplies an aborted signal.
export default function X07RoleConfirmation({ proposal, onConfirm, onCancel }) {
  const rows = Array.isArray(proposal?.module?.candidates) ? proposal.module.candidates.filter(valid) : []
  const first = proposal?.module?.first_choice
  const initial = Math.max(0, rows.findIndex(row => row.block === first?.block && row.layer === first?.layer))
  return <Confirmation key={JSON.stringify([proposal?.id, [...rows].sort((a,b) => identity(a).localeCompare(identity(b)))])} rows={rows} initial={initial}
    onConfirm={onConfirm} onCancel={onCancel} />
}

function Confirmation({ rows, initial, onConfirm, onCancel }) {
  const radioName = useId()
  const [selected, select] = useState(rows[initial] ? identity(rows[initial]) : null)
  const selectedRow = rows.find(row => identity(row) === selected)
  const [busy, setBusy] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [cancelled, setCancelled] = useState(false)
  const [error, setError] = useState(null)
  const mounted = useRef(true), pending = useRef(null), retired = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; pending.current?.abort() }
  }, [])
  const submit = event => {
    event.preventDefault()
    event.stopPropagation()
    if (pending.current || confirmed || retired.current || !selectedRow || typeof onConfirm !== 'function') return
    const controller = new AbortController()
    pending.current = controller
    setBusy(true)
    setError(null)
    const row = selectedRow
    const selection = { block: row.block, layer: row.layer, count: row.count,
      design_window: [...(row.design_window ?? row.insertion_bounds)] }
    const current = () => mounted.current && !retired.current && pending.current === controller
    Promise.resolve().then(() => current() ? onConfirm(selection, { signal: controller.signal }) : undefined).then(result => {
      if (!current()) return
      if (result?.ok === false) throw new Error(typeof result.message === 'string' ? result.message : 'Roles could not be saved.')
      setConfirmed(true)
    }).catch(reason => {
      if (current()) setError(reason instanceof Error && reason.message ? reason.message.slice(0, 500) : 'Roles could not be saved. Try again.')
    }).finally(() => {
      if (current()) { pending.current = null; setBusy(false) }
    })
  }
  const cancel = () => {
    if (confirmed || retired.current) return
    retired.current = true
    pending.current?.abort()
    pending.current = null
    setCancelled(true)
    setBusy(false)
    onCancel?.()
  }
  if (cancelled) return <p role="status">Role confirmation cancelled.</p>
  if (confirmed) return <p role="status">Roles confirmed.</p>
  return <form onSubmit={submit} aria-label="Confirm drawing roles">
    <fieldset disabled={busy}><legend>Module set</legend>
      {rows.length > 1 && <p>Choose the module set for this design.</p>}
      {rows.map(row => <label key={identity(row)}>
        <input type="radio" name={radioName} checked={selected === identity(row)} onChange={() => select(identity(row))} />
        {row.block} on {row.layer}, {row.count} inserts
      </label>)}
      {!rows.length && <p>No module set is available.</p>}
    </fieldset>
    {error && <p role="alert">{error}</p>}
    <button type="submit" disabled={busy || !rows.length || typeof onConfirm !== 'function'}>{busy ? 'Saving roles…' : 'Confirm roles'}</button>
    <button type="button" onClick={cancel}>Cancel</button>
  </form>
}
