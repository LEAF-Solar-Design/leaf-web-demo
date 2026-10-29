import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { readActiveScope, subscribeActiveScope } from '../auth/activeScope.js'
import { subscribeBeforeLogout, subscribeTokenStored } from '../auth.js'
import { readProjectPhotoPreview } from '../projects/api.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { photoReference, projectPortfolioPhotos } from './portfolioDocument.js'
import './portfolioEditor.css'

const selectionIdentities = new WeakMap()
let selectionSerial = 0
const selectionIdentity = item => {
  if (!selectionIdentities.has(item)) selectionIdentities.set(item, String(++selectionSerial))
  return selectionIdentities.get(item)
}
export const portfolioField = (document, descriptor) => ({ ...descriptor, document: selectionIdentity(document) })
const selectionKey = descriptor => {
  if (!descriptor || typeof descriptor !== 'object' || Array.isArray(descriptor)
    || Object.values(descriptor).some(value => typeof value !== 'string')) return null
  return JSON.stringify(Object.entries(descriptor).sort(([a], [b]) => a.localeCompare(b)))
}

function Photo({ image, projectId, scope }) {
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
    {url ? <img src={url} alt={image.alt} onError={() => setError('Photo could not be displayed. Reopen this view to retry.')} /> : !error && <span role="status">{scope ? 'Loading photo…' : 'Sign in to view this photo.'}</span>}
    {error && <span role="alert">{error}</span>}
  </span>
}

export function PortfolioPreview({ document, projectId, onSelectField }) {
  const scope = useSyncExternalStore(subscribeActiveScope, readActiveScope, () => null)
  return <PortfolioView key={`${scope}:${projectId}`} document={document} projectId={projectId} scope={scope} onSelectField={onSelectField} />
}

