import { useEffect, useId, useMemo, useRef, useState } from 'react'

const valid = row => row && typeof row.block === 'string' && row.block && typeof row.layer === 'string'
  && row.layer && Number.isSafeInteger(row.count) && row.count >= 0
  && Array.isArray(row.design_window ?? row.insertion_bounds)
  && (row.design_window ?? row.insertion_bounds).length === 4
  && (row.design_window ?? row.insertion_bounds).every(Number.isFinite)
  && (row.design_window ?? row.insertion_bounds)[0] <= (row.design_window ?? row.insertion_bounds)[2]
  && (row.design_window ?? row.insertion_bounds)[1] <= (row.design_window ?? row.insertion_bounds)[3]
const identity = row => JSON.stringify([row.block, row.layer])
const PAGE_SIZE = 10
const OUTLINE_HANDLE = /^[0-9A-F]{1,32}$/
const MAX_EXCLUSIONS = 4

// Data and persistence are injected. A resolved callback confirms once; rejection
// preserves the chosen set for retry. Cancellation supplies an aborted signal.
export default function X07RoleConfirmation({ proposal, outlineChoices, onConfirm, onCancel }) {
  const model = useMemo(() => {
    const rows = Array.isArray(proposal?.module?.candidates) ? proposal.module.candidates.filter(valid) : []
    const first = proposal?.module?.first_choice
    const initial = Math.max(0, rows.findIndex(row => row.block === first?.block && row.layer === first?.layer))
    const key = JSON.stringify([proposal?.id, [...rows].sort((a,b) => identity(a).localeCompare(identity(b)))])
    return { rows, initial, key }
  }, [proposal])
  return <Confirmation key={model.key} rows={model.rows} initial={model.initial} outlineChoices={outlineChoices}
    onConfirm={onConfirm} onCancel={onCancel} />
}

function Confirmation({ rows, initial, outlineChoices, onConfirm, onCancel }) {
  const radioName = useId()
  const outlineListId = useId()
  const [selected, select] = useState(() => rows[initial] ? identity(rows[initial]) : null)
  const [page, setPage] = useState(Math.floor(initial / PAGE_SIZE))
  const byIdentity = useMemo(() => new Map(rows.map(row => [identity(row), row])), [rows])
  const selectedRow = byIdentity.get(selected)
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE))
  const visible = rows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)
  const [busy, setBusy] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [cancelled, setCancelled] = useState(false)
  const [error, setError] = useState(null)
  const [outline, setOutline] = useState('')
  const [exclusions, setExclusions] = useState([''])
  const hasOutlineChoices = Array.isArray(outlineChoices) && outlineChoices.length > 0
  const mounted = useRef(true), pending = useRef(null), retired = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; pending.current?.abort() }
  }, [])
  const submit = event => {
    event.preventDefault()
    event.stopPropagation()
    if (pending.current || confirmed || retired.current || !selectedRow || typeof onConfirm !== 'function') return
    const include = hasOutlineChoices ? outline.trim().toUpperCase() : ''
    const exclude = hasOutlineChoices ? exclusions.map(handle => handle.trim().toUpperCase()).filter(Boolean) : []
    if ((include || exclude.length) && (!OUTLINE_HANDLE.test(include) || exclude.length > MAX_EXCLUSIONS
        || exclude.some(handle => !OUTLINE_HANDLE.test(handle))
        || new Set([include, ...exclude]).size !== exclude.length + 1)) {
      setError('Enter one design outline and up to four distinct exclusion handles.')
      return
    }
    const row = selectedRow
    const designWindow = include ? { outline_handle: include, exclude_handles: exclude }
      : [...(row.design_window ?? row.insertion_bounds)]
    const controller = new AbortController()
    pending.current = controller
    setBusy(true)
    setError(null)
    const selection = { block: row.block, layer: row.layer, count: row.count,
      design_window: designWindow }
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
      <p>{rows.length} selectable module sets</p>
      {visible.map(row => <label key={identity(row)}>
        <input type="radio" name={radioName} checked={selected === identity(row)} onChange={() => select(identity(row))} />
        {row.block} on {row.layer}, {row.count} inserts
      </label>)}
      {!rows.length && <p>No module set is available.</p>}
      {pages > 1 && <nav aria-label="Selectable module set pages">
        <button type="button" disabled={page === 0} onClick={() => setPage(value => value - 1)}>Previous choices</button>
        <span role="status">Choices page {page + 1} of {pages}</span>
        <button type="button" disabled={page + 1 === pages} onClick={() => setPage(value => value + 1)}>Next choices</button>
      </nav>}
      {selectedRow && <p>Selected module set: {selectedRow.block} on {selectedRow.layer}, {selectedRow.count} inserts</p>}
    </fieldset>
    {hasOutlineChoices && selectedRow && <fieldset disabled={busy}>
      <legend>Drawing outlines</legend>
      <label>Design outline handle
        <input list={outlineListId} value={outline} maxLength={32} onChange={event => setOutline(event.target.value)} />
      </label>
      <datalist id={outlineListId}>{outlineChoices.map(({ handle, layer }) =>
        <option key={handle} value={handle} label={layer || undefined} />)}</datalist>
      <p>Leave the design outline blank to use the module set bounds.</p>
      {exclusions.map((handle, index) => <label key={index}>Exclusion outline {index + 1}
        <input list={outlineListId} value={handle} maxLength={32} onChange={event => {
          const value = event.target.value
          setExclusions(previous => previous.map((entry, position) => position === index ? value : entry))
        }} />
      </label>)}
      {exclusions.length < MAX_EXCLUSIONS && <button type="button"
        onClick={() => setExclusions(previous => [...previous, ''])}>Add exclusion outline</button>}
    </fieldset>}
    {error && <p role="alert">{error}</p>}
    <button type="submit" disabled={busy || !rows.length || typeof onConfirm !== 'function'}>{busy ? 'Saving roles…' : 'Confirm roles'}</button>
    <button type="button" onClick={cancel}>Cancel</button>
  </form>
}
