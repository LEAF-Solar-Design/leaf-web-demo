import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import WidgetDesigner from './WidgetDesigner.jsx'
import WidgetPreview from './WidgetPreview.jsx'
import { createEditorState, createWidgetDoc, sameDocument, widgetEditorReducer } from './useWidgetEditor.js'
import { validateWidgetDoc } from './widgetDoc.js'
import { laFitProblem } from './laFit.js'
import { claimLegacyWidget, loadProjectWidget, loadWidget, projectWidgetId, saveWidget, widgetProjectKey } from './widgetsClient.js'
import './widgets.css'

const validId = (id) => /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}(?![\s\S])/.test(id)
function validationError(doc) {
  try { validateWidgetDoc(doc); return null }
  catch (error) {
    for (const [id, surface] of Object.entries(doc.formats)) {
      for (let index = 0; index < surface.els.length; index += 1) {
        try { validateWidgetDoc({ ...doc, active: id, formats: { [id]: { ...surface, els: [surface.els[index]] } } }) }
        catch (problem) { return { detail: problem.message, pointer: `/formats/${id}/els/${index}` } }
      }
    }
    return { detail: error.message, pointer: '/formats' }
  }
}
export function WidgetsView({ projectId = null, accountScope = null, scopeKey, hidden = false, host = null, requestPublication }) {
  return <ProjectWidgetsView key={JSON.stringify([projectId, accountScope])} projectId={projectId} accountScope={accountScope}
    scopeKey={scopeKey} hidden={hidden} host={host} requestPublication={requestPublication} />
}
function ProjectWidgetsView({ projectId, accountScope, scopeKey, hidden = false, host = null, requestPublication }) {
  const local = typeof scopeKey === 'string' && scopeKey.trim().length > 0
  const [drafts, setDrafts] = useState(() => new Map())
  const [storeState, setStoreState] = useState(null)
  const [docId, setDocId] = useState('')
  const [loadedId, setLoadedId] = useState(null)
  const [artifactId, setArtifactId] = useState(null)
  const [head, setHead] = useState(null)
  const [sha256, setSha256] = useState(null)
  const [publication, setPublication] = useState(null)
  const [status, setStatus] = useState('empty')
  const [failure, setFailure] = useState(null)
  const [graphOutputs, setGraphOutputs] = useState(() => new Map())
  const onOutputs = useCallback((id, outputs) => setGraphOutputs((previous) => new Map(previous).set(id, outputs)), [])
  const requestGeneration = useRef(0)
  const restored = useRef(false)
  const projectKey = projectId ? widgetProjectKey(projectId, accountScope) : null
  const storeRef = useRef(storeState)
  storeRef.current = storeState
  const empty = useRef(createEditorState())
  const state = local ? drafts.get(scopeKey) ?? empty.current : storeState
  useEffect(() => { requestGeneration.current += 1; return () => { requestGeneration.current += 1 } }, [scopeKey])
  useEffect(() => () => { restored.current = false }, [projectKey])
  useEffect(() => {
    if (local || hidden || !projectKey || restored.current) return
    restored.current = true
    try {
      const id = localStorage.getItem(projectKey)
      if (validId(id ?? '')) void load(id)
    } catch { /* The server document can still be reopened by name. */ }
  }, [local, hidden, projectKey])
  function remember(id) {
    if (projectKey) try { localStorage.setItem(projectKey, id) } catch { /* Optional last-opened name. */ }
  }
  const dispatch = useCallback((action) => {
    if (local) setDrafts((previous) => {
      const next = new Map(previous)
      next.set(scopeKey, widgetEditorReducer(previous.get(scopeKey) ?? createEditorState(), action))
      return next
    })
    else {
      const previous = storeRef.current
      const next = previous || action.type === 'load' ? widgetEditorReducer(previous, action) : previous
      storeRef.current = next
      setStoreState(next)
      if (previous && next.doc !== previous.doc) {
        setStatus((previous) => previous === 'conflict' || previous === 'saving' ? previous : 'editing')
        setFailure((previous) => previous?.status === 409 ? previous : null)
      }
    }
  }, [local, scopeKey])
  const invalid = useMemo(() => {
    if (!state) return null
    const problem = validationError(state.doc)
    if (problem) return problem
    for (const [id, surface] of Object.entries(state.doc.formats)) {
      const index = surface.els.findIndex(({ binding }) => binding?.source === 'graph.output' && graphOutputs.has(binding.doc_id) && !graphOutputs.get(binding.doc_id).some((output) => output.name === binding.output))
      if (index >= 0) return { detail: 'Output not found', pointer: `/formats/${id}/els/${index}/binding/output` }
    }
    return null
  }, [state?.doc, graphOutputs])
  async function load(id = docId) {
    const ticket = ++requestGeneration.current
    setDocId(id); setStatus('loading'); setFailure(null); setPublication(null)
    const result = await (projectId ? loadProjectWidget(projectId, id, { accountScope }) : loadWidget(id))
    if (ticket !== requestGeneration.current) return
    if (!result.ok) { setStatus('error'); setFailure(result); return }
    try {
      const doc = result.doc ? validateWidgetDoc(result.doc) : createWidgetDoc()
      if (result.legacy && !claimLegacyWidget(projectId, id, accountScope)) {
        setStatus('error'); setFailure({ detail: 'This legacy document could not be associated with the selected project.' }); return
      }
      dispatch({ type: 'load', doc }); setLoadedId(id); setArtifactId(result.artifactId ?? id); setDocId(id); setHead(result.revision ?? null)
      setSha256(result.sha256 ?? null)
      setStatus(result.doc ? 'saved' : 'editing')
      if (result.doc) remember(id)
    } catch (error) { setStatus('invalid'); setFailure({ detail: error.message, pointer: '/graph' }) }
  }
  function newDocument() {
    requestGeneration.current += 1
    dispatch({ type: 'load', doc: createWidgetDoc() }); setLoadedId(docId); setArtifactId(null); setHead(null); setSha256(null); setPublication(null); setFailure(null); setStatus('editing')
  }
  async function save() {
    if (local || invalid || !state || state.transaction || !validId(loadedId ?? '')) return
    const ticket = ++requestGeneration.current, sentDoc = state.doc
    setStatus('saving'); setFailure(null); setPublication(null)
    let id = artifactId ?? loadedId
    if (!artifactId && projectId) {
      try { id = await projectWidgetId(projectId, loadedId) }
      catch {
        if (ticket === requestGeneration.current) { setStatus('error'); setFailure({ detail: 'Unable to resolve the project document ID.' }) }
        return
      }
      if (ticket !== requestGeneration.current) return
    }
    const result = await saveWidget(id, sentDoc, head)
    if (ticket !== requestGeneration.current) return
    if (result.ok) { setArtifactId(id); setHead(result.revision); setSha256(result.sha256 ?? null); setStatus(sameDocument(storeRef.current?.doc, sentDoc) ? 'saved' : 'editing'); remember(loadedId) }
    else if (result.status === 422 && !sameDocument(storeRef.current?.doc, sentDoc)) { setStatus('editing') }
    else { setFailure(result); setStatus(result.status === 409 ? 'conflict' : result.status === 422 ? 'invalid' : 'error') }
  }
  const displayedStatus = invalid ? 'invalid' : status
  const problem = invalid ?? failure
  const laProblem = state ? laFitProblem(state.doc) : null
  const canDownload = !local && state && !invalid && !state.transaction && ['saved', 'editing'].includes(status) && validId(loadedId ?? '') && docId === loadedId
  function downloadDefinition() {
    if (!canDownload) return
    let url, link
    try {
      const doc = validateWidgetDoc(state.doc)
      url = URL.createObjectURL(new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' }))
      link = document.createElement('a')
      link.href = url
      link.download = `widget-${loadedId}.json`
      document.body.appendChild(link)
      link.click()
    } finally {
      link?.remove()
      if (url) setTimeout(() => URL.revokeObjectURL(url), 0)
    }
  }
  return <section className="widgets-view" hidden={hidden} aria-label={local ? 'Text widget editor' : 'Widget designer'}>
    <h2>Widget designer</h2>
    {!local && <>
      <div className="widget-toolbar">
        <label>Document ID<input value={docId} maxLength={128} disabled={status === 'loading' || status === 'saving'} onChange={(e) => setDocId(e.target.value)} /></label>
        <button type="button" disabled={!validId(docId) || status === 'loading' || status === 'saving'} onClick={() => load()}>Load</button>
        <button type="button" disabled={!validId(docId) || status === 'loading' || status === 'saving'} onClick={newDocument}>New</button>
        {state && <button type="button" onClick={save} disabled={Boolean(invalid) || status === 'invalid' || status === 'loading' || status === 'saving' || status === 'conflict' || docId !== loadedId}>Save</button>}
        <button type="button" disabled={!canDownload} onClick={downloadDefinition}>Download widget definition</button>
        {requestPublication && <button type="button" disabled={!loadedId || !head || !sha256 || status !== 'saved' || Boolean(invalid) || Boolean(state?.transaction) || docId !== loadedId}
          onClick={async () => {
            const ticket = requestGeneration.current, sentDoc = state.doc
            const result = await requestPublication({ doc: sentDoc, source: { artifact_id: artifactId ?? loadedId, revision: head, digest: sha256 } })
            if (ticket !== requestGeneration.current || !sameDocument(storeRef.current?.doc, sentDoc)) return
            setPublication(result.ok ? 'Publication requested' : result.reason === 'la_too_tall' ? laFitProblem(state.doc) : result.reason)
          }}>Request publication</button>}
      </div>
      <p>Downloading does not save edits to the project.</p>
      {requestPublication && laProblem && <p>{laProblem}</p>}
      {publication && <p role="status" aria-label="Publication request">{publication}</p>}
      <p role="status">{({ empty: 'Enter a Document ID, then choose New or Load.', loading: 'Loading widget…', saving: 'Saving widget…', saved: `Saved revision ${head ?? ''}.`, editing: 'Local edits. Save when ready.', invalid: 'Invalid widget. Correct the indicated field before saving.', conflict: 'Conflict: the document changed. Your local edits are kept. Reload to use the stored revision.', error: 'The document request failed. Your local edits are kept.' })[displayedStatus]}</p>
      {problem && <p role="alert">{typeof problem.detail === 'string' ? problem.detail : JSON.stringify(problem.detail)}{problem.pointer ? ` (${problem.pointer})` : ''}</p>}
      {status === 'conflict' && <button type="button" onClick={() => load(loadedId)}>Reload</button>}
    </>}
    {state && <WidgetDesigner key={local ? scopeKey : loadedId ?? 'store'} state={state} dispatch={dispatch} local={local} hidden={hidden || (!local && status === 'loading')} invalid={Boolean(invalid)} onOutputs={onOutputs} graphOutputs={graphOutputs} />}
    {!local && !hidden && status !== 'loading' && state && loadedId && head != null && !invalid && <WidgetPreview docId={artifactId ?? loadedId} doc={state.doc} format={state.doc.active} host={host} />}
  </section>
}
export default WidgetsView
