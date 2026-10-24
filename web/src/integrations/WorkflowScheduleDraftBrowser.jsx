import { useLayoutEffect, useRef, useState } from 'react'
import { IntegrationRefV1, IntegrationWorkflowScheduleDraftV1 } from '../../../vendor/assistant-contract/src/integration'
import { IntegrationWorkflowScheduleDraftPicker } from '../../../vendor/assistant-react/src/actions'
import { parseScheduleDraftReferencePage } from '../../../vendor/assistant-react/src/actions/workflow-schedule-draft-picker-state'
import { BASE_CSS, panel, secondaryButton, tokenStyle } from '../../../vendor/assistant-react/src/tokens'
import { UUID } from './client.js'

const failed = () => new Error('Schedule draft read unavailable.')
const failureCopy = 'The saved draft could not be read. Retry the read.'
const referenceKey = (reference) => JSON.stringify([reference.id, reference.version, reference.digest])

export function WorkflowScheduleDraftBrowser(props) {
  const availability = props.availability ?? 'ready'
  const valid = typeof props.contextKey === 'string' && props.contextKey.length > 0 &&
    typeof props.projectId === 'string' && UUID.test(props.projectId) &&
    typeof props.signal?.aborted === 'boolean' && typeof props.signal?.addEventListener === 'function' &&
    typeof props.signal?.removeEventListener === 'function' &&
    [props.guard, props.onForbidden, props.client?.listWorkflowScheduleDrafts, props.client?.getWorkflowScheduleDraft]
      .every(value => typeof value === 'function') &&
    (props.mode === undefined || props.mode === 'light' || props.mode === 'dark')
  const binding = JSON.stringify([props.contextKey, availability, valid])
  const [context, setContext] = useState(() => ({ client: props.client, projectId: props.projectId,
    signal: props.signal, binding, key: 1 }))
  if (context.client !== props.client || context.projectId !== props.projectId || context.signal !== props.signal || context.binding !== binding) {
    setContext({ client: props.client, projectId: props.projectId, signal: props.signal, binding, key: context.key + 1 })
    return null
  }
  if (!valid || !['ready', 'unavailable', 'forbidden'].includes(availability)) return <p role="status">Schedule draft discovery is unavailable.</p>
  return <ScopedDraftBrowser key={context.key} {...props} availability={availability} />
}

