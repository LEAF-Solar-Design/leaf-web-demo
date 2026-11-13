import { useEffect, useId, useRef, useState } from 'react'
import { subscribeBeforeLogout, subscribeTokenStored } from '../auth.js'
import { composerDraftAccountScope } from '../lib/composerDraft.js'
import { downloadProjectPageSource, loadProjectPage, saveProjectPage, validatePageDocument, submitProjectNativePreview, loadProjectNativePreview, downloadProjectNativePreviewScreenshot, validateNativePreview, proposeProjectPage, portfolioTextChanges, validateProjectPageProposal, PHOTO_PROPOSAL_SCHEMA, validateProjectPhotoAssignmentProposal } from './projectPageClient.js'
import { isValidPageAction } from './projectPageClient.js'
import { getStoredOrgId } from '../api.js'
import { getStoredActorBindingId } from '../projects/api.js'
import PortfolioEditor, { PortfolioPreview } from './PortfolioEditor.jsx'
import { emptyPortfolio, importPortfolio } from './portfolioDocument.js'
import { readActiveScope, subscribeActiveScope } from '../auth/activeScope.js'
import { cloneDocument, selectionIdentity, singleFieldChange } from './portfolioEdits.js'
import { appearanceStyle, textStyle } from './pageDesign.js'
import ElementInspector, { InlineText } from './ElementInspector.jsx'
import './projectPageEditor.css'

const EMPTY_PAGE = { label: 'Your page', headline: 'Coming soon', description: '', status: '', theme: 'cream', alignment: 'center' }
const TEXT_FIELDS = [['label', 'Page label', 120], ['headline', 'Headline', 240], ['description', 'Description', 1200], ['status', 'Status text', 80]]
const stable = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item)
const equalDocument = (left, right) => left && right && stable(left) === stable(right)

export function PagePreview({ document, onSelectField, projectId, onInlineChange, onSelectElement, selectedElement }) {
  if (!document) return null
  if (document.schema === 'leaf.portfolio.v1') return <PortfolioPreview document={document} projectId={projectId} onSelectField={onSelectField} onInlineChange={onInlineChange} onSelectElement={onSelectElement} selectedElement={selectedElement} />
  const text = field => onInlineChange ? <InlineText document={document} path={[field]} label={field} limit={TEXT_FIELDS.find(([key]) => key === field)[2]} onChange={onInlineChange} onSelect={() => onSelectElement?.(`${selectionIdentity(document)}:textStyles.${field}`)} /> : onSelectField
    ? <button type="button" className="project-page__edit" aria-label={`Edit ${field}`} onClick={() => onSelectField(field)}>{document[field]}</button>
    : document[field]
  const design = field => textStyle(onInlineChange && selectedElement === `${selectionIdentity(document)}:textStyles.${field}` ? { ...document.textStyles?.[field], hidden: undefined } : document.textStyles?.[field])
  return <main className="project-page" data-element={`${selectionIdentity(document)}:appearance`} data-theme={document.theme} data-alignment={document.alignment} style={appearanceStyle(document.appearance)}>
    <div className="project-page__content">
      <p className="project-page__label" data-element={`${selectionIdentity(document)}:textStyles.label`} style={design('label')}>{text('label')}</p>
      <h1 className="project-page__headline" data-element={`${selectionIdentity(document)}:textStyles.headline`} style={design('headline')}>{text('headline')}</h1>
      {(document.description || onInlineChange) && <p className="project-page__description" data-element={`${selectionIdentity(document)}:textStyles.description`} style={design('description')}>{text('description')}</p>}
      {(document.status || onInlineChange) && <p className="project-page__status" data-element={`${selectionIdentity(document)}:textStyles.status`} style={design('status')}>{text('status')}</p>}
      {typeof document.action?.label === 'string' && document.action.label && <p className="project-page__action">
        {isValidPageAction(document.action)
          ? <a href={document.action.href} target={document.action.href.startsWith('https:') ? '_blank' : undefined} rel={document.action.href.startsWith('https:') ? 'noopener noreferrer' : undefined}>{document.action.label}</a>
          : document.action.label}
      </p>}
    </div>
  </main>
}

