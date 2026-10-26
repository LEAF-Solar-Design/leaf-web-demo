import { useEffect, useRef, useState } from 'react'
import { blockLabel, ELEMENT_FAMILIES, LANES, laneLabel, strategySection } from './elementCatalog.js'

export const BLOCK_MIME = 'application/leaf-lab-block'
export default function Palette({ entries, onAdd, searchRef, tools = [], onLoadTools }) {
  const [query, setQuery] = useState('')
  const [groupBy, setGroupBy] = useState('family')
  const [toolsState, setToolsState] = useState('idle')
  const toolsRequested = useRef(false)
  const active = useRef(true)
  useEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])
  const loadTools = event => {
    if (!event.currentTarget.open || toolsRequested.current) return
    toolsRequested.current = true
    setToolsState('loading')
    Promise.resolve().then(() => onLoadTools()).then(result => {
      if (active.current) setToolsState(result?.state === 'on' ? 'on' : 'unavailable')
    }).catch(() => { if (active.current) setToolsState('unavailable') })
  }
  // A tool whose id names a non-tool entry never hides it: base entries win (withTools).
  const toolManifests = new Set(tools.map(tool => tool.manifest).filter(Boolean))
  const baseIds = new Set(Object.values(entries).filter(manifest => !toolManifests.has(manifest)).map(manifest => manifest.id))
  const usableTools = tools.filter(tool => !baseIds.has(tool.id))
  const shown = Object.values(entries).filter(manifest => !toolManifests.has(manifest) && `${blockLabel(manifest)} ${laneLabel(manifest.lane)} ${strategySection(manifest) || ''}`.toLowerCase().includes(query.toLowerCase()))
  const shownTools = usableTools.filter(tool => `${tool.title} ${tool.id}`.toLowerCase().includes(query.toLowerCase()))
  const entry = (manifest, label = blockLabel(manifest)) => <button type="button" draggable key={manifest.id} onClick={() => onAdd(manifest)}
    onDragStart={event => { event.dataTransfer.setData(BLOCK_MIME, manifest.id); event.dataTransfer.effectAllowed = 'copy' }}>
    {label}
  </button>
  const families = items => {
    const known = new Set(ELEMENT_FAMILIES.flatMap(family => family.variants))
    return [...ELEMENT_FAMILIES, ...items.filter(item => !known.has(item.id)).map(item => ({ id: item.id, variants: [item.id] }))].flatMap(family => {
      const variants = items.filter(manifest => family.variants.includes(manifest.id))
      return variants.length ? [<div key={family.id} data-element-family={family.id}>
        <h4>{variants[0].title}</h4>{variants.map(manifest => entry(manifest))}
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
      {shown.filter(manifest => manifest.lane === lane.id).map(manifest => entry(manifest))}
      {!shown.some(manifest => manifest.lane === lane.id) && <p>No blocks in this Lane.</p>}
    </section>) : <>
      <details open><summary>Strategy</summary>{['Signals', 'Rules'].map(section => <section key={section} aria-label={section}>
        <h3>{section}</h3>{families(shown.filter(manifest => strategySection(manifest) === section))}
      </section>)}</details>
      {families(shown.filter(manifest => manifest.lane !== 'engines'))}
    </>}
    {onLoadTools && <details aria-label="Tools" data-palette-tools onToggle={loadTools}>
      <summary>Tools</summary>
      {toolsState === 'loading' && <p>Loading tools.</p>}
      {toolsState === 'unavailable' && <p>Tools are not available here.</p>}
      {toolsState === 'on' && <><p>Tools run on the server: save the graph and use Run.</p>
      {shownTools.map(tool => tool.available && tool.manifest ? entry(tool.manifest, tool.title)
        : <button type="button" key={`${tool.id}@${tool.version}`} disabled draggable={false}>
          {tool.title}{' '}<span>Unavailable: {tool.reason ?? (tool.manifest ? 'unavailable' : 'no manifest')}</span>
        </button>)}
      {!shownTools.length && <p>No matching tools.</p>}</>}
    </details>}
    {!shown.length && <p>No matching blocks.</p>}
  </section>
}
