import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { readActiveScope, subscribeActiveScope } from '../auth/activeScope.js'
import { subscribeBeforeLogout, subscribeTokenStored } from '../auth.js'
import { readProjectPhotoPreview } from '../projects/api.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { photoReference } from './portfolioDocument.js'
import { cloneDocument, selectionIdentity } from './portfolioEdits.js'
import useProjectPhotos from './useProjectPhotos.js'
import PhotoMaterialIntake from '../workspace/PhotoMaterialIntake.jsx'
import { appearanceStyle, containerStyle, navigationOrder, photoStyle, photoSpanStyle, textStyle } from './pageDesign.js'
import { InlineText } from './ElementInspector.jsx'
import './portfolioEditor.css'

export const portfolioField = (document, descriptor) => ({ ...descriptor, document: selectionIdentity(document) })
const selectionKey = descriptor => {
  if (!descriptor || typeof descriptor !== 'object' || Array.isArray(descriptor)
    || Object.values(descriptor).some(value => typeof value !== 'string')) return null
  return JSON.stringify(Object.entries(descriptor).sort(([a], [b]) => a.localeCompare(b)))
}

function Photo({ image, projectId, scope, appearance, reveal }) {
  const [url, setUrl] = useState(null)
  const [error, setError] = useState('')
  const [visible, setVisible] = useState(false)
  const holder = useRef(null)
  useEffect(() => {
    if (!globalThis.IntersectionObserver) { setVisible(true); return }
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { setVisible(true); observer.disconnect() }
    }, { rootMargin: '200px' })
    observer.observe(holder.current)
    return () => observer.disconnect()
  }, [])
  useEffect(() => {
    if (!visible || !scope || !projectId) return
    let active = true, objectUrl
    const controller = new AbortController()
    const clear = () => {
      active = false; controller.abort()
      if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null }
      setUrl(null)
    }
    const logout = subscribeBeforeLogout(clear)
    const token = subscribeTokenStored(() => { if (readActiveScope() !== scope) clear() })
    setError(''); setUrl(null)
    readProjectPhotoPreview(projectId, image.material_id, { signal: controller.signal, expectedScope: scope }).then(blob => {
      if (!active || readActiveScope() !== scope) return
      objectUrl = URL.createObjectURL(blob); setUrl(objectUrl)
    }).catch(failure => { if (active && failure.name !== 'AbortError') setError('Photo could not load. Reopen this view to retry.') })
    return () => { clear(); logout(); token() }
  }, [visible, scope, projectId, image.material_id, image.preview_sha256])
  return <span className="portfolio-photo" ref={holder}>
    {url ? <img src={url} alt={image.alt} style={photoStyle(reveal ? { ...image.display, hidden: undefined } : image.display, appearance)} onError={() => setError('Photo could not be displayed. Reopen this view to retry.')} /> : !error && <span role="status">{scope ? 'Loading photo…' : 'Sign in to view this photo.'}</span>}
    {error && <span role="alert">{error}</span>}
  </span>
}

export function PortfolioPreview({ document, projectId, onSelectField, onInlineChange, onSelectElement, selectedElement }) {
  const scope = useSyncExternalStore(subscribeActiveScope, readActiveScope, () => null)
  return <PortfolioView key={`${scope}:${projectId}`} document={document} projectId={projectId} scope={scope} onSelectField={onSelectField} onInlineChange={onInlineChange} onSelectElement={onSelectElement} selectedElement={selectedElement} />
}

