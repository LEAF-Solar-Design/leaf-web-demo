import { useEffect, useRef, useState } from 'react'
import { manifests } from './registry.js'
import { blockLabel, ELEMENT_FAMILIES, LANES, laneLabel, strategySection } from './elementCatalog.js'

export const BLOCK_MIME = 'application/leaf-lab-block'
export default function Palette({ onAdd }) {
  const [query, setQuery] = useState('')
  const [groupBy, setGroupBy] = useState('family')
  const search = useRef(null)
  useEffect(() => {
    const slash = event => {
      if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey ||
        event.target.closest?.('input,textarea,select,[contenteditable="true"]')) return
      const region = search.current?.closest('[data-canvas-editor]')
      if (!region || region.closest('[hidden]')) return
      event.preventDefault(); search.current.focus()
    }
    window.addEventListener('keydown', slash)
    return () => window.removeEventListener('keydown', slash)
  }, [])
  const shown = manifests.filter(manifest => `${blockLabel(manifest)} ${laneLabel(manifest.lane)} ${strategySection(manifest) || ''}`.toLowerCase().includes(query.toLowerCase()))
  const entry = manifest => <button type="button" draggable key={manifest.id} onClick={() => onAdd(manifest)}
    onDragStart={event => { event.dataTransfer.setData(BLOCK_MIME, manifest.id); event.dataTransfer.effectAllowed = 'copy' }}>
    {blockLabel(manifest)}
  </button>
  const families = items => ELEMENT_FAMILIES.flatMap(family => {
    const variants = items.filter(manifest => family.variants.includes(manifest.id))
    return variants.length ? [<div key={family.id} data-element-family={family.id}>
      <h4>{variants[0].title}</h4>{variants.map(entry)}
    </div>] : []
  })
  return <section className="lab-palette" aria-label="Strategy palette">
    <h2>Strategy palette</h2><p>Strategy decides when to buy. Sizing and Cash out stay in their own Lanes.</p>
    <label>Search blocks (/)<input ref={search} type="search" value={query} onChange={event => setQuery(event.target.value)} /></label>
    <label>Group blocks<select value={groupBy} onChange={event => setGroupBy(event.target.value)}>
      <option value="family">Element families</option><option value="lane">Nine Lanes</option>
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
