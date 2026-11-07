import { useEffect, useRef, useState } from 'react'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { CONTAINER_DESIGN, NAV_DESIGN, PAGE_APPEARANCE, PORTFOLIO_APPEARANCE, PHOTO_DESIGN, TEXT_DESIGN } from './pageDesign.js'
import { findNode, resetDesign, selectionIdentity, setOverride } from './portfolioEdits.js'

const names = { font: 'Font', size: 'Text size', weight: 'Weight', color: 'Text color', align: 'Alignment', lineHeight: 'Line height',
  letterSpacing: 'Letter spacing', italic: 'Italic', underline: 'Underline', hidden: 'Hidden', columns: 'Columns', mobileColumns: 'Mobile columns',
  gap: 'Gap', padding: 'Padding', maxWidth: 'Maximum width', background: 'Background', radius: 'Rounding', borderWidth: 'Border width',
  borderColor: 'Border color', shadow: 'Shadow', fit: 'Image fit', imageFit: 'Image fit', aspect: 'Aspect ratio', focal: 'Focal point', span: 'Column span',
  scale: 'Text scale', headingWeight: 'Heading weight', spacing: 'Spacing', width: 'Content width', layout: 'Navigation layout',
  hideHome: 'Hide home', hideAbout: 'Hide About', hideResume: 'Hide resume' }
const options = {
  font: ['classic', 'serif', 'sans', 'mono'], weight: ['regular', 'medium', 'bold'], headingWeight: ['regular', 'medium', 'bold'],
  align: ['left', 'center', 'right'], shadow: ['none', 'soft', 'strong'], fit: ['contain', 'cover'], imageFit: ['contain', 'cover'],
  aspect: ['natural', 'square', 'landscape', 'portrait'], focal: ['center', 'top', 'bottom', 'left', 'right', 'top-left', 'top-right', 'bottom-left', 'bottom-right'],
  scale: ['compact', 'regular', 'large'], spacing: ['tight', 'regular', 'airy'], width: ['narrow', 'regular', 'wide'], layout: ['row', 'column'],
}
const booleans = ['italic', 'underline', 'hidden', 'hideHome', 'hideAbout', 'hideResume']
const colors = ['color', 'background', 'borderColor', 'text', 'muted', 'accent']
const limits = { size: [12, 96], lineHeight: [1, 2.5], letterSpacing: [-1, 8], columns: [1, 4], mobileColumns: [1, 2],
  gap: [0, 96], padding: [0, 96], maxWidth: [240, 1800], radius: [0, 64], borderWidth: [0, 8], span: [1, 2] }
const human = key => names[key] || key.replace(/([A-Z])/g, ' $1').replace(/^./, value => value.toUpperCase())
const at = (node, path) => path.reduce((value, key) => value?.[key], node)

export function designElements(document) {
  const rows = [], portfolio = Boolean(document.categories)
  const add = (node, label, path, rules, content, limit) => rows.push({ id: `${selectionIdentity(node)}:${path.join('.')}`, node: selectionIdentity(node), label, path, rules, content, limit })
  add(document, 'Page appearance', ['appearance'], portfolio ? PORTFOLIO_APPEARANCE : PAGE_APPEARANCE)
  const rootText = portfolio ? ['name', 'headline', 'introduction', 'about', 'aboutHeading', 'contactHeading', 'location', 'email', 'resumeHeading'] : ['label', 'headline', 'description', 'status']
  const textLimits = { name: 120, headline: 240, introduction: 2400, about: 8000, location: 160, email: 254, label: 120, description: 1200, status: 80 }
  rootText.forEach(key => add(document, human(key), ['textStyles', key], TEXT_DESIGN,
    ['location', 'email'].includes(key) ? ['contact', key] : key.endsWith('Heading') ? ['labels', key] : [key], textLimits[key] || 80))
  if (!portfolio) return rows
  add(document, 'Navigation', ['nav'], NAV_DESIGN)
  add(document, 'Close image label', ['labels'], null, ['labels', 'viewerClose'], 80)
  for (const category of document.categories) {
    add(category, `Gallery: ${category.title}`, ['layout'], CONTAINER_DESIGN)
    add(category, `Gallery address: ${category.title}`, [], null, ['slug'], 48)
    for (const key of ['title', 'description', 'navigation']) add(category, `${human(key)}: ${category.title}`, ['textStyles', key], TEXT_DESIGN, [key === 'navigation' ? 'navLabel' : key], key === 'description' ? 1200 : key === 'title' ? 120 : 40)
    for (const image of category.images) {
      add(image, `Photo: ${image.alt}`, ['display'], PHOTO_DESIGN)
      add(image, `Caption: ${image.alt}`, ['captionStyle'], TEXT_DESIGN, ['title'], 200)
      add(image, `Alt text: ${image.alt}`, [], null, ['alt'], 400)
    }
  }
  for (const section of document.resume) {
    add(section, `Resume section: ${section.heading}`, ['layout'], CONTAINER_DESIGN)
    add(section, `Section heading: ${section.heading}`, ['headingStyle'], TEXT_DESIGN, ['heading'], 120)
    for (const entry of section.entries) {
      add(entry, `Resume entry: ${entry.title || 'Untitled'}`, ['layout'], CONTAINER_DESIGN)
      for (const key of ['title', 'subtitle', 'dates', 'description']) add(entry, `${human(key)}: ${entry.title || 'Untitled entry'}`, ['textStyles', key], TEXT_DESIGN, [key], { title: 200, subtitle: 240, dates: 120, description: 2400 }[key])
    }
  }
  return rows
}