function PortfolioView({ document, projectId, scope, onSelectField }) {
  const [view, setView] = useState('home')
  const [selected, setSelected] = useState(null)
  const trigger = useRef(null), closeButton = useRef(null), surface = useRef(null)
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
  const photo = image => <Photo key={`${image.material_id}:${image.preview_sha256}`} image={image} projectId={projectId} scope={scope} />
  const edit = (descriptor, label) => onSelectField && <button type="button" className="portfolio__edit" aria-label={`Edit ${label}`} onClick={() => onSelectField(portfolioField(document, descriptor))}>Edit {label}</button>
  const galleryEdits = item => <>{edit({ field: 'gallery.title', gallery: item.slug }, `${item.title} gallery title`)}{edit({ field: 'gallery.description', gallery: item.slug }, `${item.title} gallery description`)}</>
  return <main className="portfolio" ref={surface}>
    <nav aria-label="Portfolio">
      <button onClick={() => navigate('home')} aria-current={view === 'home' ? 'page' : undefined}>{document.name}</button>
      {document.categories.map((item, index) => <button key={index} onClick={() => navigate(item.slug)} aria-current={view === item.slug ? 'page' : undefined}>{item.title}</button>)}
      <button onClick={() => navigate('about')}>About / Contact</button><button onClick={() => navigate('resume')}>Resume</button>
    </nav>
    {edit({ field: 'name' }, 'name')}
    {view === 'about' ? <><h1>About {document.name}</h1><p>{document.about}</p>{edit({ field: 'about' }, 'About')}<h2>Contact</h2><p>{document.contact.location}</p>{edit({ field: 'contact.location' }, 'location')}<p>{document.contact.email}</p>{edit({ field: 'contact.email' }, 'contact email')}</>
      : view === 'resume' ? <><h1>Resume</h1>{document.resume.map((section, index) => <section key={index}><h2>{section.heading}</h2>{edit({ field: 'resume.heading', section: selectionIdentity(section) }, `resume section ${index + 1} heading`)}{section.entries.map((entry, i) => <article key={i}><h3>{entry.title}</h3><p>{entry.subtitle}</p><p>{entry.dates}</p><p>{entry.description}</p>{['title', 'subtitle', 'dates', 'description'].map(field => <span key={field}>{edit({ field: `resume.${field}`, section: selectionIdentity(section), entry: selectionIdentity(entry) }, `resume section ${index + 1} entry ${i + 1} ${field}`)}</span>)}</article>)}</section>)}</>
        : category ? <><h1>{category.title}</h1><p>{category.description}</p>{galleryEdits(category)}<div className="portfolio-grid">{category.images.map(image => <figure key={image.material_id}><button onClick={event => { trigger.current = event.currentTarget; setSelected(image) }} aria-label={`View ${image.alt}`}>
          {photo(image)}<span>{image.title}</span>
        </button>{edit({ field: 'caption', gallery: category.slug, material: image.material_id }, `${category.title} caption for ${image.alt}`)}</figure>)}</div></>
          : <><h1>{document.headline}</h1>{edit({ field: 'headline' }, 'headline')}<p>{document.introduction}</p>{edit({ field: 'introduction' }, 'introduction')}<div className="portfolio-grid">{document.categories.map((item, index) => <div key={index}><button onClick={() => navigate(item.slug)}>
            {item.images[item.hero] ? photo(item.images[item.hero]) : <span>Add photos in the Project panel.</span>}<h2>{item.title}</h2><p>{item.description}</p>
          </button>{galleryEdits(item)}</div>)}</div></>}
    {selected && <div className="portfolio-viewer" role="dialog" aria-modal="true" aria-label={selected.alt}>
      <button ref={closeButton} onClick={close}>Close image</button>{photo(selected)}<p>{selected.title}</p>
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

export default function PortfolioEditor({ document, onChange, projectId, reviewRequest, selectedField }) {
  const scope = useSyncExternalStore(subscribeActiveScope, readActiveScope, () => null)
  const [photos, setPhotos] = useState([]), [error, setError] = useState(''), [loading, setLoading] = useState(false)
  const operation = useRef(null)
  const fields = useRef(new Map())
  const target = descriptor => node => {
    const key = selectionKey(portfolioField(document, descriptor))
    if (node) fields.current.set(key, node)
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
  useEffect(() => {
    setPhotos([]); setError('')
    const clear = () => { operation.current?.abort(); operation.current = null; setPhotos([]); setLoading(false) }
    const logout = subscribeBeforeLogout(clear)
    return () => { clear(); logout() }
  }, [projectId, scope])
  const loadPhotos = async () => {
    if (!scope || operation.current) return
    const controller = new AbortController(); operation.current = controller; setLoading(true); setError('')
    try {
      const rows = await projectPortfolioPhotos(projectId, { signal: controller.signal, expectedScope: scope })
      if (!controller.signal.aborted && readActiveScope() === scope) setPhotos(rows)
    } catch (failure) { if (!controller.signal.aborted && readActiveScope() === scope) setError(failure.message) }
    finally { if (operation.current === controller) { operation.current = null; setLoading(false) } }
  }
  const update = action => {
    const next = JSON.parse(JSON.stringify(document))
    selectionIdentities.set(next, selectionIdentity(document))
    for (const field of ['categories', 'resume']) document[field].forEach((item, index) => {
      identities.current.set(next[field][index], identity(item))
    })
    document.resume.forEach((section, si) => {
      selectionIdentities.set(next.resume[si], selectionIdentity(section))
      section.entries.forEach((entry, ei) => selectionIdentities.set(next.resume[si].entries[ei], selectionIdentity(entry)))
    })
    action(next); onChange(next)
  }
  const category = (index, action) => update(next => action(next.categories[index]))
  const image = (ci, ii, action) => category(ci, next => action(next.images[ii]))
  const count = document.categories.reduce((sum, item) => sum + item.images.length, 0)
  const move = (ci, ii, delta) => category(ci, next => {
    const hero = next.images[next.hero], target = ii + delta
    ;[next.images[ii], next.images[target]] = [next.images[target], next.images[ii]]
    next.hero = next.images.indexOf(hero)
  })
  const removeImage = (ci, ii) => category(ci, next => {
    const hero = next.images[next.hero]
    next.images.splice(ii, 1); next.hero = Math.max(0, next.images.indexOf(hero))
  })
  return <div className="portfolio-editor">
    { [['name', 'Name', 120], ['headline', 'Headline', 240], ['introduction', 'Introduction', 2400]].map(([key, label, limit]) => <Field key={key} inputRef={target({ field: key })} label={label} value={document[key]} limit={limit} multiline={key === 'introduction'} onChange={value => update(next => { next[key] = value })} />) }
    <Disclosure key={`${scope}:${projectId}:about`} title="About / Contact" reviewRequest={reviewRequest}>
    <Field inputRef={target({ field: 'about' })} label="About" value={document.about} limit={8000} multiline onChange={value => update(next => { next.about = value })} />
    <Field inputRef={target({ field: 'contact.email' })} label="Contact email" value={document.contact.email} limit={254} onChange={value => update(next => { next.contact.email = value })} />
    <Field inputRef={target({ field: 'contact.location' })} label="Location" value={document.contact.location} limit={160} onChange={value => update(next => { next.contact.location = value })} />
    </Disclosure>
    <h3>Galleries</h3><p>Add photos from this project. At least one photo per gallery is required to save.</p>
    <button type="button" disabled={loading || !scope} onClick={loadPhotos}>{loading ? 'Loading photos…' : 'Load project photos'}</button>
    {error && <p role="alert">{error}</p>}
    {document.categories.map((item, ci) => <section className="portfolio-editor__category" key={`${scope}:${projectId}:${identity(item)}`} aria-label={`Gallery ${ci + 1}`}>
      <Disclosure title={`${item.title || `Gallery ${ci + 1}`} · ${item.images.length} ${item.images.length === 1 ? 'photo' : 'photos'}`} initiallyOpen={added.current.has(identity(item)) || !item.images.length} reviewRequest={reviewRequest}>
      <Field inputRef={target({ field: 'gallery.title', gallery: item.slug })} label="Category title" value={item.title} limit={120} onChange={value => category(ci, next => { next.title = value })} />
      <Field label="Category slug" value={item.slug} limit={48} onChange={value => category(ci, next => { next.slug = value })} />
      <Field inputRef={target({ field: 'gallery.description', gallery: item.slug })} label="Category description" value={item.description} limit={1200} multiline onChange={value => category(ci, next => { next.description = value })} />
      <label><span>Hero image</span><select value={item.hero} disabled={!item.images.length} onChange={event => category(ci, next => { next.hero = Number(event.target.value) })}>{item.images.map((entry, ii) => <option key={ii} value={ii}>{entry.title || entry.alt || `Image ${ii + 1}`}</option>)}</select></label>
      <label><span>Add project photo to {item.title || `gallery ${ci + 1}`}</span><select value="" disabled={count >= 200} onChange={event => {
        const row = photos.find(photo => photo.material_id === event.target.value)
        if (row) {
          added.current.add(identity(item))
          added.current.add(`${identity(item)}:${row.material_id}`)
          category(ci, next => { next.images.push(photoReference(row)) })
        }
      }}><option value="">Choose an uploaded photo</option>{photos.filter(photo => !item.images.some(entry => entry.material_id === photo.material_id)).map(photo => <option key={photo.material_id} value={photo.material_id}>{photo.name}</option>)}</select></label>
      {!loading && !photos.length && <p>Use Load project photos after uploading in Photos.</p>}
      {item.images.map((entry, ii) => <section key={entry.material_id} aria-label={`Image ${ii + 1}`}>
        <Disclosure title={`Photo ${ii + 1}: ${entry.title || entry.alt || 'Untitled photo'}`} initiallyOpen={added.current.has(`${identity(item)}:${entry.material_id}`)} reviewRequest={reviewRequest}>
        <Field inputRef={target({ field: 'caption', gallery: item.slug, material: entry.material_id })} label={`Caption ${ii + 1}`} value={entry.title} limit={200} onChange={value => image(ci, ii, next => { next.title = value })} />
        <Field label={`Alt text ${ii + 1}`} value={entry.alt} limit={400} onChange={value => image(ci, ii, next => { next.alt = value })} />
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
      {section.entries.map((entry, ei) => <section key={ei} aria-label={`Resume entry ${ei + 1}`}>
        {[['title', 200], ['subtitle', 240], ['dates', 120], ['description', 2400]].map(([field, limit]) => <Field key={field} inputRef={target({ field: `resume.${field}`, section: selectionIdentity(section), entry: selectionIdentity(entry) })} label={`Entry ${field}`} value={entry[field]} limit={limit} multiline={field === 'description'} onChange={value => update(next => { next.resume[si].entries[ei][field] = value })} />)}
        <button type="button" onClick={() => update(next => { next.resume[si].entries.splice(ei, 1) })}>Remove entry {ei + 1}</button>
      </section>)}
      <button type="button" disabled={section.entries.length >= 20} onClick={() => update(next => { next.resume[si].entries.push({ title: '', subtitle: '', dates: '', description: '' }) })}>Add resume entry</button>
      <button type="button" onClick={() => update(next => { next.resume.splice(si, 1) })}>Remove resume section {si + 1}</button>
      </Disclosure>
    </section>)}
    <button type="button" disabled={document.resume.length >= 8} onClick={() => update(next => { next.resume.push(newItem({ heading: 'Experience', entries: [] })) })}>Add resume section</button>
  </div>
}
