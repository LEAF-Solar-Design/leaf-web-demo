import { useId, useState } from 'react'

export default function ModuleSetPicker({ data, onSelect, running = false }) {
  const id = useId()
  const [choices, setChoices] = useState([])
  const sets = Array.isArray(data?.sets) ? data.sets : []
  const selected = sets.filter((set) => choices.includes(set.set_id))
  return <fieldset disabled={running || !onSelect}>
    <legend>Module set</legend>
    <p>Choose the intended design set. These candidates come from this drawing.</p>
    <label htmlFor={id}>Design set</label>
    <select id={id} multiple value={selected.map((set) => set.set_id)}
      onChange={(event) => setChoices(Array.from(event.target.selectedOptions, (option) => option.value))}>
      {sets.map((set) => <option key={set.set_id} value={set.set_id}>
        {set.layer}{set.block ? ` / ${set.block}` : ' / closed four-corner shapes'}: {set.count} candidates
      </option>)}
    </select>
    <button type="button" className="chip-act" disabled={!selected.length || running || !onSelect}
      onClick={() => { if (selected.length && !running && onSelect) onSelect(selected.map((set) => set.set_id)) }}>
      Count selected set
    </button>
    {!sets.length && <p>No module candidates were found.</p>}
    {!onSelect && <p>Run the module tool on the current drawing to choose a set.</p>}
    {data?.selected_sets?.length > 0 && <p>Counted {data.count} modules on {data.selected_sets.map((set) =>
      `${set.layer}${set.block ? ` / ${set.block}` : ''}`).join(', ')}.</p>}
  </fieldset>
}