function ScopedDraftBrowser({ client, projectId, signal, contextKey, availability, guard, onForbidden, mode = 'light' }) {
  const [access, setAccess] = useState(signal.aborted ? 'forbidden' : availability)
  const [view, setView] = useState({ phase: 'idle', draft: null })
  const scope = useRef(null)
  const selected = useRef(null)
  const detail = useRef(null)
  const result = useRef(null)
  const callbacks = useRef({ guard, onForbidden })
  callbacks.current = { guard, onForbidden }

  function cancelDetail() {
    detail.current?.dispose()
    detail.current = null
  }
  useLayoutEffect(() => {
    const ctx = { controller: new AbortController() }
    scope.current = ctx
    const abort = () => {
      ctx.controller.abort()
      cancelDetail()
      selected.current = null
      setView({ phase: 'idle', draft: null })
      setAccess('forbidden')
    }
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
    return () => {
      scope.current = null
      ctx.controller.abort()
      cancelDetail()
      selected.current = null
      signal.removeEventListener('abort', abort)
    }
  }, [signal])
  useLayoutEffect(() => {
    if (view.phase !== 'idle') result.current?.focus()
  }, [view])

  function attempt(callerSignal) {
    const ctx = scope.current
    if (access !== 'ready' || !ctx || signal.aborted || ctx.controller.signal.aborted || callerSignal?.aborted) throw failed()
    const controller = new AbortController()
    const signals = [ctx.controller.signal, ...(callerSignal ? [callerSignal] : [])]
    let timer
    const dispose = () => {
      clearTimeout(timer)
      signals.forEach(source => source.removeEventListener('abort', dispose))
      controller.abort()
    }
    signals.forEach(source => source.addEventListener('abort', dispose, { once: true }))
    const current = () => scope.current === ctx && !signal.aborted && !controller.signal.aborted && !ctx.controller.signal.aborted
    return { controller, current, dispose, assert: () => { if (!current()) throw failed() },
      deadline: (callback) => { timer = setTimeout(callback, 45000) } }
  }
  function forbidden(operation, error) {
    if (!operation.current() || ![401, 403].includes(error?.status)) return
    scope.current.controller.abort()
    cancelDetail()
    selected.current = null
    setView({ phase: 'idle', draft: null })
    setAccess('forbidden')
    callbacks.current.onForbidden()
  }

  async function loadPage(query, callerSignal) {
    cancelDetail()
    selected.current = null
    setView({ phase: 'idle', draft: null })
    const request = { limit: query.limit, ...(query.after === undefined ? {} : { after: query.after }) }
    const operation = attempt(callerSignal)
    try {
      await callbacks.current.guard(operation.controller.signal)
      operation.assert()
      const response = await client.listWorkflowScheduleDrafts(projectId, { ...request, signal: operation.controller.signal })
      operation.assert()
      const page = parseScheduleDraftReferencePage(response, request.after)
      if (!page || page.references.length > request.limit) throw failed()
      await callbacks.current.guard(operation.controller.signal)
      operation.assert()
      return page
    } catch (error) { forbidden(operation, error); throw failed() }
    finally { operation.dispose() }
  }

  function read(reference) {
    // Parse synchronously, before starting any asynchronous work; never lend this copy to clients.
    const snapshot = IntegrationRefV1.parse(reference)
    if (detail.current?.key === referenceKey(snapshot)) return
    cancelDetail()
    selected.current = snapshot
    let operation
    try { operation = attempt() } catch { selected.current = null; return }
    operation.key = referenceKey(snapshot)
    detail.current = operation
    setView({ phase: 'pending', draft: null })
    operation.deadline(() => {
      if (detail.current !== operation || !operation.current()) return
      operation.dispose()
      detail.current = null
      setView({ phase: 'timeout', draft: null })
    })
    void (async () => {
      try {
        await callbacks.current.guard(operation.controller.signal)
        operation.assert()
        const response = await client.getWorkflowScheduleDraft(projectId, { ...snapshot }, { signal: operation.controller.signal })
        operation.assert()
        const draft = IntegrationWorkflowScheduleDraftV1.parse(response)
        if (draft.id !== snapshot.id || draft.version !== snapshot.version) throw failed()
        await callbacks.current.guard(operation.controller.signal)
        operation.assert()
        setView({ phase: 'success', draft })
      } catch (error) {
        if (operation.current()) {
          if ([401, 403].includes(error?.status)) forbidden(operation, error)
          else setView({ phase: 'failure', draft: null })
        }
      } finally {
        operation.dispose()
        if (detail.current === operation) detail.current = null
      }
    })().catch(() => {})
  }

  const draft = view.draft
  const message = ({ pending: 'Loading saved draft.', cancelled: 'Draft read cancelled.',
    timeout: 'The saved draft read timed out. Retry the read.', failure: failureCopy })[view.phase]
  return <section className="workflow-schedule-draft-browser leaf-assistant" aria-label="Saved schedule drafts"
    style={{ ...tokenStyle(mode), ...panel, minWidth: 0, maxWidth: '100%', overflowWrap: 'anywhere' }}>
    <style>{`${BASE_CSS}
      .ops-workspace .workflow-schedule-draft-browser button { margin: 0; max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
      .workflow-schedule-draft-browser [tabindex]:focus-visible { outline: 2px solid var(--leaf-ring); outline-offset: 2px; }
      .workflow-schedule-draft-browser dl { margin: 0; }`}</style>
    <IntegrationWorkflowScheduleDraftPicker contextKey={contextKey} availability={access} mode={mode}
      onLoadPage={loadPage} onSelect={read} />
    <div ref={result} role="status" aria-live="polite" aria-atomic="true" tabIndex={-1}>
      {message && <p>{message}</p>}
      {draft && <>
        <h3>{draft.title}</h3>
        <dl>
          <dt>Draft ID</dt><dd>{draft.id}</dd><dt>Draft version</dt><dd>{draft.version}</dd>
          <dt>Workflow reference ID</dt><dd>{draft.workflow_ref.id}</dd><dt>Workflow reference version</dt><dd>{draft.workflow_ref.version}</dd>
          <dt>Schedule</dt><dd>Daily</dd><dt>Timezone</dt><dd>{draft.schedule.timezone}</dd>
          <dt>Time</dt><dd>{String(draft.schedule.hour).padStart(2, '0')}:{String(draft.schedule.minute).padStart(2, '0')}</dd>
          <dt>Gap policy</dt><dd>{draft.schedule.gap_policy}</dd><dt>Fold policy</dt><dd>{draft.schedule.fold_policy}</dd>
        </dl>
      </>}
      <p>This is a saved draft. Nothing is scheduled or authorized to run.</p>
    </div>
    {view.phase === 'pending' && <button type="button" style={secondaryButton} onClick={() => {
      cancelDetail(); setView({ phase: 'cancelled', draft: null })
    }}>Cancel draft read</button>}
    {['cancelled', 'timeout', 'failure'].includes(view.phase) && selected.current && access === 'ready' &&
      <button type="button" style={secondaryButton} onClick={() => read(selected.current)}>Retry draft read</button>}
  </section>
}
