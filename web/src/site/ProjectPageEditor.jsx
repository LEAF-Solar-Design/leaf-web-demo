import { useEffect, useId, useRef, useState } from 'react'
import { subscribeBeforeLogout, subscribeTokenStored } from '../auth.js'
import { composerDraftAccountScope } from '../lib/composerDraft.js'
import { downloadProjectPageSource, loadProjectPage, saveProjectPage, validatePageDocument, submitProjectNativePreview, loadProjectNativePreview, downloadProjectNativePreviewScreenshot, validateNativePreview } from './projectPageClient.js'
import PortfolioEditor, { PortfolioPreview } from './PortfolioEditor.jsx'
import { emptyPortfolio, importPortfolio } from './portfolioDocument.js'
import { readActiveScope, subscribeActiveScope } from '../auth/activeScope.js'
import './projectPageEditor.css'

const EMPTY_PAGE = { label: 'Your page', headline: 'Coming soon', description: '', status: '', theme: 'cream', alignment: 'center' }
const TEXT_FIELDS = [['label', 'Page label', 120], ['headline', 'Headline', 240], ['description', 'Description', 1200], ['status', 'Status text', 80]]
const stable = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item)
const equalDocument = (left, right) => left && right && (left.schema || right.schema ? stable(left) === stable(right) : Object.keys(EMPTY_PAGE).every(key => left[key] === right[key]))

export function PagePreview({ document, onSelectField, projectId }) {
  if (!document) return null
  if (document.schema === 'leaf.portfolio.v1') return <PortfolioPreview document={document} projectId={projectId} />
  const text = field => onSelectField
    ? <button type="button" className="project-page__edit" aria-label={`Edit ${field}`} onClick={() => onSelectField(field)}>{document[field]}</button>
    : document[field]
  return <main className="project-page" data-theme={document.theme} data-alignment={document.alignment}>
    <div className="project-page__content">
      <p className="project-page__label">{text('label')}</p>
      <h1 className="project-page__headline">{text('headline')}</h1>
      {document.description && <p className="project-page__description">{text('description')}</p>}
      {document.status && <p className="project-page__status">{text('status')}</p>}
    </div>
  </main>
}

export default function ProjectPageEditor(props) {
  return <ScopedPageEditor key={JSON.stringify([props.projectId || null, props.accountScope || null])} {...props} />
}