export function DesignControls({ document, row, onChange }) {
  const node = findNode(document, row.node)
  if (!node) return null
  const value = at(node, row.path) || {}
  const change = (path, item) => onChange(setOverride(document, row.node, path, item), { coalesceKey: `${row.node}:${path.join('.')}` })
  const controls = (rules, current, path) => Object.keys(rules).map(key => {
    if (key === 'palette') return <fieldset key={key}><legend>Palette</legend>{controls({ background: true, text: true, muted: true, accent: true }, current.palette || {}, [...path, key])}</fieldset>
    if (key === 'appearance') return <fieldset key={key}><legend>Navigation text</legend>{controls(Object.fromEntries(Object.entries(TEXT_DESIGN).filter(([name]) => name !== 'hidden')), current.appearance || {}, [...path, key])}</fieldset>
    if (key === 'order') return <fieldset key={key}><legend>Navigation order</legend>{[...(current.order || []), ...['home', 'galleries', 'about', 'resume'].filter(group => !current.order?.includes(group))].map((group, index, order) => <div key={group}>{human(group)} <button type="button" disabled={!index} onClick={() => {
      const next = [...order]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; change([...path, key], next)
    }}>Move {human(group)} up</button></div>)}<button type="button" onClick={() => change([...path, key], undefined)}>Reset navigation order</button></fieldset>
    const field = [...path, key], label = human(key)
    return <div key={key}><label><span>{label}</span>{options[key] ? <select value={current[key] ?? ''} onChange={event => change(field, event.target.value || undefined)}>
      <option value="">Inherit</option>{options[key].map(option => <option key={option} value={option}>{human(option)}</option>)}
    </select> : booleans.includes(key) ? <input type="checkbox" checked={current[key] === true} onChange={event => change(field, event.target.checked || undefined)} />
      : <input type={limits[key] ? 'number' : 'text'} value={current[key] ?? ''} min={limits[key]?.[0]} max={limits[key]?.[1]} step={key === 'lineHeight' ? .1 : 1}
        maxLength={colors.includes(key) ? 7 : 40} placeholder={colors.includes(key) ? '#RRGGBB' : 'Inherit'} onChange={event => change(field, event.target.value === '' ? undefined : limits[key] ? Number(event.target.value) : event.target.value)} />}</label>
      {current[key] !== undefined && <button type="button" onClick={() => change(field, undefined)}>Reset {label.toLowerCase()}</button>}
    </div>
  })
  return <div className="element-properties">
    {!document.categories && row.path[0] === 'appearance' && <>
      <label><span>Page theme</span><select value={document.theme} onChange={event => change(['theme'], event.target.value)}>{['cream', 'light', 'dark'].map(value => <option key={value} value={value}>{human(value)}</option>)}</select></label>
      <label><span>Page alignment</span><select value={document.alignment} onChange={event => change(['alignment'], event.target.value)}>{['left', 'center'].map(value => <option key={value} value={value}>{human(value)}</option>)}</select></label>
    </>}
    {node.images && <label><span>Hide gallery</span><input type="checkbox" checked={node.hidden === true} onChange={event => change(['hidden'], event.target.checked || undefined)} /></label>}
    {row.content && <label><span>Text: {row.label}</span><textarea value={at(node, row.content) || ''} maxLength={row.limit} onChange={event => change(row.content, event.target.value || (row.content[0] === 'labels' || row.content[0] === 'navLabel' ? undefined : ''))} /></label>}
    {row.rules && controls(row.rules, value, row.path)}
    {row.rules && <button type="button" onClick={() => change(row.path, undefined)}>Reset these properties</button>}
    <p>Hidden content remains in your project. Hiding does not change access.</p>
  </div>
}

