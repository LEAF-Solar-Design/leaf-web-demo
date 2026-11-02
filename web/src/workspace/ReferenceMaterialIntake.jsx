import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { readActiveScope, subscribeActiveScope } from '../auth/activeScope.js'
import { subscribeBeforeLogout } from '../auth.js'
import { listProjectReferences, readProjectReferenceOriginal, uploadProjectReference } from '../projects/api.js'
import { humanizeError } from '../errorHumanize.js'

const message = error => humanizeError(error?.message || 'Reference files could not be loaded. Try again.')
const merge = (rows, added) => [...new Map([...rows, ...added].map(row => [row.material_id, row])).values()]

export default function ReferenceMaterialIntake({ projectId }) {
  const scope = useSyncExternalStore(subscribeActiveScope, readActiveScope, () => null)
  if (!projectId || !scope) return <section aria-label="Reference files">
    <h3>Reference files</h3><p>Sign in and open a project to add original reference files.</p>
  </section>
  return <ReferenceSession key={`${scope}:${projectId}`} projectId={projectId} scope={scope} />
}

function ReferenceSession({ projectId, scope }) {
  const [rows, setRows] = useState([])
  const [queue, setQueue] = useState([])
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [cursor, setCursor] = useState(null)
  const [error, setError] = useState('')
  const [downloading, setDownloading] = useState(null)
  const input = useRef(null)
  const live = useRef(false)
  const running = useRef(false)
  const downloadRunning = useRef(false)
  const abort = useRef(null)
  const urls = useRef(new Map())
  const current = () => live.current && readActiveScope() === scope
  const options = () => ({ signal: abort.current.signal, expectedScope: scope })
  const load = async (next = null) => {
    if (!current()) return
    setLoading(true)
    setError('')
    try {
      const result = await listProjectReferences(projectId, { ...options(), cursor: next })
      if (!current()) return
      setRows(previous => next ? merge(previous, result.materials) : result.materials)
      setCursor(result.next_cursor || null)
    } catch (cause) {
      if (current() && cause?.name !== 'AbortError') setError(message(cause))
    } finally { if (current()) setLoading(false) }
  }
  useEffect(() => {
    live.current = true
    abort.current = new AbortController()
    void load()
    const clear = () => {
      live.current = false
      abort.current.abort()
      for (const [url, timer] of urls.current) { clearTimeout(timer); URL.revokeObjectURL(url) }
      urls.current.clear()
      setRows([]); setQueue([]); setBusy(false); setLoading(false); setError(''); setDownloading(null)
    }
    const logout = subscribeBeforeLogout(clear)
    return () => { clear(); logout() }
  }, [projectId, scope])

  const run = async items => {
    if (running.current || !current()) return
    running.current = true
    setBusy(true)
    setQueue(items)
    setError('')
    const update = (index, state, detail = '') => setQueue(previous => previous.map((item, i) => i === index ? { ...item, state, detail } : item))
    try {
      for (let index = 0; index < items.length && current(); index++) {
        const item = items[index]
        if (item.state === 'uploaded') continue
        update(index, 'uploading')
        try {
          const result = await uploadProjectReference(projectId, item.file, options())
          if (!current()) break
          setRows(previous => merge(previous, [result.material]))
          update(index, 'uploaded')
        } catch (cause) {
          if (!current()) break
          update(index, 'failed', message(cause))
        }
      }
    } finally { running.current = false; if (current()) setBusy(false) }
  }
  const choose = event => {
    const files = Array.from(event.target.files || [])
    event.target.value = ''
    if (!current() || running.current || loading || !files.length) return
    if (files.length > 20) { setError('Choose up to 20 reference files at a time.'); return }
    void run(files.map(file => ({ file, state: 'waiting', detail: '' })))
  }
  const download = async row => {
    if (!current() || downloadRunning.current) return
    downloadRunning.current = true
    setDownloading(row.material_id)
    setError('')
    try {
      const blob = await readProjectReferenceOriginal(projectId, row.material_id, options())
      if (!current()) return
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = row.name
      document.body.appendChild(link)
      link.click()
      link.remove()
      // Keep the URL long enough for the browser to start saving; revoke on logout too.
      const timer = setTimeout(() => { URL.revokeObjectURL(url); urls.current.delete(url) }, 1000)
      urls.current.set(url, timer)
    } catch (cause) {
      if (current() && cause?.name !== 'AbortError') setError(message(cause))
    } finally { downloadRunning.current = false; if (current()) setDownloading(null) }
  }
  const failed = queue.filter(item => item.state === 'failed').length
  const uploaded = queue.filter(item => item.state === 'uploaded').length
  return <section aria-label="Reference files">
    <h3>Reference files</h3>
    <p>Original MOV up to 64 MiB; UTF-8 TXT and JSON up to 8 MiB each. Originals stay with this shared project.</p>
    <p>Preview unavailable. These files have not been consumed by a model. Uploading does not start an author turn or build.</p>
    <input ref={input} type="file" aria-label="Original reference files" accept=".mov,.txt,.json" multiple hidden disabled={busy || loading} onChange={choose} />
    <button type="button" disabled={busy || loading} onClick={() => input.current?.click()}>Add reference files</button>
    <p role="status">{busy ? `Uploading reference files: ${uploaded} of ${queue.length} uploaded.` : loading ? 'Loading reference files…' : `${rows.length} reference files in this project.${queue.length ? ` ${uploaded} uploaded; ${failed} need a retry.` : ''}`}</p>
    {error && <p role="alert">{error} <button type="button" disabled={busy || loading} onClick={() => void load()}>Reload reference files</button></p>}
    {failed > 0 && !busy && <button type="button" onClick={() => void run(queue)}>Retry failed reference files</button>}
    {queue.length > 0 && <ul aria-label="Reference upload progress">{queue.map((item, index) => <li key={index}>
      {item.file.name}: {item.state === 'failed' ? item.detail : item.state === 'uploaded' ? 'Uploaded' : item.state === 'uploading' ? 'Uploading…' : 'Waiting'}
    </li>)}</ul>}
    {rows.length > 0 && <ul aria-label="Saved reference files">{rows.map(row => <li key={row.material_id}>
      <span>{row.name}</span>{' '}<span>{row.byte_size} bytes · {row.media_type}</span>{' '}
      <span>Uploaded · Preview unavailable · Model not consumed</span>{' '}
      <small>ID: {row.material_id} · SHA-256: {row.sha256}</small>{' '}
      <time dateTime={row.created_at}>{new Date(row.created_at).toLocaleString()}</time>{' '}
      <button type="button" disabled={downloading !== null} onClick={() => void download(row)}>
        {downloading === row.material_id ? 'Downloading…' : `Download original ${row.name}`}
      </button>
    </li>)}</ul>}
    {cursor && <button type="button" disabled={loading || busy} onClick={() => void load(cursor)}>Load more reference files</button>}
  </section>
}