function ScopedPageEditor({ projectId, accountScope, onPreview, selectedField }) {
  const [draft, setDraft] = useState(null)
  const [saved, setSaved] = useState(null)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState('')
  const [suspended, setSuspended] = useState(false)
  const [native, setNative] = useState(null)
  const nativeSession = useRef(null)
  const live = useRef(false)
  const operation = useRef(null)
  const preview = useRef(onPreview)
  const fields = useRef({})
  const downloads = useRef(new Set())
  const id = useId()
  preview.current = onPreview
  const enabled = Boolean(projectId && accountScope && accountScope !== 'guest' && !suspended)
  const dirty = Boolean(draft && !equalDocument(draft, saved?.document))
  let validation = null
  if (draft) {
    try { validatePageDocument(draft) } catch (failure) { validation = failure.message }
  }

  const clearNative = () => {
    const session = nativeSession.current
    nativeSession.current = null
    if (session) {
      session.controller?.abort()
      clearTimeout(session.timer)
      clearTimeout(session.deadlineTimer)
      if (session.url) URL.revokeObjectURL(session.url)
    }
  }
  useEffect(() => {
    clearNative()
    setNative(null)
    return clearNative
  }, [saved?.revision, saved?.digest])

  useEffect(() => {
    live.current = true
    const invalidate = () => {
      live.current = false
      operation.current?.abort()
      operation.current = null
      clearNative()
      setNative(null)
      setDraft(null)
      setSaved(null)
      setError(null)
      setBusy(null)
      setSuspended(true)
      for (const url of downloads.current) URL.revokeObjectURL(url)
      downloads.current.clear()
    }
    const checkAccount = () => { if (composerDraftAccountScope() !== accountScope) invalidate() }
    const photoScope = readActiveScope()
    const activeScope = subscribeActiveScope(() => { if (readActiveScope() !== photoScope) invalidate() })
    const logout = subscribeBeforeLogout(invalidate)
    const token = subscribeTokenStored(checkAccount)
    const storage = event => { if (event.key === 'leaf.jwt' || event.key === null) checkAccount() }
    window.addEventListener('storage', storage)
    return () => {
      live.current = false
      operation.current?.abort()
      clearNative()
      logout(); token(); activeScope(); window.removeEventListener('storage', storage)
      for (const url of downloads.current) URL.revokeObjectURL(url)
      downloads.current.clear()
    }
  }, [accountScope])

  useEffect(() => { fields.current[typeof selectedField === 'string' ? selectedField : selectedField?.field]?.focus() }, [selectedField, draft !== null])

  const show = document => preview.current?.({ document, name: document.name || document.label || 'Page' })
  const create = () => {
    if (!enabled || operation.current) return
    const document = { ...EMPTY_PAGE }
    setDraft(document); setSaved(null); setError(null); setNotice('')
    show(document)
  }
  const createPortfolio = () => {
    if (!enabled || operation.current) return
    const document = emptyPortfolio()
    setDraft(document); setError(null); setNotice('')
    show(document)
  }
  const perform = async (kind, action) => {
    if (!enabled || operation.current) return
    const controller = new AbortController()
    operation.current = controller
    setBusy(kind); setError(null); setNotice('')
    const current = () => live.current && operation.current === controller && !controller.signal.aborted
    try { await action(controller.signal, current) }
    catch (failure) {
      if (current()) setError({ message: failure.message || 'The page action failed. Try again.', conflict: failure.status === 409 })
    } finally {
      if (current()) { operation.current = null; setBusy(null) }
    }
  }
  const open = () => perform('loading', async (signal, current) => {
    const page = await loadProjectPage(projectId, { signal })
    if (!current()) return
    if (!page) { setNotice('No saved page yet. Choose Create a page to begin.'); return }
    setSaved(page); setDraft(page.document)
    show(page.document)
  })
  const importContent = event => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    const expectedScope = readActiveScope()
    if (!expectedScope) { setError({ message: 'Sign in before importing project photos.' }); return }
    void perform('importing', async (signal, current) => {
      const document = await importPortfolio(file, projectId, { signal, expectedScope })
      if (!current() || readActiveScope() !== expectedScope) return
      setDraft(document); show(document)
      setNotice('Portfolio imported as an unsaved draft. Review it, then Save page.')
    })
  }
  const save = () => perform('saving', async (signal, current) => {
    const page = await saveProjectPage(projectId, draft, saved?.revision || 0, { signal })
    if (!current()) return
    setSaved(page); setDraft(page.document)
    show(page.document)
  })
  const download = (target = 'web') => perform('exporting', async (signal, current) => {
    const revision = saved.revision
    const blob = await downloadProjectPageSource(projectId, revision, target === 'ios' ? { signal, target } : { signal })
    if (!current()) return
    const url = URL.createObjectURL(blob)
    downloads.current.add(url)
    const link = window.document.createElement('a')
    link.href = url; link.download = `${target === 'ios' ? 'swiftui-portfolio' : 'nextjs-page'}-r${revision}.zip`
    window.document.body.appendChild(link)
    try { link.click() } finally {
      link.remove()
      setTimeout(() => { URL.revokeObjectURL(url); downloads.current.delete(url) }, 1000)
    }
    setNotice(target === 'ios'
      ? 'Native SwiftUI source downloaded from this saved portfolio. Open the Xcode project to build it. This does not install or publish an app.'
      : 'Next.js source downloaded. See the included README to run it independently.')
  })
  const change = (field, value) => {
    const document = { ...draft, [field]: value }
    setDraft(document); setNotice('')
    show(document)
  }
  const changePortfolio = document => { setDraft(document); setNotice(''); show(document) }
  const state = busy === 'saving' ? 'Saving…' : dirty ? 'Unsaved changes' : saved ? 'Saved' : ''

  const observeNative = async (session, submit = false) => {
    if (!live.current || nativeSession.current !== session || session.active) return
    session.active = true
    session.deadline = Date.now() + 15 * 60 * 1000
    const controller = new AbortController()
    session.controller = controller
    const current = () => live.current && nativeSession.current === session && !controller.signal.aborted
    const publish = extra => setNative({ revision: session.revision, job: session.job, url: session.url, active: session.active, ...extra })
    const stop = () => {
      clearTimeout(session.timer)
      clearTimeout(session.deadlineTimer)
      session.active = false
      session.controller = null
    }
    session.deadlineTimer = setTimeout(() => {
      if (!current()) return
      controller.abort()
      stop()
      publish({ message: 'Observation paused after 15 minutes. The build may still be running. Check again for progress.' })
    }, 15 * 60 * 1000)
    publish({})
    const step = async first => {
      try {
        const value = first && submit
          ? await submitProjectNativePreview(projectId, session.revision, session.key, { signal: controller.signal })
          : await loadProjectNativePreview(projectId, session.job.job_id, { signal: controller.signal })
        if (!current()) return
        // prepare_source copies page.digest into source.document_sha256.
        const job = validateNativePreview(value, session.job || { revision: session.revision, document_sha256: session.digest })
        session.job = job
        if (job.status !== 'complete' && session.url) {
          URL.revokeObjectURL(session.url)
          session.url = null
        }
        publish({})
        if (job.status === 'complete') {
          const blob = await downloadProjectNativePreviewScreenshot(projectId, job.job_id, { signal: controller.signal })
          if (!current()) return
          const url = URL.createObjectURL(blob)
          if (session.url) URL.revokeObjectURL(session.url)
          session.url = url
          stop(); publish({})
        } else if (['failed', 'cancelled'].includes(job.status)) {
          stop(); publish({})
        } else if (Date.now() >= session.deadline) {
          stop(); publish({ message: 'Observation paused. The build may still be running. Check again for progress.' })
        } else session.timer = setTimeout(() => { if (current()) void step(false) }, 2000)
      } catch {
        if (!current()) return
        if (session.url) { URL.revokeObjectURL(session.url); session.url = null }
        stop()
        publish({ message: session.job
          ? 'Could not check this preview or load its screenshot. Check again to retry.'
          : 'Submission could not be confirmed. Retry to check the same submission.' })
      }
    }
    await step(true)
  }
  const buildNative = () => {
    if (!enabled || operation.current || busy || dirty || validation || !saved
      || draft?.schema !== 'leaf.portfolio.v1' || nativeSession.current?.active) return
    let session = nativeSession.current
    if (session?.job && !['failed', 'cancelled'].includes(session.job.status)) return
    if (!session || session.job) {
      clearNative()
      session = { revision: saved.revision, digest: saved.digest, key: crypto.randomUUID(), job: null, url: null, active: false }
      nativeSession.current = session
    }
    void observeNative(session, true)
  }
  const checkNative = () => {
    const session = nativeSession.current
    if (enabled && !session?.active && session?.job) void observeNative(session)
  }
  const nativeStatus = native?.job?.status
  const nativeLabel = nativeStatus === 'complete' ? native.url ? 'Ready' : native.active ? 'Building' : 'Screenshot unavailable'
    : ({ queued: 'Queued', running: 'Building', failed: 'Failed', cancelled: 'Cancelled' })[nativeStatus]

  return <section className="project-page-editor" aria-label="Page editor">
    <h2>Page</h2>
    <p className="project-page-editor__note">Create a coming-soon page or photo portfolio. Saving keeps a draft in this project; it does not publish a site.</p>
    {!enabled && <p role="status">Sign in and choose a project to edit a page.</p>}
    <div className="project-page-editor__actions">
      {!draft && <button type="button" onClick={create} disabled={!enabled || Boolean(busy)}>Create a page</button>}
      {draft?.schema !== 'leaf.portfolio.v1' && <button type="button" onClick={createPortfolio} disabled={!enabled || Boolean(busy)}>Create a portfolio</button>}
      <button type="button" onClick={open} disabled={!enabled || Boolean(busy)}>{busy === 'loading' ? 'Opening…' : draft ? 'Reload saved page' : 'Open page'}</button>
    </div>
    <label><span>Import portfolio content</span><input type="file" accept=".json,application/json" disabled={!enabled || Boolean(busy)} onChange={importContent} /></label>
    {draft && <>
      <p role="status" aria-live="polite">{state}</p>
      <fieldset disabled={Boolean(busy) || !enabled}>
        <legend className="project-page-editor__legend">Page content</legend>
        {draft.schema === 'leaf.portfolio.v1' ? <PortfolioEditor document={draft} onChange={changePortfolio} projectId={projectId} /> : <>{TEXT_FIELDS.map(([field, label, limit]) => <label key={field} htmlFor={`${id}-${field}`}>
          <span>{label}</span>
          {field === 'description'
            ? <textarea id={`${id}-${field}`} ref={node => { fields.current[field] = node }} value={draft[field]} maxLength={limit} rows={4} onChange={event => change(field, event.target.value)} />
            : <input id={`${id}-${field}`} ref={node => { fields.current[field] = node }} value={draft[field]} maxLength={limit} onChange={event => change(field, event.target.value)} />}
        </label>)}
        <label htmlFor={`${id}-theme`}><span>Theme</span><select id={`${id}-theme`} value={draft.theme} onChange={event => change('theme', event.target.value)}>
          <option value="cream">Cream</option><option value="light">Light</option><option value="dark">Dark</option>
        </select></label>
        <label htmlFor={`${id}-alignment`}><span>Alignment</span><select id={`${id}-alignment`} value={draft.alignment} onChange={event => change('alignment', event.target.value)}>
          <option value="center">Centered</option><option value="left">Left</option>
        </select></label>
        </>}
      </fieldset>
      {validation && <p className="project-page-editor__error" role="alert">{validation}</p>}
      <div className="project-page-editor__actions">
        <button type="button" onClick={save} disabled={!enabled || Boolean(busy) || !dirty || Boolean(validation) || error?.conflict}>Save page</button>
        <button type="button" onClick={() => download()} disabled={!enabled || Boolean(busy) || dirty || !saved}>{busy === 'exporting' ? 'Downloading…' : 'Download Next.js source'}</button>
        {draft.schema === 'leaf.portfolio.v1' && <button type="button" onClick={() => download('ios')} disabled={!enabled || Boolean(busy) || dirty || !saved}>Download native iOS source</button>}
      </div>
      {dirty && <p className="project-page-editor__note">Save before downloading source. Reload saved page replaces your current draft.</p>}
      {saved?.document.schema === 'leaf.portfolio.v1' && draft.schema === 'leaf.portfolio.v1' && <section className="project-page-editor__native" aria-label="iOS preview">
        <h3>iOS preview</h3>
        <p className="project-page-editor__note">Build a private simulator preview of your saved portfolio. It is not an installed or published app.</p>
        <div className="project-page-editor__actions">
          <button type="button" onClick={buildNative} disabled={!enabled || Boolean(busy) || dirty || Boolean(validation) || Boolean(native?.active)
            || Boolean(native?.job && !['failed', 'cancelled'].includes(nativeStatus))}>Build iOS preview</button>
          {native?.job && <button type="button" onClick={checkNative} disabled={!enabled || Boolean(native.active)}>Check preview again</button>}
        </div>
        {dirty && <p className="project-page-editor__note">Save changes before building a new preview.</p>}
        {native && <p role="status" aria-live="polite">{nativeLabel || (native.active ? 'Submitting' : 'Submission unconfirmed')} · Saved revision {native.revision}</p>}
        {native?.message && <p className="project-page-editor__note" role="status">{native.message}</p>}
        {native?.url && <img className="project-page-editor__native-image" src={native.url} alt={`iOS simulator preview of saved portfolio revision ${native.job.revision}`} />}
      </section>}
    </>}
    {error && <p className="project-page-editor__error" role="alert">{error.message}</p>}
    {notice && <p role="status">{notice}</p>}
  </section>
}