export default function ElementInspector({ document, selected, onSelect, onChange, onClose, children }) {
  const holder = useRef(null)
  const [narrow, setNarrow] = useState(() => globalThis.matchMedia?.('(max-width: 899px)').matches ?? false)
  useEffect(() => {
    const media = globalThis.matchMedia?.('(max-width: 899px)')
    const update = () => setNarrow(Boolean(media?.matches))
    media?.addEventListener?.('change', update)
    return () => media?.removeEventListener?.('change', update)
  }, [])
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useEffect(() => {
    if (!closeRef.current) return
    const previous = globalThis.document.activeElement
    const sheet = () => globalThis.matchMedia?.('(max-width: 899px)').matches
    if (sheet() && !previous?.classList.contains('canvas-inline-text')) holder.current?.querySelector('button')?.focus({ preventScroll: true })
    const trap = event => {
      if (!sheet() || event.key !== 'Tab' || holder.current?.closest('[hidden], [inert]')) return
      const controls = [...holder.current.querySelectorAll('button, input, select, textarea, summary, [tabindex="0"]')].filter(node => !node.disabled && !node.closest('fieldset[disabled]') && node.getClientRects().length)
      if (!controls.length) return
      const index = controls.indexOf(globalThis.document.activeElement)
      if (index < 0 || event.shiftKey && index === 0 || !event.shiftKey && index === controls.length - 1) {
        event.preventDefault(); controls[event.shiftKey ? controls.length - 1 : 0].focus()
      }
    }
    globalThis.document.addEventListener('keydown', trap)
    return () => { globalThis.document.removeEventListener('keydown', trap); if (previous?.isConnected) previous.focus({ preventScroll: true }) }
  }, [])
  useEscapeOwner('page-inspector', Boolean(onClose), () => onClose?.(), { layer: 'edit', scope: holder,
    when: () => Boolean(holder.current && !holder.current.closest('[hidden], [inert]')) })
  const rows = designElements(document), row = selected ? rows.find(item => item.id === selected) : rows[0]
  return <section ref={holder} className="element-inspector" aria-label="Element properties" role={onClose && narrow ? 'dialog' : undefined} aria-modal={onClose && narrow ? true : undefined}>
    {onClose && <button type="button" onClick={onClose}>Close properties</button>}
    <label><span>Elements / Outline</span><select value={row?.id || ''} onChange={event => onSelect(event.target.value)}>{!row && <option value="">Selected element was removed. Choose an element.</option>}{rows.map(item => {
      const node = findNode(document, item.node), config = at(node, item.path)
      return <option key={item.id} value={item.id}>{item.label}{config?.hidden || node.hidden ? ' (hidden)' : item.content && !at(node, item.content) ? ' (empty)' : ''}</option>
    })}</select></label>
    {row && <DesignControls document={document} row={row} onChange={onChange} />}
    <button type="button" onClick={() => onChange(resetDesign(document))}>Reset all design</button>
    {children}
  </section>
}

export function InlineText({ document, node = document, path, label, limit, onChange, onSelect, style }) {
  const snapshot = useRef(null), identity = selectionIdentity(node)
  const holder = useRef(null), [focused, setFocused] = useState(false)
  const cancel = () => {
    if (snapshot.current !== null) onChange(setOverride(document, identity, path, snapshot.current.value))
    holder.current?.blur()
  }
  useEscapeOwner('page-inline-text', focused, cancel, { layer: 'edit', scope: holder })
  const value = at(node, path) || ''
  return <textarea ref={holder} data-element={`${identity}:${node.material_id ? 'captionStyle' : path[0] === 'heading' ? 'headingStyle' : `textStyles.${path.at(-1)}`}`} className="canvas-inline-text" aria-label={label} value={value} maxLength={limit} style={style} rows={1}
    onFocus={() => { snapshot.current = { value: at(node, path) }; setFocused(true); onSelect?.() }} onBlur={() => { snapshot.current = null; setFocused(false) }}
    onChange={event => onChange(setOverride(document, identity, path, event.target.value || (path[0] === 'labels' || path[0] === 'navLabel' ? undefined : '')), { coalesceKey: `${identity}:${path.join('.')}` })}
    onKeyDown={event => { if (event.key === 'Escape' && snapshot.current !== null) {
      event.preventDefault(); event.stopPropagation()
      cancel()
    } }} />
}