export default function ProjectPageEditor(props) {
  return <ScopedPageEditor key={JSON.stringify([props.projectId || null, props.accountScope || null])} {...props} />
}

function ScopedPageEditor({ projectId, accountScope, onPreview, onSession, selectedField, incomingProposal, selectedElement, onSelectElement }) {
  const [draft, setDraft] = useState(null)
  const [saved, setSaved] = useState(null)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState('')
  const [suspended, setSuspended] = useState(false)
  const [native, setNative] = useState(null)
  const [reviewRequest, setReviewRequest] = useState(0)
  const [instruction, setInstruction] = useState('')
  const [proposal, setProposal] = useState(null)
  const [localElement, setLocalElement] = useState(null)
  const sourceSnapshot = useRef(null)
  const history = useRef({ past: [], future: [], key: null, time: 0 })
  const sessionActions = useRef({})
  const session = useRef(null)
  if (!session.current) session.current = Object.fromEntries(['change', 'save', 'reload', 'open', 'undo', 'redo', 'discard'].map(key => [key, (...args) => {
    if (live.current) return sessionActions.current[key]?.(...args)
  }]))
  const intake = useRef(null)
  const actionRow = useRef(null)
  const editor = useRef(null)
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
  sourceSnapshot.current = { saved, draft, busy, enabled }
  useEffect(() => { setProposal(null) }, [draft, saved])
  useEffect(() => { setInstruction('') }, [saved])

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
  const acceptSaved = page => {
    const previous = sourceSnapshot.current.saved
    if (previous?.revision !== page?.revision || previous?.digest !== page?.digest) {
      // Retire the old observer before publishing the new saved source.
      clearNative()
      setNative(null)
    }
    sourceSnapshot.current = { ...sourceSnapshot.current, saved: page }
    setSaved(page)
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
      setProposal(null)
      setInstruction('')
      setDraft(null)
      sourceSnapshot.current = { saved: null, draft: null }
      history.current = { past: [], future: [], key: null, time: 0 }
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

  useEffect(() => {
    const field = typeof selectedField === 'string' ? selectedField : selectedField?.field
    if (TEXT_FIELDS.some(([name]) => name === field)) fields.current[field]?.focus()
  }, [selectedField, draft !== null])
  useEffect(() => {
    if (!draft || !actionRow.current) return
    const heading = editor.current.closest('.browser-project-panel')?.querySelector('.browser-panel-heading')
    const measure = () => {
      editor.current?.style.setProperty('--page-actions-height', `${actionRow.current.getBoundingClientRect().height}px`)
      editor.current?.style.setProperty('--page-sticky-top', `${heading?.getBoundingClientRect().height || 0}px`)
    }
    measure()
    if (!globalThis.ResizeObserver) return
    const observer = new ResizeObserver(measure)
    observer.observe(actionRow.current)
    if (heading) observer.observe(heading)
    return () => observer.disconnect()
  }, [draft !== null])

  const show = document => preview.current?.({ document, name: document.name || document.label || 'Page' })
  const replaceDraft = (document, savedPage = sourceSnapshot.current.saved) => {
    history.current = { past: [], future: [], key: null, time: 0 }
    sourceSnapshot.current = { draft: document, saved: savedPage }
    setDraft(document)
  }
  const create = () => {
    if (!enabled || operation.current) return
    const document = { ...EMPTY_PAGE }
    setProposal(null); setInstruction('')
    replaceDraft(document, null); setSaved(null); setError(null); setNotice('')
    show(document)
  }
  const createPortfolio = () => {
    if (!enabled || operation.current) return
    const document = emptyPortfolio()
    setProposal(null); setInstruction('')
    replaceDraft(document); setError(null); setNotice('')
    show(document)
  }
  const perform = async (kind, action) => {
    if (!live.current || !enabled || operation.current) return
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
    setProposal(null)
    const page = await loadProjectPage(projectId, { signal })
    if (!current()) return
    if (!page) { setNotice('No saved page yet. Choose Create a page to begin.'); return }
    acceptSaved(page); replaceDraft(page.document, page)
    show(page.document)
  })
  const importContent = event => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    const expectedScope = readActiveScope()
    if (!expectedScope) { setError({ message: 'Sign in before importing project photos.' }); return }
    void perform('importing', async (signal, current) => {
      setProposal(null)
      const document = await importPortfolio(file, projectId, { signal, expectedScope })
      if (!current() || readActiveScope() !== expectedScope) return
      replaceDraft(document); show(document)
      setNotice('Portfolio imported as an unsaved draft. Review it, then Save page.')
    })
  }
  const save = () => !error?.conflict && perform('saving', async (signal, current) => {
    const snapshot = sourceSnapshot.current
    if (!snapshot.draft || error?.conflict) return
    const page = await saveProjectPage(projectId, snapshot.draft, snapshot.saved?.revision || 0, { signal })
    if (!current()) return
    // Retain the exact draft object and its ephemeral selection identities on save.
    const accepted = equalDocument(page.document, snapshot.draft) ? { ...page, document: snapshot.draft } : page
    acceptSaved(accepted)
    sourceSnapshot.current = { saved: accepted, draft: snapshot.draft }
    setDraft(snapshot.draft)
    show(snapshot.draft)
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
  const changePortfolio = (document, options = {}) => {
    if (!live.current || !enabled || operation.current || !document) return
    const before = sourceSnapshot.current.draft
    const coalesceKey = Object.hasOwn(options, 'coalesceKey') ? options.coalesceKey : singleFieldChange(before, document)
    if (equalDocument(before, document)) return
    const now = Date.now(), stack = history.current
    if (before && (!coalesceKey || stack.key !== coalesceKey || now - stack.time > 1500)) {
      stack.past.push(before)
      if (stack.past.length > 50) stack.past.shift()
    }
    stack.future = []; stack.key = coalesceKey; stack.time = now
    sourceSnapshot.current = { ...sourceSnapshot.current, draft: document }
    setProposal(null); setDraft(document); setNotice(''); show(document)
  }
  const change = (field, value) => {
    const document = cloneDocument(sourceSnapshot.current.draft)
    document[field] = value
    changePortfolio(document, { coalesceKey: field })
  }
  const changeAction = (field, value) => {
    const document = cloneDocument(sourceSnapshot.current.draft)
    const action = { label: '', href: '', ...document.action, [field]: value }
    if (action.label || action.href) document.action = action
    else delete document.action
    changePortfolio(document, { coalesceKey: `action.${field}` })
  }
  const travel = direction => {
    if (!live.current || !enabled || operation.current) return
    const stack = history.current, source = direction === 'undo' ? stack.past : stack.future
    if (!source.length) return
    const destination = direction === 'undo' ? stack.future : stack.past
    destination.push(sourceSnapshot.current.draft)
    if (destination.length > 50) destination.shift()
    const document = source.pop()
    stack.key = null
    sourceSnapshot.current = { ...sourceSnapshot.current, draft: document }
    setProposal(null); setDraft(document); setNotice(''); show(document)
  }
  const proposalBlocked = !enabled || Boolean(busy) || dirty || Boolean(validation) || !saved || Boolean(error?.conflict) || Boolean(native?.active)
  useEffect(() => {
    if (!incomingProposal) return
    if (intake.current?.request !== incomingProposal) intake.current = { request: incomingProposal, opened: false, done: false }
    const pending = intake.current
    if (pending.done) return
    const finish = accepted => {
      pending.done = true
      try { incomingProposal.consumed(accepted) } catch { /* intake remains private and non-fatal */ }
    }
    try {
      if (!live.current || !enabled || composerDraftAccountScope() !== accountScope
        || incomingProposal.accountScope !== accountScope || incomingProposal.activeScope !== readActiveScope()
        || !incomingProposal.isCurrent()) { finish(false); return }
      // A load started by this request must settle before reviewing its snapshot.
      if (pending.opened && busy === 'loading') return
      if (busy || operation.current || dirty || validation || error?.conflict || nativeSession.current?.active || native?.active) {
        finish(false); return
      }
      if (!draft && !saved && !pending.opened) {
        if (proposal) { finish(false); return }
        pending.opened = true
        void open()
        return
      }
      if (proposalBlocked || !draft || draft.schema !== 'leaf.portfolio.v1' || proposal) { finish(false); return }
      if (incomingProposal.result?.schema === PHOTO_PROPOSAL_SCHEMA) {
        const snapshot = sourceSnapshot.current
        const provenance = incomingProposal.provenance
        const scope = readActiveScope()
        const current = () => live.current && intake.current === pending && enabled
          && composerDraftAccountScope() === accountScope && readActiveScope() === scope
          && incomingProposal.isCurrent() && provenance?.orgId && provenance?.actorBindingId
          && getStoredOrgId() === provenance.orgId && getStoredActorBindingId() === provenance.actorBindingId
          && typeof provenance.sessionId === 'string' && Boolean(provenance.sessionId)
          && typeof provenance.turnId === 'string' && Boolean(provenance.turnId.trim()) && provenance.turnId.length <= 200
          && Number.isSafeInteger(provenance.seq) && provenance.seq > 0
          && sourceSnapshot.current.saved === snapshot.saved && sourceSnapshot.current.draft === snapshot.draft
          && sourceSnapshot.current.enabled && !sourceSnapshot.current.busy && !operation.current && !nativeSession.current?.active
        if (!current()) { finish(false); return }
        let cancelled = false
        void validateProjectPhotoAssignmentProposal(projectId, saved, incomingProposal.result).then(value => {
          if (cancelled || !current()) { finish(false); return }
          setProposal({ photo: true, snapshot: { ...snapshot, isCurrent: current }, document: value.proposed_document, changes: value.changes })
          finish(true)
        }).catch(() => finish(false))
        return () => { cancelled = true }
      }
      const value = validateProjectPageProposal(projectId, saved, incomingProposal.result)
      const document = JSON.parse(JSON.stringify(value.proposed_document))
      const changes = portfolioTextChanges(saved.document, document)
      if (!changes.length) setNotice('No text changes proposed.')
      else setProposal({ snapshot: { ...sourceSnapshot.current, isCurrent: incomingProposal.isCurrent }, document, changes })
      finish(true)
    } catch { finish(false) }
  }, [incomingProposal, saved, draft, busy, enabled, dirty, validation, error, native]) // existing fenced operations own loading
  const generateProposal = () => {
    if (!live.current || proposalBlocked || operation.current || !instruction.trim() || Array.from(instruction).length > 4000
      || draft?.schema !== 'leaf.portfolio.v1') return
    const snapshot = sourceSnapshot.current
    void perform('proposing', async (signal, current) => {
      setProposal(null)
      const result = await proposeProjectPage(projectId, snapshot.saved, instruction, { signal })
      if (!current() || sourceSnapshot.current.saved !== snapshot.saved || sourceSnapshot.current.draft !== snapshot.draft) return
      const changes = portfolioTextChanges(snapshot.saved.document, result.proposed_document)
      if (!changes.length) { setNotice('No text changes proposed.'); return }
      setProposal({ snapshot, document: result.proposed_document, changes })
    })
  }
  const cancelProposal = () => {
    if (busy !== 'proposing') return
    operation.current?.abort()
    operation.current = null
    setBusy(null); setProposal(null); setNotice('Proposal generation cancelled.')
  }
  const useProposal = () => {
    if (!live.current || !proposal || proposalBlocked || operation.current || nativeSession.current?.active || sourceSnapshot.current.saved !== proposal.snapshot.saved
      || sourceSnapshot.current.draft !== proposal.snapshot.draft || (proposal.snapshot.isCurrent && !proposal.snapshot.isCurrent())) { setProposal(null); return }
    const document = proposal.document
    if (!proposal.photo) portfolioTextChanges(saved.document, document)
    changePortfolio(cloneDocument(document, sourceSnapshot.current.draft), { coalesceKey: null })
    setNotice('Review the draft, then Save page.')
  }
  const state = busy === 'saving' ? 'Saving…' : dirty ? 'Unsaved changes' : saved ? 'Saved' : ''
  sessionActions.current = { change: changePortfolio, save, reload: open, open,
    undo: () => travel('undo'), redo: () => travel('redo'),
    discard: () => { if (sourceSnapshot.current.saved) changePortfolio(cloneDocument(sourceSnapshot.current.saved.document), { coalesceKey: null }) },
  }
  useEffect(() => {
    onSession?.({ session: session.current, document: draft, state: { enabled, dirty, busy, validation,
      canUndo: history.current.past.length > 0, canRedo: history.current.future.length > 0,
      savedRevision: saved?.revision, conflict: Boolean(error?.conflict), error: error?.message, notice, status: state } })
  }, [draft, saved, enabled, dirty, busy, validation, error, notice, onSession])
  const proposalNotice = notice && /^(?:No text changes proposed\.|Review the draft, then Save page\.|Proposal generation cancelled\.|Proposal discarded\.)/.test(notice)

  const observeNative = async (session, submit = false) => {
    if (!live.current || nativeSession.current !== session || session.active) return
    session.active = true
    session.deadline = Date.now() + 15 * 60 * 1000
    const controller = new AbortController()
    session.controller = controller
    const current = () => live.current && nativeSession.current === session && !controller.signal.aborted
      && sourceSnapshot.current.saved?.revision === session.revision
      && sourceSnapshot.current.saved?.digest === session.digest
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
    if (enabled && !busy && !operation.current && !session?.active && session?.job) void observeNative(session)
  }
  const nativeStatus = native?.job?.status
  const nativeLabel = nativeStatus === 'complete' ? native.url ? 'Ready' : native.active ? 'Building' : 'Screenshot unavailable'
    : ({ queued: 'Queued', running: 'Building', failed: 'Failed', cancelled: 'Cancelled' })[nativeStatus]

  return <section className="project-page-editor" aria-label="Page editor" ref={editor} onFocusCapture={event => {
    const row = actionRow.current
    if (row && !row.contains(event.target) && event.target.getBoundingClientRect().top < row.getBoundingClientRect().bottom) {
      event.target.scrollIntoView?.({ block: 'nearest' })
    }
  }}>
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
      <div className="project-page-editor__save-row" ref={actionRow}>
        <button type="button" onClick={save} disabled={!enabled || Boolean(busy) || !dirty || Boolean(validation) || error?.conflict}>Save page</button>
        <p role="status" aria-live="polite">{state}</p>
      </div>
      {draft.schema === 'leaf.portfolio.v1' && <section className="project-page-editor__proposal" aria-label="Ask for a text change">
        <h3>Ask for a text change</h3>
        <p className="project-page-editor__note">Propose text-only edits to a saved portfolio. Photos, layout and structure stay the same. Review before using any changes.</p>
        <label htmlFor={`${id}-instruction`}><span>What should change?</span>
          <textarea id={`${id}-instruction`} rows={3} maxLength={4000} value={instruction} disabled={!enabled || Boolean(busy)} onChange={event => setInstruction(event.target.value)} />
        </label>
        {(dirty || validation || !saved) && <p className="project-page-editor__note">Save changes first to request a text proposal.</p>}
        <div className="project-page-editor__actions">
          <button type="button" onClick={generateProposal} disabled={proposalBlocked || !instruction.trim() || Array.from(instruction).length > 4000}>{busy === 'proposing' ? 'Generating proposal…' : 'Generate proposal'}</button>
          {busy === 'proposing' && <button type="button" onClick={cancelProposal}>Cancel generation</button>}
        </div>
        {proposal && <section aria-label={proposal.photo ? 'Review proposed photo assignments' : 'Review proposed text'} className="project-page-editor__proposal-review">
          <h4>{proposal.photo ? 'Review proposed photo assignments' : 'Review proposed text'}</h4>
          <p className="project-page-editor__note">Your draft and preview stay as they are until you use these changes.</p>
          {proposal.photo && <p>Use proposed assignments to review the draft, then Save page.</p>}
          <ol>{proposal.changes.map((change, index) => <li key={index}>
            {proposal.photo ? <><h5>{change.material_name}</h5>
              <dl><dt>Original photo ID</dt><dd>{change.material_id}</dd><dt>Target gallery</dt><dd>{change.gallery_title} ({change.gallery_slug})</dd>
                <dt>Caption</dt><dd>{change.title || '(Empty)'}</dd><dt>Alt text</dt><dd>{change.alt}</dd></dl></> : <>
              <h5>{change.label}</h5>
              <dl><dt>Current text</dt><dd>{change.before || '(Empty)'}</dd><dt>Proposed text</dt><dd>{change.after || '(Empty)'}</dd></dl></>}
          </li>)}</ol>
          <div className="project-page-editor__actions">
            <button type="button" onClick={useProposal} disabled={proposalBlocked}>{proposal.photo ? 'Use proposed assignments' : 'Use proposed changes'}</button>
            <button type="button" onClick={() => { setProposal(null); setNotice('Proposal discarded. Your draft is unchanged.') }}>Discard proposal</button>
          </div>
        </section>}
        {proposalNotice && <p role="status">{notice}</p>}
      </section>}
      {validation && <div className="project-page-editor__validation">
        <p className="project-page-editor__error" role="alert">{validation}</p>
        {draft.schema === 'leaf.portfolio.v1' && <button type="button" onClick={() => setReviewRequest(value => value + 1)}>Show all fields to review errors</button>}
      </div>}
      <fieldset disabled={Boolean(busy) || !enabled}>
        <legend className="project-page-editor__legend">Page content</legend>
        <details><summary>Element design</summary><ElementInspector document={draft} selected={selectedElement ?? localElement} onSelect={onSelectElement || setLocalElement} onChange={changePortfolio} /></details>
        {draft.schema === 'leaf.portfolio.v1' ? <PortfolioEditor document={draft} onChange={changePortfolio} projectId={projectId} reviewRequest={reviewRequest} selectedField={enabled ? selectedField : null} onSelectElement={onSelectElement || setLocalElement} /> : <>{TEXT_FIELDS.map(([field, label, limit]) => <label key={field} htmlFor={`${id}-${field}`} onFocus={() => (onSelectElement || setLocalElement)(`${selectionIdentity(draft)}:textStyles.${field}`)}>
          <span>{label}</span>
          {field === 'description'
            ? <textarea id={`${id}-${field}`} ref={node => { fields.current[field] = node }} value={draft[field]} maxLength={limit} rows={4} onChange={event => change(field, event.target.value)} />
            : <input id={`${id}-${field}`} ref={node => { fields.current[field] = node }} value={draft[field]} maxLength={limit} onChange={event => change(field, event.target.value)} />}
        </label>)}
          <label><span>Contact link label</span><input value={draft.action?.label ?? ''} maxLength={120}
            onChange={event => changeAction('label', event.target.value)} /></label>
          <label><span>Contact link URL</span><input value={draft.action?.href ?? ''} maxLength={2048}
            onChange={event => changeAction('href', event.target.value)} /></label>
          <p className="project-page-editor__note">Optional. Use an HTTPS link or mailto address. Clear both fields to remove the link.</p>
        <label htmlFor={`${id}-theme`}><span>Theme</span><select id={`${id}-theme`} value={draft.theme} onChange={event => change('theme', event.target.value)}>
          <option value="cream">Cream</option><option value="light">Light</option><option value="dark">Dark</option>
        </select></label>
        <label htmlFor={`${id}-alignment`}><span>Alignment</span><select id={`${id}-alignment`} value={draft.alignment} onChange={event => change('alignment', event.target.value)}>
          <option value="center">Centered</option><option value="left">Left</option>
        </select></label>
        </>}
      </fieldset>
      <div className="project-page-editor__actions">
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
          {native?.job && <button type="button" onClick={checkNative} disabled={!enabled || Boolean(busy) || Boolean(native.active)}>Check preview again</button>}
        </div>
        {dirty && <p className="project-page-editor__note">Save changes before building a new preview.</p>}
        {native && <p role="status" aria-live="polite">{nativeLabel || (native.active ? 'Submitting' : 'Submission unconfirmed')} · Saved revision {native.revision}</p>}
        {native?.message && <p className="project-page-editor__note" role="status">{native.message}</p>}
        {native?.url && <img className="project-page-editor__native-image" src={native.url} alt={`iOS simulator preview of saved portfolio revision ${native.job.revision}`} />}
      </section>}
    </>}
    {error && <p className="project-page-editor__error" role="alert">{error.message}</p>}
    {notice && !proposalNotice && <p role="status">{notice}</p>}
  </section>
}