function PortfolioView({ document, projectId, scope, onSelectField, onInlineChange, onSelectElement, selectedElement }) {
  const [view, setView] = useState('home')
  const [selected, setSelected] = useState(null)
  const trigger = useRef(null), closeButton = useRef(null), surface = useRef(null)
  const navigationSelection = useRef(null)
  useEffect(() => {
    const selection = (onInlineChange || onSelectField) ? selectedElement : null
    if (navigationSelection.current === selection) return
    navigationSelection.current = selection
    // Ordinary editor renders must not undo the user's gallery navigation.
    if (!selection) return
    const [identity, path] = selectedElement.split(':')
    const gallery = document.categories.find(item => selectionIdentity(item) === identity || item.images.some(image => selectionIdentity(image) === identity))
    const resume = document.resume.some(section => selectionIdentity(section) === identity || section.entries.some(entry => selectionIdentity(entry) === identity))
    setView(gallery ? gallery.slug : resume || path === 'textStyles.resumeHeading' ? 'resume' : ['about', 'aboutHeading', 'contactHeading', 'location', 'email'].some(key => path === `textStyles.${key}`) ? 'about' : 'home')
    setSelected(null)
  }, [selectedElement, document, onInlineChange, onSelectField])
  useEffect(() => {
    if (!selectedElement) return
    const nodes = [...(surface.current?.querySelectorAll('[data-element]') || [])]
    const target = nodes.find(node => node.dataset.element === selectedElement) || nodes.find(node => node.dataset.element.startsWith(`${selectedElement.split(':')[0]}:`))
    target?.scrollIntoView?.({ block: 'start', behavior: 'smooth' })
  }, [selectedElement, view, document])
  const close = () => { setSelected(null); trigger.current?.focus() }
  const visible = () => Boolean(surface.current && !surface.current.closest('[hidden], [inert], [aria-hidden="true"]'))
  useEscapeOwner('portfolio-image', Boolean(selected), close, { layer: 'sheet', scope: surface, when: visible })
  useEffect(() => {
    if (!selected) return
    closeButton.current?.focus()
    const key = event => {
      if (!visible()) return
      if (event.key === 'Tab') { event.preventDefault(); closeButton.current?.focus() }
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [selected])
  const category = document.categories.find(item => item.slug === view)
  const navigate = next => { setSelected(null); setView(next) }
  const selectedIdentity = (onInlineChange || onSelectField) ? selectedElement?.split(':')[0] : null
  const reveals = node => selectionIdentity(node) === selectedIdentity || node.images?.some(reveals) || node.entries?.some(reveals)
  const layout = node => containerStyle(reveals(node) ? { ...node.layout, hidden: undefined } : node.layout)
  const photo = image => <Photo key={`${image.material_id}:${image.preview_sha256}`} image={image} projectId={projectId} scope={scope} appearance={document.appearance} reveal={reveals(image)} />
  const edit = (descriptor, label) => onSelectField && <button type="button" className="portfolio__edit" aria-label={`Edit ${label}`} onClick={() => onSelectField(portfolioField(document, descriptor))}>Edit {label}</button>
  const galleryEdits = item => <>{edit({ field: 'gallery.title', gallery: item.slug }, `${item.title} gallery title`)}{edit({ field: 'gallery.description', gallery: item.slug }, `${item.title} gallery description`)}</>
  const nav = document.nav || {}, labels = document.labels || {}
  const visibleCategories = document.categories.filter(item => reveals(item) || !item.hidden && !item.layout?.hidden)
  const text = (node, path, label, limit, style = {}, fallback = '') => onInlineChange
    ? <InlineText document={document} node={node} path={path} label={label} limit={limit} style={textStyle(reveals(node) ? { ...style, hidden: undefined } : style)} onChange={onInlineChange} onSelect={() => onSelectElement?.(`${selectionIdentity(node)}:${node.material_id ? 'captionStyle' : path[0] === 'heading' ? 'headingStyle' : `textStyles.${path.at(-1)}`}`)} />
    : <span data-element={`${selectionIdentity(node)}:${node.material_id ? 'captionStyle' : path[0] === 'heading' ? 'headingStyle' : `textStyles.${path.at(-1)}`}`} style={textStyle(style)}>{path.reduce((value, key) => value?.[key], node) ?? fallback}</span>
  const rootText = (key, limit, fallback) => text(document, key.endsWith('Heading') ? ['labels', key] : [key], key, limit, document.textStyles?.[key], fallback)
  const galleryText = item => <><h2>{text(item, ['title'], 'Gallery title', 120, item.textStyles?.title)}</h2><p>{text(item, ['description'], 'Gallery description', 1200, item.textStyles?.description)}</p></>
  const grid = item => ({ '--design-mobile-columns': item?.layout?.mobileColumns ?? document.appearance?.mobileColumns, gap: item?.layout?.gap ?? document.appearance?.gap, '--design-columns': item?.layout?.columns ?? document.appearance?.columns ?? (item && document.galleryLayout === 'single' ? 1 : 2) })
  return <main className="portfolio" data-element={`${selectionIdentity(document)}:appearance`} ref={surface} style={appearanceStyle(document.appearance)}>
    {(!nav.hidden || selectedElement === `${selectionIdentity(document)}:nav` && onInlineChange) && <nav data-element={`${selectionIdentity(document)}:nav`} aria-label="Portfolio" style={{ ...containerStyle(nav), ...textStyle(nav.appearance), display: 'flex', flexDirection: nav.layout }}>
      {navigationOrder(nav).map(group => group === 'home' ? !nav.hideHome && <button key={group} onClick={() => navigate('home')} aria-current={view === 'home' ? 'page' : undefined} style={textStyle(document.textStyles?.name)}>{nav.homeLabel || document.name}</button>
        : group === 'galleries' ? visibleCategories.map(item => <button key={selectionIdentity(item)} style={textStyle(item.textStyles?.navigation)} onClick={() => navigate(item.slug)} aria-current={view === item.slug ? 'page' : undefined}>{item.navLabel || item.title}</button>)
          : group === 'about' ? !nav.hideAbout && <button key={group} onClick={() => navigate('about')}>{nav.aboutLabel || 'About / Contact'}</button>
            : !nav.hideResume && <button key={group} onClick={() => navigate('resume')}>{nav.resumeLabel || 'Resume'}</button>)}
    </nav>}
    {onInlineChange ? rootText('name', 120) : edit({ field: 'name' }, 'name')}
    {view === 'about' ? <><h1>{rootText('aboutHeading', 80, `About ${document.name}`)}</h1><p>{rootText('about', 8000)}</p>{edit({ field: 'about' }, 'About')}<h2>{rootText('contactHeading', 80, 'Contact')}</h2>
      {['location', 'email'].map(key => <p key={key}>{text(document, ['contact', key], key, key === 'email' ? 254 : 160, document.textStyles?.[key])}{edit({ field: `contact.${key}` }, key === 'email' ? 'contact email' : key)}</p>)}</>
      : view === 'resume' ? <><h1>{rootText('resumeHeading', 80, 'Resume')}</h1>{document.resume.map((section, index) => <section key={selectionIdentity(section)} className={section.layout && (section.layout.columns || section.layout.mobileColumns || section.layout.gap !== undefined) ? 'design-container' : undefined} data-element={`${selectionIdentity(section)}:layout`} style={layout(section)}><h2>{text(section, ['heading'], 'Section heading', 120, section.headingStyle)}</h2>{edit({ field: 'resume.heading', section: selectionIdentity(section) }, `resume section ${index + 1} heading`)}{section.entries.map((entry, i) => <article className={entry.layout && (entry.layout.columns || entry.layout.mobileColumns || entry.layout.gap !== undefined) ? 'design-container' : undefined} data-element={`${selectionIdentity(entry)}:layout`} style={layout(entry)} key={selectionIdentity(entry)}>
        {['title', 'subtitle', 'dates', 'description'].map(field => {
          const Tag = field === 'title' ? 'h3' : 'p'
          return <Tag key={field}>{text(entry, [field], `Resume ${field}`, { title: 200, subtitle: 240, dates: 120, description: 2400 }[field], entry.textStyles?.[field])}{edit({ field: `resume.${field}`, section: selectionIdentity(section), entry: selectionIdentity(entry) }, `resume section ${index + 1} entry ${i + 1} ${field}`)}</Tag>
        })}
      </article>)}</section>)}</>
        : category ? <section data-element={`${selectionIdentity(category)}:layout`} style={layout(category)}><h1>{text(category, ['title'], 'Gallery title', 120, category.textStyles?.title)}</h1><p>{text(category, ['description'], 'Gallery description', 1200, category.textStyles?.description)}</p>{galleryEdits(category)}<div className="portfolio-grid" style={grid(category)} data-gallery-layout={document.galleryLayout || 'grid'}>{category.images.filter(image => reveals(image) || !image.display?.hidden).map(image => <figure key={image.material_id} data-element={`${selectionIdentity(image)}:display`} style={photoSpanStyle(image.display, document, category)}><button onClick={event => { if (onInlineChange) { onSelectElement?.(`${selectionIdentity(image)}:display`, true); return } trigger.current = event.currentTarget; setSelected(image) }} aria-label={`View ${image.alt}`}>
          {photo(image)}{!onInlineChange && <span style={textStyle(image.captionStyle)}>{image.title}</span>}
        </button>{onInlineChange && text(image, ['title'], 'Photo caption', 200, image.captionStyle)}{edit({ field: 'caption', gallery: category.slug, material: image.material_id }, `${category.title} caption for ${image.alt}`)}</figure>)}</div></section>
          : <><h1>{rootText('headline', 240)}</h1>{edit({ field: 'headline' }, 'headline')}<p>{rootText('introduction', 2400)}</p>{edit({ field: 'introduction' }, 'introduction')}<div className="portfolio-grid" style={grid()}>{visibleCategories.map(item => <div key={selectionIdentity(item)} data-element={`${selectionIdentity(item)}:layout`} style={layout(item)}><button onClick={() => navigate(item.slug)}>
            {item.images[item.hero] && !item.images[item.hero].display?.hidden ? photo(item.images[item.hero]) : <span>Add photos in the Project panel.</span>}{!onInlineChange && galleryText(item)}
          </button>{onInlineChange && galleryText(item)}{galleryEdits(item)}</div>)}</div></>}
    {selected && <div className="portfolio-viewer" role="dialog" aria-modal="true" aria-label={selected.alt}>
      <button ref={closeButton} onClick={close}>{labels.viewerClose || 'Close image'}</button>{photo(selected)}<p style={textStyle(selected.captionStyle)}>{selected.title}</p>
    </div>}
  </main>
}

function Field({ label, value, limit, onChange, multiline = false, inputRef }) {
  return <label><span>{label}</span>{multiline ? <textarea ref={inputRef} value={value} maxLength={limit} rows={4} onChange={event => onChange(event.target.value)} /> : <input ref={inputRef} value={value} maxLength={limit} onChange={event => onChange(event.target.value)} />}</label>
}

function Disclosure({ title, initiallyOpen = false, reviewRequest, children }) {
  const holder = useRef(null)
  useEffect(() => {
    if (reviewRequest) holder.current.open = true
  }, [reviewRequest])
  return <details className="portfolio-editor__disclosure" ref={holder} open={initiallyOpen || undefined}>
    <summary>{title}</summary>
    <div className="portfolio-editor__fields">{children}</div>
  </details>
}

export default function PortfolioEditor(props) {
  const scope = useSyncExternalStore(subscribeActiveScope, readActiveScope, () => null)
  return <ScopedPortfolioEditor key={`${scope}:${props.projectId}`} {...props} scope={scope} />
}

function ScopedPortfolioEditor({ document, onChange, projectId, reviewRequest, selectedField, onSelectElement, scope }) {
  const [showIntake, setShowIntake] = useState(false)
  const { photos, error, loading, loadPhotos } = useProjectPhotos(projectId, scope)
  const latest = useRef(null), alive = useRef(false)
  latest.current = { document, photos }
  useEffect(() => {
    alive.current = true
    const logout = subscribeBeforeLogout(() => { alive.current = false })
    return () => { alive.current = false; logout() }
  }, [])
  const fields = useRef(new Map())
  const target = descriptor => node => {
    const key = selectionKey(portfolioField(document, descriptor))
    if (node) {
      fields.current.set(key, node)
      const gallery = document.categories.find(item => item.slug === descriptor.gallery)
      const owner = descriptor.material ? gallery?.images.find(item => item.material_id === descriptor.material) : descriptor.entry ? document.resume.flatMap(section => section.entries).find(item => selectionIdentity(item) === descriptor.entry) : descriptor.section ? document.resume.find(item => selectionIdentity(item) === descriptor.section) : gallery || document
      if (owner) node.dataset.element = `${selectionIdentity(owner)}:${['alt', 'gallery.slug'].includes(descriptor.field) ? '' : descriptor.field === 'caption' ? 'captionStyle' : descriptor.field === 'resume.heading' ? 'headingStyle' : `textStyles.${descriptor.field.split('.').at(-1)}`}`
    }
    else fields.current.delete(key)
  }
  useEffect(() => {
    const node = fields.current.get(selectionKey(selectedField?.field))
    if (!node || node.disabled || node.closest('fieldset[disabled]')) return
    for (let parent = node.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === 'DETAILS') parent.open = true
    }
    // The inspector's sticky Save row uses scroll margins on these existing inputs.
    node.scrollIntoView?.({ block: 'center' })
    node.focus({ preventScroll: true })
  }, [selectedField])
  // Editor-only identities survive the document clone, without entering the saved schema.
  const identities = useRef(new WeakMap()), serial = useRef(0), added = useRef(new Set())
  const identity = item => {
    if (!identities.current.has(item)) identities.current.set(item, ++serial.current)
    return identities.current.get(item)
  }
  const newItem = item => { added.current.add(identity(item)); return item }
  const update = (action, source = document) => {
    const next = cloneDocument(source)
    for (const field of ['categories', 'resume']) source[field].forEach((item, index) => {
      identities.current.set(next[field][index], identity(item))
    })
    action(next); latest.current.document = next; onChange(next)
  }
  const category = (index, action) => update(next => action(next.categories[index]))
  const image = (ci, ii, action) => category(ci, next => action(next.images[ii]))
  const count = document.categories.reduce((sum, item) => sum + item.images.length, 0)
  const addPhoto = (item, materialId, control) => {
    if (!alive.current || !scope || !projectId || readActiveScope() !== scope || control.closest('fieldset[disabled]')) return
    const source = latest.current.document
    const ci = source.categories.findIndex(entry => identity(entry) === identity(item))
    const row = latest.current.photos.find(photo => photo.material_id === materialId)
    if (ci < 0 || !row || source.categories[ci].images.some(entry => entry.material_id === materialId)
      || source.categories.reduce((sum, entry) => sum + entry.images.length, 0) >= 200) return
    added.current.add(identity(item))
    added.current.add(`${identity(item)}:${materialId}`)
    update(next => { next.categories[ci].images.push(photoReference(row)) }, source)
  }
  const move = (ci, ii, delta) => category(ci, next => {
    const hero = next.images[next.hero], target = ii + delta
    ;[next.images[ii], next.images[target]] = [next.images[target], next.images[ii]]
    next.hero = next.images.indexOf(hero)
  })
  const removeImage = (ci, ii) => category(ci, next => {
    const hero = next.images[next.hero]
    next.images.splice(ii, 1); next.hero = Math.max(0, next.images.indexOf(hero))
  })
  return <div className="portfolio-editor" onFocus={event => { if (event.target.dataset.element) onSelectElement?.(event.target.dataset.element) }}>
    { [['name', 'Name', 120], ['headline', 'Headline', 240], ['introduction', 'Introduction', 2400]].map(([key, label, limit]) => <Field key={key} inputRef={target({ field: key })} label={label} value={document[key]} limit={limit} multiline={key === 'introduction'} onChange={value => update(next => { next[key] = value })} />) }
    <Disclosure key={`${scope}:${projectId}:about`} title="About / Contact" reviewRequest={reviewRequest}>
    <Field inputRef={target({ field: 'about' })} label="About" value={document.about} limit={8000} multiline onChange={value => update(next => { next.about = value })} />
    <Field inputRef={target({ field: 'contact.email' })} label="Contact email" value={document.contact.email} limit={254} onChange={value => update(next => { next.contact.email = value })} />
    <Field inputRef={target({ field: 'contact.location' })} label="Location" value={document.contact.location} limit={160} onChange={value => update(next => { next.contact.location = value })} />
    </Disclosure>
    <h3>Galleries</h3><p>Add photos from this project. At least one photo per gallery is required to save.</p>
    <label><span>Photo layout</span><select value={document.galleryLayout || 'grid'} onChange={event => update(next => {
      if (event.target.value === 'single') next.galleryLayout = 'single'
      else delete next.galleryLayout
    })}><option value="grid">Grid</option><option value="single">Single column</option></select></label>
    <button type="button" disabled={loading || !scope} onClick={loadPhotos}>{loading ? 'Loading photos…' : 'Load project photos'}</button>
    <details onToggle={event => setShowIntake(event.currentTarget.open)}><summary>Upload project photos</summary>{showIntake && <PhotoMaterialIntake projectId={projectId} onUploaded={loadPhotos} />}</details>
    {error && <p role="alert">{error}</p>}
    {document.categories.map((item, ci) => <section className="portfolio-editor__category" key={`${scope}:${projectId}:${identity(item)}`} aria-label={`Gallery ${ci + 1}`}>
      <Disclosure title={`${item.title || `Gallery ${ci + 1}`} · ${item.images.length} ${item.images.length === 1 ? 'photo' : 'photos'}`} initiallyOpen={added.current.has(identity(item)) || !item.images.length} reviewRequest={reviewRequest}>
      <Field inputRef={target({ field: 'gallery.title', gallery: item.slug })} label="Category title" value={item.title} limit={120} onChange={value => category(ci, next => { next.title = value })} />
      <Field inputRef={target({ field: 'gallery.slug', gallery: item.slug })} label="Category slug" value={item.slug} limit={48} onChange={value => category(ci, next => { next.slug = value })} />
      <Field inputRef={target({ field: 'gallery.description', gallery: item.slug })} label="Category description" value={item.description} limit={1200} multiline onChange={value => category(ci, next => { next.description = value })} />
      <label><span>Hero image</span><select value={item.hero} disabled={!item.images.length} onChange={event => category(ci, next => { next.hero = Number(event.target.value) })}>{item.images.map((entry, ii) => <option key={ii} value={ii}>{entry.title || entry.alt || `Image ${ii + 1}`}</option>)}</select></label>
      <label><span>Add project photo to {item.title || `gallery ${ci + 1}`}</span><select value="" disabled={count >= 200 || !scope} onChange={event => addPhoto(item, event.target.value, event.currentTarget)}><option value="">Choose an uploaded photo</option>{photos.filter(photo => !item.images.some(entry => entry.material_id === photo.material_id)).map(photo => <option key={photo.material_id} value={photo.material_id}>{photo.name}</option>)}</select></label>
      {photos.length > 0 && <div className="portfolio-editor__photo-picker" role="group" aria-label={`Choose photos for ${item.title || `gallery ${ci + 1}`}`}>
        {photos.filter(photo => !item.images.some(entry => entry.material_id === photo.material_id)).map(photo => <button key={`${photo.material_id}:${photo.preview_sha256}`} type="button" className="portfolio-editor__photo-choice" disabled={count >= 200 || !scope} aria-label={`Add ${photo.name} to ${item.title || `gallery ${ci + 1}`}`} onClick={event => addPhoto(item, photo.material_id, event.currentTarget)}>
          <Photo image={photoReference(photo)} projectId={projectId} scope={scope} />
          <span>{photo.name}</span>
        </button>)}
        {photos.every(photo => item.images.some(entry => entry.material_id === photo.material_id)) && <p>All project photos are already in this gallery.</p>}
      </div>}
      {count >= 200 && <p role="status">The portfolio has reached its 200 photo limit. Remove a photo before adding another.</p>}
      {!loading && !photos.length && <p>Use Load project photos after uploading in Photos.</p>}
      {item.images.map((entry, ii) => <section key={entry.material_id} aria-label={`Image ${ii + 1}`}>
        <Disclosure title={`Photo ${ii + 1}: ${entry.title || entry.alt || 'Untitled photo'}`} initiallyOpen={added.current.has(`${identity(item)}:${entry.material_id}`)} reviewRequest={reviewRequest}>
        <Field inputRef={target({ field: 'caption', gallery: item.slug, material: entry.material_id })} label={`Caption ${ii + 1}`} value={entry.title} limit={200} onChange={value => image(ci, ii, next => { next.title = value })} />
        <Field inputRef={target({ field: 'alt', gallery: item.slug, material: entry.material_id })} label={`Alt text ${ii + 1}`} value={entry.alt} limit={400} onChange={value => image(ci, ii, next => { next.alt = value })} />
        <div className="project-page-editor__actions">
          <button type="button" disabled={ii === 0} onClick={() => move(ci, ii, -1)}>Move image {ii + 1} up</button>
          <button type="button" disabled={ii === item.images.length - 1} onClick={() => move(ci, ii, 1)}>Move image {ii + 1} down</button>
          <button type="button" onClick={() => removeImage(ci, ii)}>Remove image {ii + 1}</button>
        </div>
        <label><span>Assign image {ii + 1} to gallery</span><select value={ci} onChange={event => {
          const target = Number(event.target.value)
          added.current.add(identity(document.categories[target]))
          added.current.add(`${identity(document.categories[target])}:${entry.material_id}`)
          update(next => {
            const source = next.categories[ci], hero = source.images[source.hero]
            const [moved] = source.images.splice(ii, 1)
            source.hero = Math.max(0, source.images.indexOf(hero)); next.categories[target].images.push(moved)
          })
        }}>{document.categories.map((destination, index) => <option key={index} value={index} disabled={index !== ci && destination.images.some(photo => photo.material_id === entry.material_id)}>{destination.title}</option>)}</select></label>
        </Disclosure>
      </section>)}
      <button type="button" disabled={document.categories.length <= 1} onClick={() => update(next => { next.categories.splice(ci, 1) })}>Remove gallery {ci + 1}</button>
      {[-1, 1].map(delta => <button key={delta} type="button" disabled={ci + delta < 0 || ci + delta >= document.categories.length} onClick={() => update(next => {
        const [moved] = next.categories.splice(ci, 1); next.categories.splice(ci + delta, 0, moved)
      })}>Move gallery {ci + 1} {delta < 0 ? 'up' : 'down'}</button>)}
      </Disclosure>
    </section>)}
    <button type="button" disabled={document.categories.length >= 12} onClick={() => update(next => {
      let suffix = 1
      while (next.categories.some(item => item.slug === `gallery-${suffix}`)) suffix++
      next.categories.push(newItem({ slug: `gallery-${suffix}`, title: 'New gallery', description: '', hero: 0, images: [] }))
    })}>Add gallery</button>
    <h3>Resume</h3>
    {document.resume.map((section, si) => <section key={`${scope}:${projectId}:${identity(section)}`} aria-label={`Resume section ${si + 1}`}>
      <Disclosure title={section.heading || `Resume section ${si + 1}`} initiallyOpen={added.current.has(identity(section))} reviewRequest={reviewRequest}>
      <Field inputRef={target({ field: 'resume.heading', section: selectionIdentity(section) })} label="Section heading" value={section.heading} limit={120} onChange={value => update(next => { next.resume[si].heading = value })} />
      {section.entries.map((entry, ei) => <section key={selectionIdentity(entry)} aria-label={`Resume entry ${ei + 1}`}>
        {[['title', 200], ['subtitle', 240], ['dates', 120], ['description', 2400]].map(([field, limit]) => <Field key={field} inputRef={target({ field: `resume.${field}`, section: selectionIdentity(section), entry: selectionIdentity(entry) })} label={`Entry ${field}`} value={entry[field]} limit={limit} multiline={field === 'description'} onChange={value => update(next => { next.resume[si].entries[ei][field] = value })} />)}
        <button type="button" onClick={() => update(next => { next.resume[si].entries.splice(ei, 1) })}>Remove entry {ei + 1}</button>
        {[-1, 1].map(delta => <button key={delta} type="button" disabled={ei + delta < 0 || ei + delta >= section.entries.length} onClick={() => update(next => {
          const [moved] = next.resume[si].entries.splice(ei, 1); next.resume[si].entries.splice(ei + delta, 0, moved)
        })}>Move entry {ei + 1} {delta < 0 ? 'up' : 'down'}</button>)}
      </section>)}
      <button type="button" disabled={section.entries.length >= 20} onClick={() => update(next => { next.resume[si].entries.push({ title: '', subtitle: '', dates: '', description: '' }) })}>Add resume entry</button>
      <button type="button" onClick={() => update(next => { next.resume.splice(si, 1) })}>Remove resume section {si + 1}</button>
      {[-1, 1].map(delta => <button key={delta} type="button" disabled={si + delta < 0 || si + delta >= document.resume.length} onClick={() => update(next => {
        const [moved] = next.resume.splice(si, 1); next.resume.splice(si + delta, 0, moved)
      })}>Move resume section {si + 1} {delta < 0 ? 'up' : 'down'}</button>)}
      </Disclosure>
    </section>)}
    <button type="button" disabled={document.resume.length >= 8} onClick={() => update(next => { next.resume.push(newItem({ heading: 'Experience', entries: [] })) })}>Add resume section</button>
  </div>
}
