import { useId, useState } from 'react'

const validCount = (count) => Number.isSafeInteger(count) && count >= 0

export default function ModuleSetPicker({ data, drawingKey, onSelect, running = false }) {
  const pickerId = useId()
  // Fixed-width UTF-16 units preserve opaque IDs, including lone surrogates.
  const inputId = (id) => `${pickerId}-set-${id.split('').map((unit) =>
    unit.charCodeAt(0).toString(16).padStart(4, '0')).join('')}`
  const [selection, setSelection] = useState({ drawingKey, choices: [] })
  const [submitError, setSubmitError] = useState(null)
  if (submitError && submitError.drawingKey !== drawingKey) setSubmitError(null)
  const hasSummary = data?.selected_sets !== undefined && (!Array.isArray(data.selected_sets) || data.selected_sets.length > 0)
  // App only supplies this callback when the result origin is the current drawing.
  const staleResult = hasSummary && !onSelect
  const rows = Array.isArray(data?.sets) && !staleResult ? data.sets : []
  const ids = rows.map((row) => row?.set_id)
  const unavailable = !Array.isArray(data?.sets) || ids.some((id) => typeof id !== 'string' || !id.trim())
    || new Set(ids).size !== ids.length || rows.some((row) => typeof row?.layer !== 'string'
      || (row.block !== undefined && typeof row.block !== 'string') || !validCount(row.count))
  const sets = unavailable ? [] : rows
  const available = new Set(sets.map((set) => set.set_id))
  const candidateById = new Map(sets.map((set) => [set.set_id, set]))
  const choices = selection.drawingKey === drawingKey ? selection.choices.filter((id) => available.has(id)) : []
  if (selection.drawingKey !== drawingKey || choices.length !== selection.choices.length) {
    setSelection({ drawingKey, choices })
  }
  const selected = sets.filter((set) => choices.includes(set.set_id))
  const summaryIds = Array.isArray(data?.selected_sets) ? data.selected_sets.map((set) => set?.set_id) : []
  const validSummary = Array.isArray(data?.selected_sets) && data.selected_sets.length > 0
    && !unavailable && new Set(summaryIds).size === summaryIds.length
    && validCount(data.count) && data.selected_sets.every((set) => set !== null && typeof set === 'object'
      && typeof set.set_id === 'string' && available.has(set.set_id) && typeof set.layer === 'string'
      && typeof set.block === 'string' && validCount(set.count)
      && set.count === candidateById.get(set.set_id).count && set.count <= data.count
      && set.layer === candidateById.get(set.set_id).layer
      && set.block === (candidateById.get(set.set_id).block || ''))
    && data.count <= data.selected_sets.reduce((sum, set) => sum + set.count, 0)
  const submitSelection = () => {
    if (unavailable || !selected.length || running || !onSelect) return
    try {
      onSelect(selected.map((set) => set.set_id))
      setSubmitError(null)
    } catch {
      setSubmitError({ drawingKey, message: 'Unable to start module count. Try counting the selected set again.' })
    }
  }
  return <fieldset disabled={unavailable || running || !onSelect}>
    <legend>Module set</legend>
    <p>Choose the intended design set. These candidates come from this drawing.</p>
    <ul>
      {sets.map((set) => <li key={set.set_id}><label htmlFor={inputId(set.set_id)}>
        <input id={inputId(set.set_id)} type="checkbox" checked={choices.includes(set.set_id)}
          onChange={(event) => {
            if (!running && onSelect) {
              setSubmitError(null)
              setSelection({ drawingKey, choices: event.target.checked
                ? [...choices, set.set_id] : choices.filter((id) => id !== set.set_id) })
            }
          }} />
        {set.layer}{set.block ? ` / ${set.block}` : ' / closed four-corner shapes'}: {set.count} candidates
      </label></li>)}
    </ul>
    <button type="button" className="chip-act" disabled={unavailable || !selected.length || running || !onSelect}
      onClick={submitSelection}>
      Count selected set
    </button>
    <div role="status" aria-live="polite" aria-atomic="true">
    {running && <p>Counting modules.</p>}
    {unavailable ? <p>Module sets unavailable. Run the module tool again to refresh valid, unique set IDs.</p>
      : !staleResult && !sets.length && <p>No module candidates were found.</p>}
    {!onSelect && <p>Run the module tool on the current drawing to choose a set.</p>}
    {hasSummary && !staleResult && !running && (validSummary ? <p>Counted {data.count} modules on {data.selected_sets.map((set) =>
      `${set.layer}${set.block ? ` / ${set.block}` : ''}`).join(', ')}.</p>
      : <p>Count summary unavailable. Run the module tool again to refresh the result.</p>)}
    </div>
    {submitError && submitError.drawingKey === drawingKey && <p role="alert">{submitError.message}</p>}
  </fieldset>
}
