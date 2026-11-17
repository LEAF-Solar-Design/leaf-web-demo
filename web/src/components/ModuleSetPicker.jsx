import { useEffect, useId, useRef, useState } from 'react'

const validCount = (count) => Number.isSafeInteger(count) && count >= 0

export default function ModuleSetPicker({ data, drawingKey, onSelect, running = false }) {
  const pickerId = useId()
  // Fixed-width UTF-16 units preserve opaque IDs, including lone surrogates.
  const inputId = (id) => `${pickerId}-set-${id.split('').map((unit) =>
    unit.charCodeAt(0).toString(16).padStart(4, '0')).join('')}`
  const [selection, setSelection] = useState({ drawingKey, choices: [], edited: false })
  const recordedRegion = data?.region ?? data?.selection_window
  const [designWindow, setDesignWindow] = useState(() => ({ drawingKey,
    values: Array.isArray(recordedRegion) ? recordedRegion.map(String) : ['', '', '', ''] }))
  const [submitError, setSubmitError] = useState(null)
  const [pendingRequest, setPendingRequest] = useState(null)
  const submitEpoch = useRef(0)
  const activeRequest = useRef(null)
  const latestDrawing = useRef(drawingKey)
  latestDrawing.current = drawingKey
  useEffect(() => () => { submitEpoch.current += 1 }, [])
  const submitting = Boolean(pendingRequest && pendingRequest.drawingKey === drawingKey)
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
    const drawingChanged = selection.drawingKey !== drawingKey
    if (drawingChanged) {
      submitEpoch.current += 1
      activeRequest.current = null
      if (pendingRequest) setPendingRequest(null)
      setDesignWindow({ drawingKey, values: ['', '', '', ''] })
    }
    setSelection({ drawingKey, choices, edited: !drawingChanged && selection.edited })
  }
  const choiceIds = new Set(choices)
  const selected = sets.filter((set) => choiceIds.has(set.set_id))
  const summaryIds = Array.isArray(data?.selected_sets) ? data.selected_sets.map((set) => set?.set_id) : []
  const summaryIdSet = new Set(summaryIds)
  const windowValues = designWindow.drawingKey === drawingKey ? designWindow.values : ['', '', '', '']
  const windowEnabled = windowValues.some((value) => value.trim() !== '')
  const bounds = windowValues.map(Number)
  const validWindow = !windowEnabled || (windowValues.every((value) => value.trim() !== '')
    && bounds.every(Number.isFinite) && bounds[0] < bounds[2] && bounds[1] < bounds[3])
  const windowMatchesSummary = JSON.stringify(windowEnabled ? bounds : null)
    === JSON.stringify(recordedRegion ?? null)
  const summaryMatchesChoices = !selection.edited || (summaryIds.length === choices.length
    && summaryIds.every((id) => choiceIds.has(id)) && windowMatchesSummary)
  const validSummary = Array.isArray(data?.selected_sets) && data.selected_sets.length > 0
    && !unavailable && summaryIdSet.size === summaryIds.length
    && validCount(data.count) && data.selected_sets.every((set) => set !== null && typeof set === 'object'
      && typeof set.set_id === 'string' && available.has(set.set_id) && typeof set.layer === 'string'
      && typeof set.block === 'string' && validCount(set.count)
      && set.count === candidateById.get(set.set_id).count && set.count <= data.count
      && set.layer === candidateById.get(set.set_id).layer
      && set.block === (candidateById.get(set.set_id).block || ''))
    && data.count <= data.selected_sets.reduce((sum, set) => sum + set.count, 0)
  const submitSelection = () => {
    if (unavailable || !validWindow || !selected.length || running || submitting || !onSelect
      || (activeRequest.current && activeRequest.current.drawingKey === drawingKey)) return
    const epoch = ++submitEpoch.current
    activeRequest.current = { drawingKey, epoch }
    const finish = (failed = false) => {
      if (submitEpoch.current !== epoch || latestDrawing.current !== drawingKey) return
      activeRequest.current = null
      setPendingRequest(null)
      setSubmitError(failed ? { drawingKey, message: 'Unable to start module count. Try counting the selected set again.' } : null)
    }
    try {
      const ids = selected.map((set) => set.set_id)
      const outcome = windowEnabled ? onSelect(ids, [...bounds]) : onSelect(ids)
      if (outcome && typeof outcome.then === 'function') {
        setPendingRequest({ drawingKey, epoch })
        Promise.resolve(outcome).then(() => finish(), () => finish(true))
      } else finish()
    } catch {
      finish(true)
    }
  }
  return <fieldset disabled={unavailable || running || submitting || !onSelect}>
    <legend>Module set</legend>
    <p>Choose the intended design set. These candidates come from this drawing.</p>
    <ul>
      {sets.map((set) => <li key={set.set_id}><label htmlFor={inputId(set.set_id)}>
        <input id={inputId(set.set_id)} type="checkbox" checked={choiceIds.has(set.set_id)}
          onChange={(event) => {
            if (!running && !submitting && onSelect) {
              setSubmitError(null)
              setSelection({ drawingKey, edited: true, choices: event.target.checked
                ? [...choices, set.set_id] : choices.filter((id) => id !== set.set_id) })
            }
          }} />
        {set.layer}{set.block ? ` / ${set.block}` : ' / closed four-corner shapes'}: {set.count} candidates
      </label></li>)}
    </ul>
    <details open={windowEnabled || undefined}>
      <summary>Design window</summary>
      <p>Enter drawing coordinates to count one design copy. Blank bounds count the whole set.</p>
      {['Minimum X', 'Minimum Y', 'Maximum X', 'Maximum Y'].map((label, index) => <label key={label}>
        {label}
        <input type="number" step="any" value={windowValues[index]}
          onChange={(event) => {
            if (running || submitting || !onSelect) return
            const values = [...windowValues]
            values[index] = event.target.value
            setDesignWindow({ drawingKey, values })
            setSelection({ drawingKey, choices, edited: true })
            setSubmitError(null)
          }} />
      </label>)}
      <p>Window counts require fully contained instances. A boundary overlapping an instance needs adjustment.</p>
    </details>
    {windowEnabled && !validWindow && <p role="alert">Enter four finite bounds with minimums below maximums.</p>}
    <button type="button" className="chip-act" disabled={unavailable || !validWindow || !selected.length || running || submitting || !onSelect}
      onClick={submitSelection}>
      Count selected set
    </button>
    <div role="status" aria-live="polite" aria-atomic="true">
    {(running || submitting) && <p>Counting modules.</p>}
    {unavailable ? <p>Module sets unavailable. Run the module tool again to refresh valid, unique set IDs.</p>
      : !staleResult && !sets.length && <p>No module candidates were found.</p>}
    {!onSelect && <p>Run the module tool on the current drawing to choose a set.</p>}
    {hasSummary && !staleResult && !running && !submitting && (validSummary ? summaryMatchesChoices ? <p>Counted {data.count} modules on {sets.filter((set) => summaryIdSet.has(set.set_id)).map((set) =>
      `${set.layer}${set.block ? ` / ${set.block}` : ''}`).join(', ')}.</p>
      : <p>Choices changed. Count the selected set to update this result.</p>
      : <p>Count summary unavailable. Run the module tool again to refresh the result.</p>)}
    </div>
    {submitError && submitError.drawingKey === drawingKey && <p role="alert">{submitError.message}</p>}
  </fieldset>
}
