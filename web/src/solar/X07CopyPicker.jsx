import { useEffect, useRef, useState } from 'react'

function valid(row) {
  const b = row?.design_window
  return row && typeof row.block === 'string' && row.block && typeof row.layer === 'string' && row.layer
    && Number.isSafeInteger(row.oracle_count) && row.oracle_count >= 0
    && Array.isArray(b) && b.length === 4 && b.every(Number.isFinite) && b[0] <= b[2] && b[1] <= b[3]
}

// oracle_count is caller-supplied independent evidence, never a product count.
export default function X07CopyPicker({ copies, onPick, onCancel }) {
  const rows = Array.isArray(copies) ? copies : []
  const invalid = !Array.isArray(copies) || rows.some(row => !valid(row)) || rows.some(row =>
    row.block.toLowerCase() !== rows[0].block.toLowerCase() || row.layer.toLowerCase() !== rows[0].layer.toLowerCase())
  return <Picker key={JSON.stringify(rows)} rows={rows} invalid={invalid} onPick={onPick} onCancel={onCancel} />
}

function Picker({ rows, invalid, onPick, onCancel }) {
  const [state, setState] = useState('idle'), [error, setError] = useState(false)
  const active = useRef(true), pending = useRef(null), finished = useRef(false)
  useEffect(() => { active.current = true; return () => { active.current = false; pending.current?.abort() } }, [])
  const pick = row => {
    if (invalid || finished.current || pending.current || typeof onPick !== 'function') return
    const controller = new AbortController()
    pending.current = controller
    setState('saving'); setError(false)
    const current = () => active.current && !finished.current && pending.current === controller
    Promise.resolve().then(() => current() ? onPick({ block: row.block, layer: row.layer,
      design_window: [...row.design_window], oracle_count: row.oracle_count }, { signal: controller.signal }) : undefined)
      .then(() => { if (current()) { finished.current = true; setState('selected') } })
      .catch(() => { if (current()) { pending.current = null; setState('idle'); setError(true) } })
  }
  const cancel = () => {
    if (finished.current) return
    finished.current = true; pending.current?.abort(); setState('cancelled'); onCancel?.()
  }
  if (state === 'selected') return <p role="status">Design copy selected.</p>
  if (state === 'cancelled') return <p role="status">Copy choice cancelled.</p>
  return <section aria-label="Choose design copy">
    {invalid ? <p role="alert">Copy bounds or independent counts are invalid.</p>
      : !rows.length ? <p>No design copies are available.</p>
        : <table><caption>Copies of {rows[0].block} on {rows[0].layer}</caption>
          <thead><tr><th>Location</th><th>Independent count</th><th>Choice</th></tr></thead>
          <tbody>{rows.map((row, index) => <tr key={index}>
            <td>{row.design_window.map(value => value.toFixed(2)).join(', ')}</td><td>{row.oracle_count}</td>
            <td><button type="button" disabled={state === 'saving' || typeof onPick !== 'function'} onClick={() => pick(row)}>Use copy {index + 1}</button></td>
          </tr>)}</tbody></table>}
    {error && <p role="alert">Copy choice could not be saved. Try again.</p>}
    <button type="button" onClick={cancel}>Cancel</button>
  </section>
}
