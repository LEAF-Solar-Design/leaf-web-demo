import { useState } from 'react'

export default function ModuleSetPicker({ data, drawingKey, onSelect, running = false }) {
  const [selection, setSelection] = useState({ drawingKey, choices: [] })
  const rows = Array.isArray(data?.sets) ? data.sets : []
  const ids = rows.map((row) => row?.set_id)
  const unavailable = !Array.isArray(data?.sets) || ids.some((id) => typeof id !== 'string' || !id.trim())
    || new Set(ids).size !== ids.length
  const sets = unavailable ? [] : rows
  const available = new Set(sets.map((set) => set.set_id))
  const choices = selection.drawingKey === drawingKey ? selection.choices.filter((id) => available.has(id)) : []
  if (selection.drawingKey !== drawingKey || choices.length !== selection.choices.length) {
    setSelection({ drawingKey, choices })
  }
  const selected = sets.filter((set) => choices.includes(set.set_id))
  return <fieldset disabled={unavailable || running || !onSelect}>
    <legend>Module set</legend>
    <p>Choose the intended design set. These candidates come from this drawing.</p>
    <ul>
      {sets.map((set) => <li key={set.set_id}><label>
        <input type="checkbox" checked={choices.includes(set.set_id)}
          onChange={(event) => {
            if (!running && onSelect) setSelection({ drawingKey, choices: event.target.checked
              ? [...choices, set.set_id] : choices.filter((id) => id !== set.set_id) })
          }} />
        {set.layer}{set.block ? ` / ${set.block}` : ' / closed four-corner shapes'}: {set.count} candidates
      </label></li>)}
    </ul>
    <button type="button" className="chip-act" disabled={unavailable || !selected.length || running || !onSelect}
      onClick={() => { if (!unavailable && selected.length && !running && onSelect) onSelect(selected.map((set) => set.set_id)) }}>
      Count selected set
    </button>
    {unavailable ? <p>Module sets unavailable. Run the module tool again to refresh valid, unique set IDs.</p>
      : !sets.length && <p>No module candidates were found.</p>}
    {!onSelect && <p>Run the module tool on the current drawing to choose a set.</p>}
    {data?.selected_sets?.length > 0 && <p>Counted {data.count} modules on {data.selected_sets.map((set) =>
      `${set.layer}${set.block ? ` / ${set.block}` : ''}`).join(', ')}.</p>}
  </fieldset>
}
