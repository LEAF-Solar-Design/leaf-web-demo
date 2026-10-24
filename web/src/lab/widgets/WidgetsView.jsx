import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import WidgetDesigner from './WidgetDesigner.jsx'
import { createEditorState, createWidgetDoc, sameDocument, widgetEditorReducer } from './useWidgetEditor.js'
import { DESIGNER_DEFAULT_DOC } from './designerDefault.js'
import { validateWidgetDoc } from './widgetDoc.js'
import { loadWidget, saveWidget } from './widgetsClient.js'
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
export function WidgetsView({ scopeKey, hidden = false }) {
  const local = typeof scopeKey === 'string' && scopeKey.trim().length > 0
  const [drafts, setDrafts] = useState(() => new Map())
  const [storeState, setStoreState] = useState(null)
  const [docId, setDocId] = useState('')
  const [loadedId, setLoadedId] = useState(null)
  const [head, setHead] = useState(null)
  const [status, setStatus] = useState('empty')
  const [failure, setFailure] = useState(null)
  const [graphOutputs, setGraphOutputs] = useState(() => new Map())
  const onOutputs = useCallback((id, outputs) => setGraphOutputs((previous) => new Map(previous).set(id, outputs)), [])
  const requestGeneration = useRef(0)
  const storeRef = useRef(storeState)
  storeRef.current = storeState
  const empty = useRef(createEditorState())
  const state = local ? drafts.get(scopeKey) ?? empty.current : storeState
  useEffect(() => { requestGeneration.current += 1; return () => { requestGeneration.current += 1 } }, [scopeKey])
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
    setStatus('loading'); setFailure(null)
    const result = await loadWidget(id)
    if (ticket !== requestGeneration.current) return
    if (!result.ok) { setStatus('error'); setFailure(result); return }
    try {
      const doc = result.doc ? validateWidgetDoc(result.doc) : createWidgetDoc(DESIGNER_DEFAULT_DOC)
      dispatch({ type: 'load', doc }); setLoadedId(id); setDocId(id); setHead(result.revision ?? null)
      setStatus(result.doc ? 'saved' : 'editing')
    } catch (error) { setStatus('invalid'); setFailure({ detail: error.message, pointer: '/graph' }) }
  }
  function newDocument() {
    requestGeneration.current += 1
    dispatch({ type: 'load', doc: createWidgetDoc(DESIGNER_DEFAULT_DOC) }); setLoadedId(docId); setHead(null); setFailure(null); setStatus('editing')
  }
  async function save() {
    if (local || invalid || !state || state.transaction || !validId(loadedId ?? '')) return
    const ticket = ++requestGeneration.current, sentDoc = state.doc
    setStatus('saving'); setFailure(null)
    const result = await saveWidget(loadedId, sentDoc, head)
    if (ticket !== requestGeneration.current) return
    if (result.ok) { setHead(result.revision); setStatus(sameDocument(storeRef.current?.doc, sentDoc) ? 'saved' : 'editing') }
    else if (result.status === 422 && !sameDocument(storeRef.current?.doc, sentDoc)) { setStatus('editing') }
    else { setFailure(result); setStatus(result.status === 409 ? 'conflict' : result.status === 422 ? 'invalid' : 'error') }
  }
  const displayedStatus = invalid ? 'invalid' : status
  const problem = invalid ?? failure
  return <section className="widgets-view" hidden={hidden} aria-label={local ? 'Text widget editor' : 'Widget designer'}>
    <h2>Widget designer</h2>
    {!local && <>
      <div className="widget-toolbar">
        <label>Document ID<input value={docId} maxLength={128} disabled={status === 'loading' || status === 'saving'} onChange={(e) => setDocId(e.target.value)} /></label>
        <button type="button" disabled={!validId(docId) || status === 'loading' || status === 'saving'} onClick={() => load()}>Load</button>
        <button type="button" disabled={!validId(docId) || status === 'loading' || status === 'saving'} onClick={newDocument}>New</button>
        {state && <button type="button" onClick={save} disabled={Boolean(invalid) || status === 'invalid' || status === 'loading' || status === 'saving' || status === 'conflict' || docId !== loadedId}>Save</button>}
      </div>
      <p role="status">{({ empty: 'Choose a project to create a text widget.', loading: 'Loading widget…', saving: 'Saving widget…', saved: `Saved revision ${head ?? ''}.`, editing: 'Local edits. Save when ready.', invalid: 'Invalid widget. Correct the indicated field before saving.', conflict: 'Conflict: the document changed. Your local edits are kept. Reload to use the stored revision.', error: 'The document request failed. Your local edits are kept.' })[displayedStatus]}</p>
      {problem && <p role="alert">{typeof problem.detail === 'string' ? problem.detail : JSON.stringify(problem.detail)}{problem.pointer ? ` (${problem.pointer})` : ''}</p>}
      {status === 'conflict' && <button type="button" onClick={() => load(loadedId)}>Reload</button>}
    </>}
    {state && <WidgetDesigner key={local ? scopeKey : loadedId ?? 'store'} state={state} dispatch={dispatch} local={local} hidden={hidden || (!local && status === 'loading')} invalid={Boolean(invalid)} onOutputs={onOutputs} graphOutputs={graphOutputs} />}
  </section>
}
export default WidgetsView
