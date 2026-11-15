import { useId, useState } from 'react'

export default function ModuleSetPicker({ data, onSelect, running = false }) {
  const id = useId()
  const [choice, setChoice] = useState('')
  const sets = Array.isArray(data?.sets) ? data.sets : []
  const selected = sets.find((set) => set.set_id === choice)
  return <fieldset disabled={running || !onSelect}>
    <legend>Module set</legend>
    <p>Choose the intended design set. These candidates come from this drawing.</p>
    <label htmlFor={id}>Design set</label>
    <select id={id} value={selected ? choice : ''} onChange={(event) => setChoice(event.target.value)}>
      <option value="">Choose a set</option>
      {sets.map((set) => <option key={set.set_id} value={set.set_id}>
        {set.layer}{set.block ? ` / ${set.block}` : ' / closed four-corner shapes'}: {set.count} candidates
      </option>)}
    </select>
    <button type="button" className="chip-act" disabled={!selected || running || !onSelect}
      onClick={() => { if (selected && !running && onSelect) onSelect(selected.set_id) }}>
      Count selected set
    </button>
    {!sets.length && <p>No module candidates were found.</p>}
    {!onSelect && <p>Run the module tool on the current drawing to choose a set.</p>}
    {data?.selected_set && <p>Counted {data.count} modules on {data.selected_set.layer}
      {data.selected_set.block ? ` / ${data.selected_set.block}` : ''}.</p>}
  </fieldset>
}
