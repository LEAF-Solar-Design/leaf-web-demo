import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { readActiveScope, subscribeActiveScope } from '../auth/activeScope.js'
import { listProjectPhotos, readProjectPhotoPreview, uploadProjectPhoto } from '../projects/api.js'
import { humanizeError } from '../errorHumanize.js'
import './photoMaterialIntake.css'

const message = error => humanizeError(error?.message || 'Photos could not be loaded. Try again.')
const mergeRows = (rows, added) => [...new Map([...rows, ...added].map(row => [row.material_id, row])).values()]

// Remount every local byte, request and object URL when authority changes.
export default function PhotoMaterialIntake({ projectId }) {
  const scope = useSyncExternalStore(subscribeActiveScope, readActiveScope, () => null)
  if (!projectId || !scope) return <p>Sign in and open a project to add photos.</p>
  return <PhotoSession key={`${scope}:${projectId}`} projectId={projectId} scope={scope} />
}

function PhotoSession({ projectId, scope }) {
  const [photos, setPhotos] = useState([])
  const [previews, setPreviews] = useState({})
  const [queue, setQueue] = useState([])
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [cursor, setCursor] = useState(null)
  const input = useRef(null)
  const live = useRef(false)
  const running = useRef(false)
  const abort = useRef(null)
  const urls = useRef(new Map())
  const current = () => live.current && readActiveScope() === scope
  const options = () => ({ signal: abort.current.signal, expectedScope: scope })
  const preview = async photo => {
    if (!current() || urls.current.has(photo.material_id)) return
    try {
      const blob = await readProjectPhotoPreview(projectId, photo.material_id, options())
      if (!current() || urls.current.has(photo.material_id)) return
      const url = URL.createObjectURL(blob)
      urls.current.set(photo.material_id, url)
      setPreviews(previous => ({ ...previous, [photo.material_id]: url }))
    } catch (cause) {
      if (current() && cause?.name !== 'AbortError') setError(message(cause))
    }
  }
  const load = async (next = null) => {
    setLoading(true)
    setError('')
    try {
      const result = await listProjectPhotos(projectId, { ...options(), cursor: next })
      if (!current()) return
      const rows = result.materials || []
      setPhotos(previous => mergeRows(previous, rows))
      setCursor(result.next_cursor || null)
      for (const photo of rows) {
        if (!current()) break
        await preview(photo)
      }
    } catch (cause) {
      if (current() && cause?.name !== 'AbortError') setError(message(cause))
    } finally { if (current()) setLoading(false) }
  }
  useEffect(() => {
    live.current = true
    abort.current = new AbortController()
    void load()
    return () => {
      live.current = false
      abort.current.abort()
      for (const url of urls.current.values()) URL.revokeObjectURL(url)
      urls.current.clear()
    }
  }, [projectId, scope])

  const run = async items => {
    if (running.current || !current()) return
    running.current = true
    setBusy(true)
    setError('')
    setQueue(items)
    const update = (index, state, detail = '') => setQueue(previous => previous.map((item, i) => i === index ? { ...item, state, detail } : item))
    try {
      for (let index = 0; index < items.length && current(); index++) {
        const item = items[index]
        if (item.state === 'ready') continue
        update(index, 'uploading')
        try {
          const result = await uploadProjectPhoto(projectId, item.file, options())
          if (!current()) break
          setPhotos(previous => mergeRows(previous, [result.material]))
          update(index, 'ready')
          await preview(result.material)
        } catch (cause) {
          if (!current()) break
          update(index, 'failed', message(cause))
        }
      }
    } finally {
      running.current = false
      if (current()) setBusy(false)
    }
  }
  const choose = event => {
    const files = Array.from(event.target.files || [])
    event.target.value = ''
    if (running.current || !files.length) return
    if (files.length > 200) { setError('Choose up to 200 photos at a time.'); return }
    void run(files.map(file => ({ file, state: 'waiting', detail: '' })))
  }
  const ready = queue.filter(item => item.state === 'ready').length
  const failed = queue.filter(item => item.state === 'failed').length
  return <section className="project-photos" aria-label="Project photos">
    <h3>Photos</h3>
    <p>Add JPEG, PNG or HEIC photos. Originals are kept with a web preview.</p>
    <input ref={input} type="file" aria-label="Photo files" accept=".jpg,.jpeg,.png,.heic,.heif" multiple hidden disabled={busy} onChange={choose} />
    <button type="button" disabled={busy} onClick={() => input.current?.click()}>Add photos</button>
    <p role="status">{busy ? `Adding photos: ${ready} of ${queue.length} ready.` : queue.length ? `${ready} ${ready === 1 ? 'photo' : 'photos'} ready.${failed ? ` ${failed} need a retry.` : ''}` : loading ? 'Loading photos…' : `${photos.length} photos in this project.`}</p>
    {error && <p role="alert">{error} <button type="button" disabled={loading || busy} onClick={() => void load()}>Reload photos</button></p>}
    {failed > 0 && !busy && <button type="button" onClick={() => void run(queue)}>Retry failed photos</button>}
    {queue.some(item => item.state !== 'ready') && <ul aria-label="Photo upload progress">{queue.map((item, index) => item.state === 'ready' ? null : <li key={index}>
      <span>{item.file.name}</span> <span>{item.state === 'failed' ? item.detail : item.state === 'uploading' ? 'Uploading…' : 'Waiting'}</span>
    </li>)}</ul>}
    <ul className="project-photo-grid" aria-label="Saved photos">{photos.map(photo => <li key={photo.material_id}>
      {previews[photo.material_id] && <img src={previews[photo.material_id]} alt={photo.name} width="128" height="104" />}
      <span>{photo.name}</span><small>Ready</small>
    </li>)}</ul>
    {cursor && <button type="button" disabled={loading || busy} onClick={() => void load(cursor)}>Load more photos</button>}
  </section>
}
