import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { readActiveScope, subscribeActiveScope } from '../auth/activeScope.js'
import { subscribeBeforeLogout, subscribeTokenStored } from '../auth.js'
import { readProjectPhotoPreview } from '../projects/api.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { photoReference, projectPortfolioPhotos } from './portfolioDocument.js'
import './portfolioEditor.css'

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

export function PortfolioPreview({ document, projectId }) {
  const scope = useSyncExternalStore(subscribeActiveScope, readActiveScope, () => null)
  return <PortfolioView key={`${scope}:${projectId}`} document={document} projectId={projectId} scope={scope} />
}

function PortfolioView({ document, projectId, scope }) {
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
  return <main className="portfolio" ref={surface}>
    <nav aria-label="Portfolio">
      <button onClick={() => navigate('home')} aria-current={view === 'home' ? 'page' : undefined}>{document.name}</button>
      {document.categories.map((item, index) => <button key={index} onClick={() => navigate(item.slug)} aria-current={view === item.slug ? 'page' : undefined}>{item.title}</button>)}
      <button onClick={() => navigate('about')}>About / Contact</button><button onClick={() => navigate('resume')}>Resume</button>
    </nav>
    {view === 'about' ? <><h1>About {document.name}</h1><p>{document.about}</p><h2>Contact</h2><p>{document.contact.location}</p><p>{document.contact.email}</p></>
      : view === 'resume' ? <><h1>Resume</h1>{document.resume.map((section, index) => <section key={index}><h2>{section.heading}</h2>{section.entries.map((entry, i) => <article key={i}><h3>{entry.title}</h3><p>{entry.subtitle}</p><p>{entry.dates}</p><p>{entry.description}</p></article>)}</section>)}</>
        : category ? <><h1>{category.title}</h1><p>{category.description}</p><div className="portfolio-grid">{category.images.map((image, i) => <figure key={i}><button onClick={event => { trigger.current = event.currentTarget; setSelected(image) }} aria-label={`View ${image.alt}`}>
          {photo(image)}<span>{image.title}</span>
        </button></figure>)}</div></>
          : <><h1>{document.headline}</h1><p>{document.introduction}</p><div className="portfolio-grid">{document.categories.map((item, index) => <button key={index} onClick={() => navigate(item.slug)}>
            {item.images[item.hero] ? photo(item.images[item.hero]) : <span>Add photos in the Project panel.</span>}<h2>{item.title}</h2><p>{item.description}</p>
          </button>)}</div></>}
    {selected && <div className="portfolio-viewer" role="dialog" aria-modal="true" aria-label={selected.alt}>
      <button ref={closeButton} onClick={close}>Close image</button>{photo(selected)}<p>{selected.title}</p>
    </div>}
  </main>
}

function Field({ label, value, limit, onChange, multiline = false }) {
  return <label><span>{label}</span>{multiline ? <textarea value={value} maxLength={limit} rows={4} onChange={event => onChange(event.target.value)} /> : <input value={value} maxLength={limit} onChange={event => onChange(event.target.value)} />}</label>
}

