import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { blockLabel, ELEMENT_FAMILIES, LANES, laneLabel, strategySection } from './elementCatalog.js'
import { CATEGORIES, categoryOf, entryMeta, readRecent } from './libraryCategories.js'

const LibraryDetails = lazy(() => import('./LibraryDetails.jsx'))

export const BLOCK_MIME = 'application/leaf-lab-block'
export default function Palette({ entries, onAdd, onArm, armed: canvasArmed, searchRef, tools = [], onLoadTools }) {
  const [query, setQuery] = useState('')
  const [groupBy, setGroupBy] = useState('category')
  const [details, setDetails] = useState(null)
  const [localArmed, setLocalArmed] = useState(null)
  const armed = canvasArmed === undefined ? localArmed : canvasArmed
  const arm = manifest => { setLocalArmed(manifest); onArm?.(manifest) }
  const pointer = useRef(null)
  const [toolsState, setToolsState] = useState('idle')
  const [toolsOpen, setToolsOpen] = useState(false)
  const toolsRequested = useRef(false)
  const active = useRef(true)
  useEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])
  const loadTools = event => {
    setToolsOpen(event.currentTarget.open)
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
  const matches = (manifest, isTool = false, title = manifest.title) => `${title} ${manifest.id} ${manifest.description || ''} ${CATEGORIES.find(category => category.id === categoryOf(manifest, { isTool })).title} ${laneLabel(manifest.lane) || ''} ${strategySection(manifest) || ''}`.toLowerCase().includes(query.toLowerCase())
  const shown = Object.values(entries).filter(manifest => !toolManifests.has(manifest) && matches(manifest))
  const shownTools = usableTools.filter(tool => matches(tool.manifest || tool, true, tool.title))
  const titles = [...shown.map(manifest => manifest.title), ...(toolsState === 'on' ? shownTools.map(tool => tool.title) : [])]
  let recent = []
  try { recent = readRecent(globalThis.localStorage) } catch { /* Storage may be unavailable. */ }
  const entry = (manifest, title = blockLabel(manifest), isRecent = false) => {
    const label = (isRecent || titles.filter(value => value === title).length > 1 ? `${title} (${manifest.id})` : title) + (isRecent ? ' · Recent' : '')
    const meta = entryMeta(manifest)
    return <div key={manifest.id} data-library-entry={manifest.id}>
      <button type="button" draggable data-testid={`library-${isRecent ? 'recent-' : ''}add-${manifest.id}`} aria-label={isRecent ? label : title}
        aria-describedby={`library-${isRecent ? 'recent-' : ''}meta-${manifest.id}`} aria-pressed={armed === manifest}
        onPointerDown={event => { pointer.current = event.pointerType }}
        onPointerCancel={() => { pointer.current = null }}
        onKeyDown={() => { pointer.current = null }}
        onClick={() => {
          const coarse = ['touch', 'pen'].includes(pointer.current)
          pointer.current = null
          if (coarse) arm(armed === manifest ? null : manifest)
          else { arm(null); onAdd(manifest) }
        }}
        onDragStart={event => { pointer.current = null; event.dataTransfer.setData(BLOCK_MIME, manifest.id); event.dataTransfer.effectAllowed = 'copy' }}>
        {label}
      </button>
      <small id={`library-${isRecent ? 'recent-' : ''}meta-${manifest.id}`} style={{ display: 'block', opacity: 0.7 }}>{meta.idVersion}</small>
      {[meta.effect, meta.where, ...(meta.placeholder ? ['Placeholder'] : [])].map(chip => <span className="lab-effect-chip" key={chip}>{chip}</span>)}
      <button type="button" aria-label={`About ${title}`} onClick={() => setDetails(manifest)}>Info</button>
    </div>
  }
  const families = items => {
    const known = new Set(ELEMENT_FAMILIES.flatMap(family => family.variants))
    return [...ELEMENT_FAMILIES, ...items.filter(item => !known.has(item.id)).map(item => ({ id: item.id, variants: [item.id] }))].flatMap(family => {
      const variants = items.filter(manifest => family.variants.includes(manifest.id))
      return variants.length ? [<div key={family.id} data-element-family={family.id}>
        <h4>{variants[0].title}</h4>{variants.map(manifest => entry(manifest))}
      </div>] : []
    })
  }
  const toolGroup = onLoadTools && <details key="tools" aria-label="Tools" data-palette-tools open={toolsOpen} onToggle={loadTools}>
    <summary>Tools</summary>
    {toolsState === 'loading' && <p>Loading tools.</p>}
    {toolsState === 'unavailable' && <p>Tools are not available here.</p>}
    {toolsState === 'on' && <><p>Tools run on the server: save the graph and use Run.</p>
      {shownTools.map(tool => tool.available && tool.manifest ? entry(tool.manifest, tool.title)
        : <button type="button" key={`${tool.id}@${tool.version}`} disabled draggable={false}>
          {tool.title}{' '}<span>Unavailable: {tool.reason ?? (tool.manifest ? 'unavailable' : 'no manifest')}</span>
        </button>)}
      {!shownTools.length && <p>No matching tools.</p>}</>}
  </details>
  return <section className="lab-palette" aria-label="Strategy palette">
    <h2>Strategy palette</h2><p>Strategy decides when to buy. Sizing and Cash out stay in their own Lanes.</p>
    <label>Search blocks (/)<input ref={searchRef} type="search" value={query} onChange={event => setQuery(event.target.value)} /></label>
    <label>Group blocks<select value={groupBy} onChange={event => setGroupBy(event.target.value)}>
      <option value="category">Categories</option><option value="family">Blocks</option><option value="lane">Nine Lanes</option>
    </select></label>
    <p>Drag a block, or click or press Return on its button to add at the canvas centre. On touch screens, tap a block then tap the canvas.</p>
    {armed && <p role="status">Tap the canvas to place {armed.title}. Tap the entry again to cancel.</p>}
    {groupBy === 'category' ? CATEGORIES.map(category => {
      if (category.id === 'tools') return toolGroup
      const items = category.id === 'recent' ? recent.map(id => shown.find(manifest => manifest.id === id)
        || (toolsState === 'on' && shownTools.find(tool => tool.id === id && tool.available)?.manifest)).filter(Boolean)
        : shown.filter(manifest => categoryOf(manifest) === category.id)
      return items.length || ['recent', 'kashbot-v2'].includes(category.id) ? <section key={category.id} aria-label={category.title}>
        <h3>{category.title}</h3>{items.map(manifest => entry(manifest, usableTools.find(tool => tool.manifest === manifest)?.title || manifest.title, category.id === 'recent'))}
        {!items.length && <p>{category.id === 'recent' ? 'No recent blocks.' : 'No Kashbot v2 blocks yet.'}</p>}
      </section> : null
    }) : groupBy === 'lane' ? LANES.map(lane => <section key={lane.id} aria-label={lane.title}><h3>{lane.title}</h3>
      {shown.filter(manifest => manifest.lane === lane.id).map(manifest => entry(manifest))}
      {!shown.some(manifest => manifest.lane === lane.id) && <p>No blocks in this Lane.</p>}
    </section>) : <>
      <details open><summary>Strategy</summary>{['Signals', 'Rules'].map(section => <section key={section} aria-label={section}>
        <h3>{section}</h3>{families(shown.filter(manifest => strategySection(manifest) === section))}
      </section>)}</details>
      {families(shown.filter(manifest => manifest.lane !== 'engines'))}
    </>}
    {groupBy !== 'category' && toolGroup}
    {!shown.length && <p>No matching blocks.</p>}
    {details && <Suspense fallback={<p role="status">Loading details.</p>}>
      <LibraryDetails key={details.id} manifest={details} onClose={() => setDetails(null)} />
    </Suspense>}
  </section>
}
