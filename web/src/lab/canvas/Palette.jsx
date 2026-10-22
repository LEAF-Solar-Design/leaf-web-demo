import { useState } from 'react'
import { blockLabel, ELEMENT_FAMILIES, LANES, laneLabel, strategySection } from './elementCatalog.js'

export const BLOCK_MIME = 'application/leaf-lab-block'
export default function Palette({ entries, onAdd, searchRef }) {
  const [query, setQuery] = useState('')
  const [groupBy, setGroupBy] = useState('family')
  const shown = Object.values(entries).filter(manifest => `${blockLabel(manifest)} ${laneLabel(manifest.lane)} ${strategySection(manifest) || ''}`.toLowerCase().includes(query.toLowerCase()))
  const entry = manifest => <button type="button" draggable key={manifest.id} onClick={() => onAdd(manifest)}
    onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); onAdd(manifest) } }}
    onDragStart={event => { event.dataTransfer.setData(BLOCK_MIME, manifest.id); event.dataTransfer.effectAllowed = 'copy' }}>
    {blockLabel(manifest)}
  </button>
  const families = items => {
    const known = new Set(ELEMENT_FAMILIES.flatMap(family => family.variants))
    return [...ELEMENT_FAMILIES, ...items.filter(item => !known.has(item.id)).map(item => ({ id: item.id, variants: [item.id] }))].flatMap(family => {
      const variants = items.filter(manifest => family.variants.includes(manifest.id))
      return variants.length ? [<div key={family.id} data-element-family={family.id}>
        <h4>{variants[0].title}</h4>{variants.map(entry)}
      </div>] : []
    })
  }
  return <section className="lab-palette" aria-label="Strategy palette">
    <h2>Strategy palette</h2><p>Strategy decides when to buy. Sizing and Cash out stay in their own Lanes.</p>
    <label>Search blocks (/)<input ref={searchRef} type="search" value={query} onChange={event => setQuery(event.target.value)} /></label>
    <label>Group blocks<select value={groupBy} onChange={event => setGroupBy(event.target.value)}>
      <option value="family">Blocks</option><option value="lane">Nine Lanes</option>
    </select></label>
    <p>Drag a block or press Enter on its button to add at the canvas centre.</p>
    {groupBy === 'lane' ? LANES.map(lane => <section key={lane.id} aria-label={lane.title}><h3>{lane.title}</h3>
      {shown.filter(manifest => manifest.lane === lane.id).map(entry)}
      {!shown.some(manifest => manifest.lane === lane.id) && <p>No blocks in this Lane.</p>}
    </section>) : <>
      <details open><summary>Strategy</summary>{['Signals', 'Rules'].map(section => <section key={section} aria-label={section}>
        <h3>{section}</h3>{families(shown.filter(manifest => strategySection(manifest) === section))}
      </section>)}</details>
      {families(shown.filter(manifest => manifest.lane !== 'engines'))}
    </>}
    {!shown.length && <p>No matching blocks.</p>}
  </section>
}