export default function PortfolioEditor({ document, onChange, projectId }) {
  const scope = useSyncExternalStore(subscribeActiveScope, readActiveScope, () => null)
  const [photos, setPhotos] = useState([]), [error, setError] = useState(''), [loading, setLoading] = useState(false)
  const operation = useRef(null)
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
  const update = action => { const next = JSON.parse(JSON.stringify(document)); action(next); onChange(next) }
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
    { [['name', 'Name', 120], ['headline', 'Headline', 240], ['introduction', 'Introduction', 2400], ['about', 'About', 8000]].map(([key, label, limit]) => <Field key={key} label={label} value={document[key]} limit={limit} multiline={['introduction', 'about'].includes(key)} onChange={value => update(next => { next[key] = value })} />) }
    <Field label="Contact email" value={document.contact.email} limit={254} onChange={value => update(next => { next.contact.email = value })} />
    <Field label="Location" value={document.contact.location} limit={160} onChange={value => update(next => { next.contact.location = value })} />
    <h3>Galleries</h3><p>Add photos from this project. At least one photo per gallery is required to save.</p>
    <button type="button" disabled={loading || !scope} onClick={loadPhotos}>{loading ? 'Loading photos…' : 'Load project photos'}</button>
    {error && <p role="alert">{error}</p>}
    {document.categories.map((item, ci) => <section className="portfolio-editor__category" key={ci} aria-label={`Gallery ${ci + 1}`}>
      <h4>Gallery {ci + 1}</h4>
      <Field label="Category title" value={item.title} limit={120} onChange={value => category(ci, next => { next.title = value })} />
      <Field label="Category slug" value={item.slug} limit={48} onChange={value => category(ci, next => { next.slug = value })} />
      <Field label="Category description" value={item.description} limit={1200} multiline onChange={value => category(ci, next => { next.description = value })} />
      <label><span>Hero image</span><select value={item.hero} disabled={!item.images.length} onChange={event => category(ci, next => { next.hero = Number(event.target.value) })}>{item.images.map((entry, ii) => <option key={ii} value={ii}>{entry.title || entry.alt || `Image ${ii + 1}`}</option>)}</select></label>
      <label><span>Add project photo to {item.title || `gallery ${ci + 1}`}</span><select value="" disabled={count >= 200} onChange={event => {
        const row = photos.find(photo => photo.material_id === event.target.value)
        if (row) category(ci, next => { next.images.push(photoReference(row)) })
      }}><option value="">Choose an uploaded photo</option>{photos.filter(photo => !item.images.some(entry => entry.material_id === photo.material_id)).map(photo => <option key={photo.material_id} value={photo.material_id}>{photo.name}</option>)}</select></label>
      {!loading && !photos.length && <p>Use Load project photos after uploading in Photos.</p>}
      {item.images.map((entry, ii) => <section key={entry.material_id} aria-label={`Image ${ii + 1}`}>
        <Field label={`Caption ${ii + 1}`} value={entry.title} limit={200} onChange={value => image(ci, ii, next => { next.title = value })} />
        <Field label={`Alt text ${ii + 1}`} value={entry.alt} limit={400} onChange={value => image(ci, ii, next => { next.alt = value })} />
        <div className="project-page-editor__actions">
          <button type="button" disabled={ii === 0} onClick={() => move(ci, ii, -1)}>Move image {ii + 1} up</button>
          <button type="button" disabled={ii === item.images.length - 1} onClick={() => move(ci, ii, 1)}>Move image {ii + 1} down</button>
          <button type="button" onClick={() => removeImage(ci, ii)}>Remove image {ii + 1}</button>
        </div>
        <label><span>Assign image {ii + 1} to gallery</span><select value={ci} onChange={event => {
          const target = Number(event.target.value)
          update(next => {
            const source = next.categories[ci], hero = source.images[source.hero]
            const [moved] = source.images.splice(ii, 1)
            source.hero = Math.max(0, source.images.indexOf(hero)); next.categories[target].images.push(moved)
          })
        }}>{document.categories.map((destination, index) => <option key={index} value={index} disabled={index !== ci && destination.images.some(photo => photo.material_id === entry.material_id)}>{destination.title}</option>)}</select></label>
      </section>)}
      <button type="button" disabled={document.categories.length <= 1} onClick={() => update(next => { next.categories.splice(ci, 1) })}>Remove gallery {ci + 1}</button>
    </section>)}
    <button type="button" disabled={document.categories.length >= 12} onClick={() => update(next => {
      let suffix = 1
      while (next.categories.some(item => item.slug === `gallery-${suffix}`)) suffix++
      next.categories.push({ slug: `gallery-${suffix}`, title: 'New gallery', description: '', hero: 0, images: [] })
    })}>Add gallery</button>
    <h3>Resume</h3>
    {document.resume.map((section, si) => <section key={si} aria-label={`Resume section ${si + 1}`}>
      <Field label="Section heading" value={section.heading} limit={120} onChange={value => update(next => { next.resume[si].heading = value })} />
      {section.entries.map((entry, ei) => <section key={ei} aria-label={`Resume entry ${ei + 1}`}>
        {[['title', 200], ['subtitle', 240], ['dates', 120], ['description', 2400]].map(([field, limit]) => <Field key={field} label={`Entry ${field}`} value={entry[field]} limit={limit} multiline={field === 'description'} onChange={value => update(next => { next.resume[si].entries[ei][field] = value })} />)}
        <button type="button" onClick={() => update(next => { next.resume[si].entries.splice(ei, 1) })}>Remove entry {ei + 1}</button>
      </section>)}
      <button type="button" disabled={section.entries.length >= 20} onClick={() => update(next => { next.resume[si].entries.push({ title: '', subtitle: '', dates: '', description: '' }) })}>Add resume entry</button>
      <button type="button" onClick={() => update(next => { next.resume.splice(si, 1) })}>Remove resume section {si + 1}</button>
    </section>)}
    <button type="button" disabled={document.resume.length >= 8} onClick={() => update(next => { next.resume.push({ heading: 'Experience', entries: [] }) })}>Add resume section</button>
  </div>
}
